"""Story rules and the evolution methodology that fix the measured story problems:
contradictions, procedural windfalls, pacing, stalling, open clues, repetition,
arbitrary ties, and evaluation on truncated stories."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import random
import unittest
from collections import Counter
from unittest.mock import patch

import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Mission, Outcome, WorldGraph, rules
from core_engine.combat import CheckResult
from core_engine.evolution import NarrativeEvolution, evaluate_task
from core_engine.fitness import _fitness_breakdown, fitness
from utils.config_loader import config


def _genome(**traits):
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    for name, value in traits.items():
        setattr(genome, name, value)
    return genome


def _quiet(**story_rules):
    patches = [
        patch.dict(config._config["unexpected_events"], {"enabled": False}),
        patch.dict(config._config["game_state"], {"arrest_location": "Cell"}),
    ]
    if story_rules:
        patches.append(patch.dict(config._config["story_rules"], story_rules))
    return patches


def _rolls(*passes):
    results = iter(passes)

    def roll(combat, stat, modifier=0, inventory=(), step=0):
        passed = next(results)
        return CheckResult(stat, 1 if passed else 100, combat.target(stat, modifier, inventory, step), passed, step)

    return patch("core_engine.combat.CombatSystem.roll", roll)


def _names(state):
    return [action.name for action in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class _Rules(unittest.TestCase):
    rules = {}

    def setUp(self):
        for patcher in _quiet(**self.rules):
            patcher.start()
            self.addCleanup(patcher.stop)


def _gate_world(*actions, tags=("gate",)):
    world = WorldGraph()
    for name, location_tags in (("Gate", list(tags)), ("Yard", ["yard"]), ("Cell", ["cell"])):
        world.add_location(Location(name, location_tags))
    world.locations["Gate"].actions = [
        *actions, Action("Wait", [Outcome("You wait.", tags=["neutral"])], repeat="always", source="location", owner="Gate"),
    ]
    world.locations["Gate"].connect(world.locations["Yard"])
    return world


class FailureNarrationTests(_Rules):
    def _state(self, *actions, tags=("gate",)):
        return GameState(_gate_world(*actions, tags=tags), _genome(), start="Gate", rng=random.Random(0))

    def test_an_arrest_tells_no_success(self):
        sneak = Action("Sneak inside", [
            Outcome("You slip past unnoticed.", tags=["stealth", "success"], coin_change=5),
        ], source="location", owner="Gate")
        state = self._state(sneak, tags=("guarded",))
        coins = state.coins
        with _rolls(False):
            _choose(state, "Sneak inside")
        self.assertIn("[FAILURE] The traveler lost the encounter and was dragged to the Cell.", state.history)
        self.assertNotIn("RESULT: You slip past unnoticed.", state.history)
        self.assertNotIn("RESULT: ", state.history)             # no empty result line either
        self.assertEqual((state.current.name, state.coins), ("Cell", coins))  # nor the success's coins

    def test_checks_tell_the_branch_they_decided(self):
        steal = Action("Steal a pouch", [
            Outcome("You lift a heavy pouch.", tags=["stealth", "success"], coin_change=5),
            Outcome("A guard drives you off.", tags=["stealth", "threat", "failure"], move_to="Yard"),
        ], source="location", owner="Gate", repeat="always")
        state = self._state(steal)
        with _rolls(True):
            _choose(state, "Steal a pouch")
        self.assertIn("RESULT: You lift a heavy pouch.", state.history)   # passed: a success
        with _rolls(False):
            _choose(state, "Steal a pouch")
        self.assertIn("RESULT: A guard drives you off.", state.history)   # failed: the failure branch
        self.assertEqual(state.current.name, "Yard")
        checks = [line for line in state.history if line.startswith(("[CHECK]", "RESULT"))]
        for check, result in zip(checks[::2], checks[1::2]):
            self.assertEqual("passed" in check, "pouch" in result, (check, result))

    def test_a_failed_check_without_a_failure_branch_tells_no_success(self):
        pick = Action("Pick the lock", [Outcome("The door opens.", tags=["neutral"])],
                      skill_check={"stat": "stealth"}, source="location", owner="Gate")
        state = self._state(pick)
        with _rolls(False):
            _choose(state, "Pick the lock")
        self.assertNotIn("RESULT: The door opens.", state.history)
        self.assertIn("[FAILURE] Pick the lock failed.", state.history)


class TextConsistencyTests(_Rules):
    def _world(self):
        world = WorldGraph()
        for name, tags in (("Palace", ["palace"]), ("Vault", ["vault"]), ("Harbor", ["harbor"])):
            world.add_location(Location(name, tags))
        world.locations["Palace"].actions = [Action("Ask about the crown", [
            Outcome("She mentions it's hidden in the Vault.", tags=["spawn"], spawn=True)], source="location", owner="Palace")]
        world.allow_spawn("Palace", "Harbor")  # what the spawn scoring alone would open
        world.add_character(Character("Aurora", ["palace"], unique=True, known_locations=["Palace", "Harbor"], actions=[
            Action("(Aurora) Ask about the jewels", [Outcome("The jewels are kept in the Vault.", tags=["social"])],
                   character_name="Aurora", source="npc", owner="Aurora")]))
        return world

    def test_a_discovery_opens_the_place_its_text_names(self):
        state = GameState(self._world(), _genome(), start="Palace", rng=random.Random(0))
        _choose(state, "Ask about the crown")
        self.assertIn("Vault", state.world.locations["Palace"].connected)
        self.assertNotIn("Harbor", state.world.locations["Palace"].connected)
        with patch.dict(config._config["story_rules"], {"follow_outcome_text": False}):
            legacy = GameState(self._world(), _genome(), start="Palace", rng=random.Random(0))
            _choose(legacy, "Ask about the crown")
        self.assertIn("Harbor", legacy.world.locations["Palace"].connected)

    def test_an_npc_lead_follows_what_the_npc_said(self):
        state = GameState(self._world(), _genome(), start="Palace", rng=random.Random(0))
        _choose(state, "(Aurora) Ask about the jewels")
        self.assertIn("[LEAD] Aurora told you about the way to: Vault", state.history)
        self.assertFalse(any("way to: Harbor" in line for line in state.history))


class ProceduralEventTests(_Rules):
    def setUp(self):
        super().setUp()
        events = config._config["unexpected_events"]
        patcher = patch.dict(events, {"enabled": True, "objective_threshold": 0.0, "cooldown_steps": 0,
                                      "max_per_story": 6, "coin_budget": 10})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_effects_rotate_coins_have_a_budget_and_texts_do_not_repeat(self):
        state = GameState(_gate_world(), _genome(), start="Gate", rng=random.Random(0))
        coins, effects, texts = state.coins, [], []
        for step in range(6):
            state.actions_taken = step
            before = len(state.history)
            texts.append(state._maybe_trigger_unexpected_event())
            effects.append(next((line for line in state.history[before:] if line.startswith("[EVENT EFFECT]")), "none"))
        self.assertGreater(len(set(effects)), 2)                     # not the same windfall every time
        self.assertLessEqual(state.coins - coins, 10)                # the coin budget
        self.assertTrue(all(text.startswith("In the Gate, ") for text in texts))
        omens = [text.split(", ", 1)[1].split(". ")[0] for text in texts]
        self.assertEqual(len(set(omens)), len(omens))                # no omen twice while unused ones remain

    def test_the_shipped_frequency(self):
        self.assertEqual((config.default_config()["unexpected_events"]["max_per_story"],
                          config.default_config()["unexpected_events"]["cooldown_steps"]), (3, 4))


class PacingTests(_Rules):
    def _world(self):
        world = _gate_world(Action("Take the crown", [Outcome("You take it.", tags=["neutral"])],
                                   collects_object="Crown", source="location", owner="Gate"))
        from core_engine import Object
        world.add_object(Object("Crown", ["crown"]))
        world.add_mission(Mission("crown", {"item": "Crown"}))
        return world

    def _weight(self, state, name):
        return dict((a.name, w) for w, a in state._rank_actions(state.get_available_actions()))[name]

    def test_the_storyteller_holds_back_from_the_mission_in_the_opening(self):
        state = GameState(self._world(), _genome(goal_bias=1.0), start="Gate", rng=random.Random(0), mission="crown")
        held_back = self._weight(state, "Take the crown")
        with patch.dict(config._config["story_rules"], {"early_goal_avoidance": 0.0}):
            plain = self._weight(state, "Take the crown")
        self.assertLess(held_back, plain)
        state.actions_taken = config.get("mission_settings.pursuit_start_step")
        self.assertEqual(state._goal_restraint(), 0.0)               # gone once pursuit starts

    def test_a_stalled_traveler_moves_on(self):
        limit = config.get("narrative_end.max_consecutive_steps_in_location")
        state = GameState(_gate_world(), _genome(novelty_bias=0.0), start="Gate", rng=random.Random(0))
        for _ in range(limit):
            if state.current.name == "Gate":
                _choose(state, "Wait")
        self.assertFalse(state.ended)
        self.assertNotEqual(state.current.name, "Gate")
        self.assertTrue(any(line.startswith("[MOVE ON] After lingering in the Gate, you set out for the")
                            for line in state.history))
        with patch.dict(config._config["story_rules"], {"on_stall": "end"}):
            legacy = GameState(_gate_world(), _genome(novelty_bias=0.0), start="Gate", rng=random.Random(0))
            for _ in range(limit):
                _choose(legacy, "Wait")
        self.assertEqual(legacy.end_kind, "stalled")


class ClueTests(_Rules):
    def test_clues_are_recorded_paired_and_scored(self):
        self.assertEqual(rules.clue_pairs({"setup_1": 2}, {"payoff_2": 6}, "any")[0], [("setup_1", 2, "payoff_2", 6)])
        self.assertEqual(rules.clue_pairs({"setup_1": 2}, {"payoff_2": 6}, "numbered")[1:], (["setup_1"], ["payoff_2"]))
        world = _gate_world(
            Action("Read the note", [Outcome("A clue.", tags=["setup_1"])], source="location", owner="Gate"),
            Action("Solve it", [Outcome("It all fits.", tags=["payoff_2", "success"])], source="location", owner="Gate"),
        )
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        self.assertEqual(_fitness_breakdown(state)["setup_payoff"] if state.actions_taken else 0, 0)
        _choose(state, "Read the note")
        self.assertEqual(state._open_clues(), ["setup_1"])
        open_score = _fitness_breakdown(state)["setup_payoff"]
        weights = dict((a.name, w) for w, a in state._rank_actions(state.get_available_actions()))
        with patch.dict(config._config["story_rules"], {"payoff_weight": 0.0}):
            plain = dict((a.name, w) for w, a in state._rank_actions(state.get_available_actions()))
        self.assertGreater(weights["Solve it"], plain["Solve it"])   # an open clue pulls toward its payoff
        _choose(state, "Solve it")
        self.assertEqual(state._open_clues(), [])
        self.assertGreater(_fitness_breakdown(state)["setup_payoff"], open_score)  # this term used to stay 0


class RepetitionAndTieTests(_Rules):
    def test_going_straight_back_costs_more(self):
        state = GameState(_gate_world(), _genome(novelty_bias=0.8), start="Gate", rng=random.Random(0))
        _choose(state, "Move to Yard")
        back = next(a for a in state.get_available_actions() if a.owner == "Gate")
        with_penalty = dict((a.name, w) for w, a in state._rank_actions([back]))[back.name]
        with patch.dict(config._config["story_rules"], {"backtrack_penalty": 0.0}):
            without = dict((a.name, w) for w, a in state._rank_actions([back]))[back.name]
        self.assertAlmostEqual(without - with_penalty, 1.5 * 0.8)

    def test_near_ties_follow_story_logic_not_names(self):
        twins = [Action(name, [Outcome(f"{name}.", tags=["neutral"])], repeat="always", source="location", owner="Gate")
                 for name in ("Alpha", "Zeta")]
        state = GameState(_gate_world(*twins), _genome(novelty_bias=0.0), start="Gate", rng=random.Random(0))
        state.choice_counts[("action", "Zeta")] += 1
        order = [a.name for a in state.ranked_available_actions() if a.name in ("Alpha", "Zeta")]
        self.assertEqual(order, ["Alpha", "Zeta"])                   # the unused one first (by name: Zeta)
        firsts = set()
        for points in ({"attack": 20, "defence": 10, "stealth": 10, "persuasion": 10},
                       {"attack": 10, "defence": 20, "stealth": 10, "persuasion": 10},
                       {"attack": 10, "defence": 10, "stealth": 20, "persuasion": 10},
                       {"attack": 10, "defence": 10, "stealth": 10, "persuasion": 20}):
            other = GameState(_gate_world(*twins), _genome(novelty_bias=0.0), start="Gate", rng=random.Random(0), stats=points)
            firsts.add(next(a.name for a in other.ranked_available_actions() if a.name in ("Alpha", "Zeta")))
        self.assertEqual(firsts, {"Alpha", "Zeta"})                  # differs between stories, no dice rolled


class FitnessLossTests(_Rules):
    def _story(self, steps, won=False, ended=True, path=None, choices=None):
        state = GameState(_gate_world(), _genome(), start="Gate", rng=random.Random(0))
        state.actions_taken, state.won, state.ended = steps, won, ended
        state.end_kind = "mission" if won else ("action_limit" if ended else None)
        state.path = path or ["Gate"]
        state.tags_seen = [["urban", "threat", "success", "combat"]] * steps
        state.choice_counts = Counter(choices or {})
        return state

    def test_losses(self):
        breakdown = _fitness_breakdown(self._story(10, choices={("action", "Wait"): 5}, path=["Gate", "Yard", "Gate", "Yard"]))
        self.assertEqual(breakdown["repetition"], -(3 * 3 + 2 * 2))  # 3 uses beyond 2, 2 loops
        self.assertEqual(_fitness_breakdown(self._story(5, won=True))["pacing"], -(20 - 5) * 2)
        self.assertEqual(_fitness_breakdown(self._story(30, won=True))["pacing"], 0)
        self.assertLess(_fitness_breakdown(self._story(30, ended=False))["ending"],
                        _fitness_breakdown(self._story(30))["ending"])
        long, target = _fitness_breakdown(self._story(70)), config.get("fitness.story_length.target")
        with patch.dict(config._config["fitness"]["length_normalization"], {"enabled": False}):
            raw = _fitness_breakdown(self._story(70))
        self.assertAlmostEqual(long["dramatic_arc"], raw["dramatic_arc"] * target / 70)


class MethodologyTests(unittest.TestCase):
    def _evolution(self, **settings):
        return NarrativeEvolution(engine.build_world, fitness, settings={
            "pop_size": 2, "generations": 1, "episodes_per_genome": 3, "max_steps": 10, "validation_episodes": 4,
            "warm_start": False, **settings}, seed=3)

    def test_whole_stories_on_the_same_situations(self):
        evolution = self._evolution()
        self.assertEqual(evolution._max_steps(), 100)               # not the 10 steps asked for
        self.assertEqual(self._evolution(full_length_stories=False)._max_steps(), 10)
        situations = evolution._situations()
        world = engine.build_world(_genome())
        self.assertEqual({start for start, _ in situations}, {loc.name for loc in world.start_locations()})
        self.assertGreaterEqual(len(situations), 4 * 5)
        covered = {s for generation in range(len(situations)) for s in evolution._episode_situations(generation, 3)}
        self.assertEqual(covered, set(situations))                  # every situation is played over the generations
        first, second = evolution.benchmark_task({}), evolution.benchmark_task({"x": 1})
        self.assertEqual(first[3:5] + (first[5]["situations"],), second[3:5] + (second[5]["situations"],))
        self.assertIsNone(self._evolution(stratify_episodes=False)._situations())

    def test_consistency_aware_scores(self):
        evolution = self._evolution()
        task = evolution.benchmark_task(_genome().traits())
        mean_task = task[:5] + ({**task[5], "aggregate": "mean"},)
        self.assertLessEqual(evaluate_task(task), evaluate_task(mean_task))
        with self.assertRaises(ValueError):
            self._evolution(score_aggregate="median").run()

    def test_story_rules_are_validated(self):
        self.assertEqual(rules.validate_settings(rules.settings()), [])
        for key, value in (("on_stall", "nap"), ("repetition_power", 0.5), ("tie_epsilon", -1),
                           ("clue_matching", "some"), ("max_open_clues", 0), ("failure_tags", "failure")):
            with self.subTest(key=key):
                self.assertTrue(rules.validate_settings({**rules.DEFAULTS, key: value}))
        with patch.dict(config._config["story_rules"], {"on_stall": "nap"}), self.assertRaises(ValueError):
            engine.build_world(_genome())


class DirectorTests(_Rules):
    """The director: escalation, damage that grows, the finale and its epilogue, no options
    that can only repeat themselves, and options shown in the scene's order."""

    def _state(self, *actions):
        return GameState(_gate_world(*actions), _genome(), start="Gate", rng=random.Random(0))

    def test_escalation_and_damage(self):
        state = self._state()
        self.assertEqual(state.escalation(), 0.0)
        self.assertEqual(state._scaled_damage(2), 3)              # 2 x damage_scale 1.25, rounded
        state.actions_taken = 85                                   # 15 before the limit of 100
        self.assertAlmostEqual(state.escalation(), (0.85 - 0.3) / 0.7)
        self.assertEqual(state._scaled_damage(2), 4)              # ... x (1 + 0.6 x escalation)
        self.assertTrue(state.in_finale())
        with patch.dict(config._config["director"], {"enabled": False}):
            self.assertEqual((state.escalation(), state._scaled_damage(2), state.in_finale()), (0.0, 2, False))

    def test_the_finale_is_announced_and_the_story_ends_with_an_epilogue(self):
        state = self._state()
        state.actions_taken = 85
        state.step(0)
        self.assertIn("[FINALE] Time is running out: the story nears its end.", state.history)
        state.actions_taken = 99
        state.step(0)
        self.assertEqual(state.end_kind, "epilogue")
        self.assertTrue(state.end_reason.startswith("[EPILOGUE] The day draws to a close after 100 turns."))
        with patch.dict(config._config["director"], {"enabled": False}):
            plain = self._state()
            plain.actions_taken = 99
            plain.step(0)
            self.assertEqual(plain.end_kind, "action_limit")

    def test_an_action_that_can_only_repeat_itself_is_not_offered(self):
        pray = Action("Pray", [Outcome("Nothing answers.", tags=["neutral"])], repeat="always",
                      source="location", owner="Gate")
        state = self._state(pray)
        _choose(state, "Pray")
        self.assertNotIn("Pray", _names(state))                   # its only outcome was just told
        state.actions_taken += 6                                   # outside director.loop_window
        self.assertIn("Pray", _names(state))
        with patch.dict(config._config["director"], {"min_options": 10}):
            state.actions_taken -= 6
            self.assertIn("Pray", _names(state))                   # never fewer options than min_options

    def test_players_see_the_best_options_in_the_scene_order(self):
        actions = [Action(name, [Outcome(f"{name} done.", tags=tags)], repeat="always", source="location", owner="Gate")
                   for name, tags in (("Alpha", ["neutral"]), ("Beta", ["spawn"]), ("Gamma", ["threat"]))]
        state = self._state(*actions)
        ranked = [a.name for a in state.ranked_available_actions(limit=3)]
        shown = [a.name for a in state.ranked_available_actions(limit=3, order="world")]
        scene = [a.name for a in state.get_available_actions()]
        self.assertEqual(sorted(shown), sorted(ranked))           # the same best three...
        self.assertEqual(shown, sorted(shown, key=scene.index))   # ...in the scene's order

    def test_failures_cost_more_when_dangerous(self):
        state = self._state()
        self.assertEqual(state.combat.failure_penalty(False), 1)
        self.assertEqual(state.combat.failure_penalty(True), 2)


class MissionHistoryTests(unittest.TestCase):
    def test_a_profiles_recent_missions_are_drawn_less_often(self):
        import tempfile

        from core_engine import variables as persistent

        world = engine.build_world(_genome())
        state = GameState(world, _genome(), rng=random.Random(0))
        pool = sorted(world.missions.values(), key=lambda m: m.name)
        state.recent_missions = [pool[0].name, pool[0].name, pool[1].name]
        weights = dict(zip((m.name for m in pool), state._mission_weights(pool)))
        self.assertAlmostEqual(weights[pool[0].name], pool[0].weight * 0.3 ** 2)
        self.assertAlmostEqual(weights[pool[1].name], pool[1].weight * 0.3)
        self.assertEqual(weights[pool[2].name], pool[2].weight)
        with tempfile.TemporaryDirectory() as directory:
            store = persistent.VariableStore(path=f"{directory}/values.json")
            played = GameState(engine.build_world(_genome()), _genome(), rng=random.Random(1), store=store, profile="ann")
            self.assertEqual(store.recent_missions("ann"), [played.mission.name])
            self.assertEqual(store.profile("ann")["missions"], [played.mission.name])


class ShippedContentTests(unittest.TestCase):
    """The content fixes found by playing the game: names, placement and mission balance."""

    @staticmethod
    def _world():
        return config.as_dict()["world_definition"]

    def test_item_placeholders_name_the_item(self):
        world = WorldGraph()
        world.add_location(Location("Gate", ["gate"]))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        from core_engine import Object

        state.world.objects.append(Object("Vintage Wine", ["alcohol"], descriptions=["The labeling is fine."]))
        state.inventory.add("Vintage Wine")
        give = Action("Give him the {alcohol_desc}", [Outcome("He drinks.", tags=["neutral"])], required_tag="alcohol")
        self.assertEqual(state._prepare_action(give).name, "Give him the Vintage Wine")

    def test_no_bandits_or_bribes_inside_the_vault(self):
        npcs = {npc["name"]: npc for npc in self._world()["characters"]}
        self.assertNotIn("Vault", npcs["Bandit"]["known_locations"])
        bribe = next(a for a in npcs["Gate Guard"]["actions"] if a.get("id") == "gate_guard_give_drink")
        self.assertEqual(bribe["enabled_if"], {"not": {"at_location": "Vault"}})

    def test_missions_are_balanced(self):
        missions = self._world()["missions"]
        self.assertEqual(missions["break_into_the_vault"]["goal"], {"action": "Crack the main safe"})
        thief = self._world()["events"]["confront_thief"]
        failures = [o for a in thief["actions"] for o in a["outcomes"] if "failure" in o.get("tags", [])]
        self.assertTrue(failures and not any(o.get("remove_npc") for o in failures))  # a failure keeps the thief


if __name__ == "__main__":
    unittest.main()
