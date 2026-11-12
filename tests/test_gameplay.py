"""Gameplay rules: a bidirectional location graph (backtracking, locked exits, player-directed
navigation), scenario templates without exact duplicates, objective losses in the fitness,
explicit checks, NPC goals, resolvable encounters, Power in fights, item labels and setup/payoff
gating."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import pickle
import random
import unittest
from unittest.mock import patch

from core_engine import Action, Character, Event, GameState, Genome, Location, Object, Outcome, WorldGraph
from core_engine.augmenter import WorldAugmenter
from core_engine.builder import expand_templates, logic_errors
from core_engine.fitness import _fitness_breakdown, objective_losses
from utils.config_loader import config


def _genome(**traits):
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    for name, value in traits.items():
        setattr(genome, name, value)
    return genome


def _loc(name, *actions, **kwargs):
    location = Location(name, kwargs.pop("tags", [name.lower()]), **kwargs)
    location.actions = list(actions)
    return location


def _act(name, owner, *outcomes, **kwargs):
    return Action(name, list(outcomes), source=kwargs.pop("source", "location"), owner=owner, **kwargs)


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class _Quiet(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.dict(config._config["unexpected_events"], {"enabled": False}),
                        patch.dict(config._config["game_state"], {"auto_travel_on_spawn": True})):
            patcher.start()
            self.addCleanup(patcher.stop)


class GraphTests(_Quiet):
    def _world(self):
        world = WorldGraph()
        world.add_location(_loc("A", _act("Find a trail", "A", Outcome("A trail appears.", tags=["spawn"],
                                                                       spawn=True, reveal_location="B")),
                                _act("Wait", "A", Outcome("You wait."), repeat="always")))
        world.add_location(_loc("B", _act("Rest", "B", Outcome("You rest."), repeat="always")))
        world.add_location(_loc("C", _act("Rest", "C", Outcome("You rest."), repeat="always")))
        return world

    def test_a_discovery_connects_both_ways_and_the_player_chooses_to_go(self):
        world = self._world()
        state = GameState(world, _genome(voyage_bias=1.0), start="A", rng=random.Random(0))
        state.player_directed = True  # real play (navigation.player_directed: play)
        _choose(state, "Find a trail")
        self.assertEqual(state.current.name, "A")  # not carried off
        self.assertIn("B", world.locations["A"].connected)
        self.assertIn("A", world.locations["B"].connected)
        self.assertIn("Move to B", _names(state))
        _choose(state, "Move to B")
        self.assertIn("Return to A", _names(state))  # backtracking
        self.assertEqual(state.transition_records[0]["resolution"], "named")
        self.assertEqual(state.transition_records[-1]["resolution"], "travel")

    def test_real_play_is_player_directed_simulations_are_not(self):
        from core_engine.variables import VariableStore
        import tempfile, os

        with patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-saves-tests-")}):
            played = GameState(self._world(), _genome(), start="A", rng=random.Random(0), store=VariableStore())
        simulated = GameState(self._world(), _genome(), start="A", rng=random.Random(0))
        self.assertTrue(played.player_directed)
        self.assertFalse(simulated.player_directed)

    def test_no_place_leads_to_itself_and_names_are_unique(self):
        world = self._world()
        self.assertFalse(world.connect("A", "A"))
        self.assertTrue(world.connect("A", "C"))
        with self.assertRaises(ValueError):
            world.add_location(Location(" a ", ["dup"]))
        errors = logic_errors({"locations": {"Market": {"tags": ["x"]}, "market": {"tags": ["y"]}}})
        self.assertTrue(any("same name" in e for e in errors))

    def test_the_way_back_stays_among_the_players_choices(self):
        world = WorldGraph()
        hub = _loc("Hub", *[_act(f"Chore {n}", "Hub", Outcome(f"Chore {n}."), repeat="always") for n in range(8)])
        world.add_location(hub)
        for name in ("Gate", "North", "South", "East"):
            world.add_location(_loc(name, _act(f"Rest in {name}", name, Outcome("Rest."), repeat="always")))
            hub.connect(world.locations[name])
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        state._move_to("Hub")
        self.assertIn("Return to Gate", [a.name for a in state.player_options()])

    def test_the_known_map_is_kept_with_the_game(self):
        world = self._world()
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        _choose(state, "Find a trail")
        known = state.map_view()
        self.assertEqual(known["paths"], [["A", "B"]])
        self.assertEqual({p["name"] for p in known["places"]}, {"A", "B"})
        self.assertEqual(pickle.loads(pickle.dumps(state)).map_view(), known)


class LockedExitTests(_Quiet):
    def test_a_locked_exit_needs_an_explicit_unlock(self):
        world = WorldGraph()
        world.add_location(_loc("Hall", _act("Wait", "Hall", Outcome("You wait."), repeat="always")))
        world.add_location(_loc("Vault", _act("Work the lock", "Vault", Outcome("Click.", unlock_exit=True)),
                                _act("Count the gold", "Vault", Outcome("So much gold."), repeat="always"),
                                exit_locked=True))
        world.connect("Hall", "Vault")
        state = GameState(world, _genome(), start="Hall", rng=random.Random(0))
        _choose(state, "Move to Vault")
        self.assertTrue(state._location_is_locked())
        self.assertNotIn("Return to Hall", _names(state))
        self.assertIn("Work the lock", _names(state))
        _choose(state, "Work the lock")
        self.assertIn("[UNLOCKED] The way out of the Vault is open now.", state.history)
        self.assertIn("Return to Hall", _names(state))
        self.assertNotIn("Work the lock", _names(state))  # it is done


class TemplateTests(unittest.TestCase):
    TEMPLATES = {"district_market": {
        "tags": ["urban", "trade", "{district}"],
        "entered_descriptions": ["The {district} market of the city: {name}."],
        "actions": [{"name": "Haggle in the {district} stalls", "outcomes": [{"desc": "A bargain.", "tags": ["trade"]}]}],
    }}

    def test_variants_are_filled_in(self):
        locations, errors = expand_templates({"scenario_templates": self.TEMPLATES, "locations": {
            "North Market": {"template": "district_market", "parameters": {"district": "north"}},
            "South Market": {"template": "district_market", "parameters": {"district": "south"}, "start_eligible": True},
        }})
        self.assertEqual(errors, [])
        self.assertEqual(locations["North Market"]["tags"], ["urban", "trade", "north"])
        self.assertEqual(locations["South Market"]["entered_descriptions"], ["The south market of the city: South Market."])
        self.assertEqual(locations["South Market"]["actions"][0]["name"], "Haggle in the south stalls")
        self.assertTrue(locations["South Market"]["start_eligible"])  # its own fields stay

    def test_exact_scenario_duplicates_are_refused(self):
        world = {"scenario_templates": self.TEMPLATES, "locations": {
            "North Market": {"template": "district_market", "parameters": {"district": "north"}},
            "North Market Copy": {"template": "district_market", "parameters": {"district": "north"}},
            "Shrine": {"tags": ["holy"], "actions": [{"name": "Pray", "outcomes": [{"desc": "Peace."}]}]},
            "Second Shrine": {"tags": ["holy"], "actions": [{"name": "Pray", "outcomes": [{"desc": "Peace."}]}]},
            "Bazaar": {"template": "district_market", "parameters": {}},
            "Nowhere": {"template": "no_such_template"},
        }}
        errors = logic_errors(world)
        self.assertTrue(any("'North Market' and 'North Market Copy' are the same scenario" in e for e in errors))
        self.assertTrue(any("'Shrine' and 'Second Shrine' are the same scenario (the same content)" in e for e in errors))
        self.assertTrue(any("needs the parameter 'district'" in e for e in errors))
        self.assertTrue(any("no scenario template 'no_such_template'" in e for e in errors))

    def test_generated_duplicates_are_skipped(self):
        existing = {"North Market": {"template_id": "district_market", "parameters": {"district": "north"}}}
        generated = {
            "north market": {"template_id": "x", "parameters": {}},                                   # same name
            "Copy": {"template_id": "district_market", "parameters": {"district": "north"}},           # same scenario
            "South Market": {"template_id": "district_market", "parameters": {"district": "south"}},   # a variant
        }
        self.assertEqual(list(WorldAugmenter._new_scenarios(existing, generated)), ["South Market"])


class ObjectiveLossTests(_Quiet):
    def test_tag_led_discovery_and_duplicate_scenarios(self):
        world = WorldGraph()
        world.add_location(_loc("A", tags=["urban"]))
        world.add_location(_loc("Forest", tags=["nature", "secluded"]))
        world.add_location(_loc("Guard Post", tags=["urban", "guarded"]))
        world.allow_spawn("A", "Guard Post")
        world.allow_spawn("A", "Forest")  # both border A: the tags choose between them
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        state.apply(Outcome("A woodland trail appears.", spawn=True, target_tags=["nature", "secluded"]))
        self.assertIn("Forest", world.locations["A"].connected)
        self.assertNotIn("Guard Post", world.locations["A"].connected)
        self.assertEqual(state.transition_records[0]["resolution"], "tags")

        beat = _act("Repeatable beat", "A", Outcome("Again.", tags=["social"]), repeat="always")
        state.actions_taken = 2
        state.apply(beat.outcomes[0], action=beat)
        before = objective_losses(state)["scenario_duplication"]
        state.apply(beat.outcomes[0], action=beat)
        self.assertGreater(objective_losses(state)["scenario_duplication"], before)
        breakdown = _fitness_breakdown(state)
        self.assertTrue({"loss_transition_quality", "loss_tag_coverage", "loss_named_transition_dependence",
                         "loss_scenario_duplication", "loss_world_extension"} <= set(breakdown))
        self.assertTrue(all(value <= 0 for key, value in breakdown.items() if key.startswith("loss_")))

    def test_named_transitions_cost_when_they_are_all_there_is(self):
        world = WorldGraph()
        for name in ("A", "B", "C"):
            world.add_location(_loc(name))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        state.actions_taken = 5
        for resolution in ("named", "named", "named", "tags"):
            state._record_transition("discovery", "A", "B", resolution)
        named = objective_losses(state)["named_transition_dependence"]
        state.transition_records = [r | {"resolution": "tags"} for r in state.transition_records]
        self.assertGreater(named, objective_losses(state)["named_transition_dependence"])


class CheckTests(_Quiet):
    def test_an_unchecked_outcome_does_not_depend_on_the_storyteller(self):
        world = WorldGraph()
        world.add_location(_loc("A"))
        outcome = Outcome("Done.", tags=["threat"])
        for bias in (0.0, 1.0):
            state = GameState(world, _genome(success_bias=bias, threat_prob=bias), start="A", rng=random.Random(0))
            self.assertEqual(outcome.success_probability(state.genome, state),
                             config.get("outcome_resolution.probability_ceiling"))

    def test_a_check_is_shown_before_choosing(self):
        world = WorldGraph()
        world.add_location(_loc("A", _act("Pick the lock", "A", Outcome("Open."), skill_check={"stat": "stealth"}),
                                _act("Wait", "A", Outcome("You wait."))))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        actions = {a.name: a for a in state.ranked_available_actions()}
        hint = state.check_hint(actions["Pick the lock"])
        self.assertEqual(hint["stat"], "stealth")
        self.assertTrue(0 < hint["chance"] <= 100)
        self.assertIsNone(state.check_hint(actions["Wait"]))


class NPCGoalTests(_Quiet):
    def _world(self):
        world = WorldGraph()
        world.add_location(_loc("Gate", _act("Wait", "Gate", Outcome("You wait."), repeat="always")))
        world.add_location(_loc("Barracks"))
        world.add_location(_loc("Armory"))
        world.routes = {"Gate": {"Barracks", "Armory"}, "Barracks": {"Gate"}, "Armory": {"Gate"}}
        bribe = Action("(Guard) Bribe him", [Outcome("He pockets the coins.", tags=["neutral"])],
                       character_name="Guard", source="npc", owner="Guard", repeat="always")
        insult = Action("(Guard) Mock his helmet", [Outcome("He glares.", tags=["neutral"])],
                        character_name="Guard", source="npc", owner="Guard", repeat="always")
        world.add_character(Character("Guard", ["gate"], goals=["Collect bribes"], known_locations=["Armory"],
                                      actions=[bribe, insult]))
        return world, bribe, insult

    def test_helping_someone_twice_makes_an_ally_who_shows_the_way(self):
        world, bribe, insult = self._world()
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        state.player_directed = True  # real play: nobody walks the player off by themselves
        state.apply(insult.outcomes[0], action=insult)
        self.assertEqual(state.npc_goal_progress.get("Guard", {}), {})  # nothing to do with his goal
        state.apply(Outcome("He refuses.", tags=["failure"]), action=bribe)
        self.assertEqual(state.npc_goal_progress.get("Guard", {}), {})  # a failure does not help
        state.apply(bribe.outcomes[0], action=bribe)
        self.assertEqual(state.npc_goal_progress["Guard"], {0: 1})
        state.apply(bribe.outcomes[0], action=bribe)
        self.assertIn("Guard", state.allies)
        self.assertTrue(any(line.startswith("[ALLY] You helped a Guard collect bribes") for line in state.history))
        self.assertEqual(state.current.name, "Gate")
        self.assertIn("Armory", world.locations["Gate"].connected)  # the reward: the way to a place he knows
        state.actions_taken = 3  # a story that has begun (a story of no turns scores no terms)
        self.assertGreater(_fitness_breakdown(state)["npc_goals"], 0)

    def test_an_ally_makes_their_checks_easier(self):
        world, bribe, _ = self._world()
        bribe.skill_check = {"stat": "persuasion"}
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        before = state.check_hint(bribe)["chance"]
        state.allies.add("Guard")
        self.assertGreater(state.check_hint(bribe)["chance"], before)


class EncounterTests(_Quiet):
    def test_a_mandatory_encounter_must_be_resolvable(self):
        errors = logic_errors({"locations": {"A": {}}, "events": {"empty": {"title": "Empty"}},
                               "characters": [{"name": "Bandit", "encounter_event": "ambush"},
                                              {"name": "Thug", "encounter_event": "empty"}]})
        self.assertTrue(any("no event 'ambush'" in e for e in errors))
        self.assertTrue(any("has no actions to resolve it" in e for e in errors))

    def test_fleeing_takes_you_back_the_way_you_came(self):
        world = WorldGraph()
        world.add_location(_loc("Road"))
        world.add_location(_loc("Lane"))
        world.connect("Road", "Lane")
        state = GameState(world, _genome(), start="Road", rng=random.Random(0))
        state._move_to("Lane")
        flee = _act("Make a run for it", "Lane", Outcome("You run.", tags=["success"], return_to_previous=True),
                    source="event")
        state.apply(flee.outcomes[0], action=flee)
        self.assertEqual(state.current.name, "Road")
        self.assertIn("[BACK] You fall back the way you came, to the Road.", state.history)
        self.assertEqual(state.transition_records[-1]["resolution"], "back")


class PowerAndItemTests(_Quiet):
    def test_a_weapon_raises_power_and_eases_fights_only(self):
        world = WorldGraph()
        world.add_location(_loc("A"))
        world.add_object(Object("Sword", ["weapon"], stat_bonus={"attack": 25}))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        fight = _act("Fight", "A", Outcome("Clash.", tags=["combat"]), skill_check={"stat": "attack"})
        sneak = _act("Sneak", "A", Outcome("Quiet."), skill_check={"stat": "stealth"})
        before, before_power = state.check_hint(fight)["chance"], state.power
        sneak_before = state.check_hint(sneak)["chance"]
        state.inventory.add("Sword")
        state.update_power()
        self.assertGreater(state.power, before_power)
        self.assertGreater(state.check_hint(fight)["bonus"], 0)
        self.assertGreater(state.check_hint(fight)["chance"], before)
        self.assertEqual(state.check_hint(sneak)["chance"], sneak_before)  # not a fight
        state._change_health(-6)
        state.update_power()
        self.assertLess(state.check_hint(fight)["bonus"], 0)  # wounds hurt in a fight

    def test_an_item_template_uses_the_items_name(self):
        world = WorldGraph()
        world.add_location(_loc("A"))
        world.add_object(Object("Vintage Wine", ["alcohol"], descriptions=["The labeling is in a prestigious script."]))
        give = Action("Give him the {alcohol_desc}", [Outcome("Accepted.")], character_name="Guard",
                      required_tag="alcohol", source="npc", owner="Guard")
        world.add_character(Character("Guard", ["a"], known_locations=["A"], actions=[give]))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        state.inventory.add("Vintage Wine")
        state.ranked_available_actions()
        state.visible_inhabitants.add("Guard")
        names = _names(state)
        self.assertIn("Give him the Vintage Wine", names)
        self.assertFalse(any("prestigious" in name for name in names))


class SetupPayoffTests(_Quiet):
    def test_a_payoff_waits_for_its_setup(self):
        world = WorldGraph()
        payoff = Outcome("The clue pays off.", requires_setup="palace_route", payoff_id="palace_route")
        world.add_location(_loc("A", _act("Use the clue", "A", payoff, repeat="always"),
                                _act("Look around", "A", Outcome("A clue!", setup_id="palace_route"))))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        self.assertNotIn("Use the clue", _names(state))
        _choose(state, "Look around")
        self.assertIn("Use the clue", _names(state))
        _choose(state, "Use the clue")
        self.assertIn("palace_route", state.completed_payoffs)
        self.assertNotIn("Use the clue", _names(state))  # paid off: not again
        errors = logic_errors({"locations": {"A": {"actions": [{"name": "X", "outcomes": [
            {"desc": "Pays.", "requires_setup": "never_planted"}]}]}}})
        self.assertTrue(any("no outcome plants the setup 'never_planted'" in e for e in errors))

    def test_a_payoff_clue_needs_an_open_clue(self):
        world = WorldGraph()
        world.add_location(_loc("A", _act("Reveal the truth", "A", Outcome("It all fits.", tags=["payoff_clue"]),
                                          repeat="always"),
                                _act("Overhear", "A", Outcome("A rumour.", tags=["setup_1"]))))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        self.assertNotIn("Reveal the truth", _names(state))
        _choose(state, "Overhear")
        self.assertIn("Reveal the truth", _names(state))
        with patch.dict(config._config["story_rules"], {"gate_payoffs": False}):
            fresh = GameState(world, _genome(), start="A", rng=random.Random(0))
            self.assertIn("Reveal the truth", _names(fresh))


class StoryRhythmTests(_Quiet):
    """Alternative, unpredictable stories: the storyteller's surprises, the director's
    rhythm (boredom, due payoffs, exploration) and the fitness terms that reward them."""

    def _hall(self):
        world = WorldGraph()
        world.add_location(_loc("Hall", *[_act(f"Chore {n}", "Hall", Outcome("Same as ever."), repeat="always")
                                          for n in range(4)],
                                _act("Open the old door", "Hall", Outcome("A cellar!", tags=["discovery"],
                                                                          spawn=True), repeat="always")))
        world.add_location(_loc("Cellar", _act("Rest", "Cellar", Outcome("Rest."), repeat="always")))
        world.allow_spawn("Hall", "Cellar")
        return world

    def test_surprises_come_from_the_storys_own_dice(self):
        def story(seed, surprise):
            with patch.dict(config._config["director"], {"surprise": surprise, "surprise_margin": 50}):
                state = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(seed))
                for _ in range(12):
                    if not state.step():
                        break
                return [t["action"] for t in state.turn_log]

        self.assertEqual(story(1, 0.9), story(1, 0.9))           # reproducible from its seed
        self.assertNotEqual(story(1, 0.0), story(1, 0.9))        # but not the favourite every time
        self.assertNotEqual(story(1, 0.9), story(2, 0.9))        # another seed, another story

    def test_after_calm_turns_something_new_pulls_harder(self):
        state = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        state.outcome_counts["Same as ever."] = 3  # the chores were done before: nothing new in them
        actions = {a.name: a for a in state.ranked_available_actions()}
        scores = lambda: {a.name: score for score, a in state._rank_actions(list(actions.values()))}
        before = scores()
        state.calm_streak = 6
        self.assertGreater(state._boredom(), 0)
        after = scores()
        self.assertGreater(after["Open the old door"] - before["Open the old door"], 0)
        self.assertEqual(after["Chore 0"], before["Chore 0"])  # more of the same gains nothing

    def test_a_clue_left_open_wants_its_payoff(self):
        world = WorldGraph()
        world.add_location(_loc("A", _act("Reveal it", "A", Outcome("It fits.", tags=["payoff_clue"]), repeat="always")))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        state.setup_indices["setup_1"] = 0
        action = state.ranked_available_actions()[0]
        fresh = state._rank_actions([action])[0][0]
        state.actions_taken = 10  # the clue is 10 turns old: due
        self.assertEqual(state._due_clues(), ["setup_1"])
        self.assertGreater(state._rank_actions([action])[0][0], fresh)

    def test_the_rhythm_terms(self):
        from core_engine.fitness import _rhythm

        cfg = config.get("fitness.rhythm")
        state = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        calm = {"action": "Chore 0", "source": "location", "owner": "Hall", "location_before": "Hall",
                "location_after": "Hall", "outcome": "Same as ever.", "health": 10, "coins": 0, "check": None,
                "succeeded": True, "event": None, "event_after": None, "tags": []}
        state.turn_log = [dict(calm, turn=n) for n in range(1, 9)]  # the same, over and over
        state.actions_taken = 8
        flat = _rhythm(state, cfg)
        self.assertLess(flat["predictability"], 0)
        self.assertEqual(flat["exploration"], 0)
        lively = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        _choose(lively, "Open the old door")
        _choose(lively, "Move to Cellar")
        terms = _rhythm(lively, cfg)
        self.assertGreater(terms["exploration"], 0)
        self.assertGreater(terms["variety_of_play"], flat["variety_of_play"])

    def test_telling_different_stories_is_rewarded(self):
        from core_engine.evolution import evaluate_task
        from core_engine.fitness import alternatives
        import main as engine

        one = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        _choose(one, "Chore 0")
        same = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        _choose(same, "Chore 0")
        other = GameState(self._hall(), _genome(), start="Hall", rng=random.Random(0))
        _choose(other, "Open the old door")
        _choose(other, "Move to Cellar")
        self.assertEqual(alternatives([one, same]), 0.0)
        self.assertGreater(alternatives([one, other]), 0.3)

        stories = {1: one, 2: other}
        engine_fitness = lambda state: 0.0  # noqa: E731
        engine_fitness.rewards_alternatives = True  # as the engine's own fitness
        task = (engine.build_world, engine_fitness, _genome().traits(), [1, 2], 6)
        with patch("core_engine.evolution.play_episode", lambda build, genome, seed, steps, situation=None: stories[seed]):
            plain = evaluate_task(task + ({"alternatives_weight": 0},))
            rewarded = evaluate_task(task + ({"alternatives_weight": 40},))
        self.assertAlmostEqual(rewarded - plain, 40 * alternatives([one, other]))
        custom = evaluate_task((engine.build_world, lambda state: 0.0, _genome().traits(), [1, 2], 6,
                                {"alternatives_weight": 40}))
        self.assertEqual(custom, 0.0)  # a caller's own fitness is left as it is


class StoryCraftTests(_Quiet):
    """Twists that keep their word, payoffs that call back, choices with a price, a quest
    that keeps the story moving, and no aimless back and forth."""

    def _gate(self):
        world = WorldGraph()
        world.add_location(_loc("Gate", _act("Wait", "Gate", Outcome("You wait."), repeat="always")))
        world.add_location(_loc("Yard", _act("Rest", "Yard", Outcome("You rest."), repeat="always")))
        world.connect("Gate", "Yard")
        return world

    def _twists(self, effects, **extra):
        cfg = {"enabled": True, "objective_threshold": 0.0, "cooldown_steps": 0, "max_per_story": 6,
               "coin_budget": 50, "twist_payoff_delay": 2, "effects": effects, **extra}
        patcher = patch.dict(config._config["unexpected_events"], cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_twist_matches_what_it_does_and_a_mystery_pays_off_later(self):
        self._twists([{"tags": ["unexpected", "social"], "twists": [{
            "text": "whispers that someone has followed you since the {place}",
            "payoff": "The one who followed you since the {place} strikes at last.",
            "payoff_effect": {"health_change": -1, "tags": ["threat"]}}]}])
        state = GameState(self._gate(), _genome(), start="Gate", rng=random.Random(0))
        told = state._maybe_trigger_unexpected_event()
        self.assertIn("whispers that someone has followed you since the Gate", told)
        self.assertEqual(len(state._open_clues()), 1)                    # a thread is open
        state.actions_taken = 1
        self.assertIsNone(state._due_twist(config.get("unexpected_events")))  # not yet due
        state.actions_taken = 2
        health = state.health
        state._maybe_trigger_unexpected_event()
        self.assertIn("[TWIST] The one who followed you since the Gate strikes at last.", state.history)
        self.assertLess(state.health, health)
        self.assertEqual(state._open_clues(), [])                        # and closed

    def test_a_bargain_is_a_choice_with_a_price_later(self):
        self._twists([{"tags": ["unexpected", "social"], "twists": [{"text": "offers a dangerous bargain"}],
                       "bargain": {"accept": {"name": "Take it", "desc": "You shake on it with {actor}.",
                                              "coin_change": 12, "payoff": "{actor_cap} comes to collect.",
                                              "payoff_effect": {"coin_change": -10}},
                                   "refuse": {"name": "Refuse it", "desc": "You walk away."}}}])
        state = GameState(self._gate(), _genome(), start="Gate", rng=random.Random(0))
        state._maybe_trigger_unexpected_event()
        state._start_pending_event()
        self.assertEqual(sorted(_names(state)), ["Refuse it", "Take it"])
        coins = state.coins
        _choose(state, "Take it")
        self.assertEqual(state.coins, coins + 12)
        state.actions_taken += 3
        state._maybe_trigger_unexpected_event()
        self.assertTrue(any(line.startswith("[TWIST]") and "comes to collect" in line for line in state.history))
        self.assertEqual(state.coins, coins + 2)

        refused = GameState(self._gate(), _genome(), start="Gate", rng=random.Random(0))
        refused._maybe_trigger_unexpected_event()
        refused._start_pending_event()
        _choose(refused, "Refuse it")
        refused.actions_taken += 5
        refused._maybe_trigger_unexpected_event()
        self.assertFalse(any(line.startswith("[TWIST]") for line in refused.history))  # nothing owed

    def test_a_payoff_calls_back_to_the_setup_it_belongs_with(self):
        world = self._gate()
        world.locations["Gate"].actions += [
            _act("Overhear", "Gate", Outcome("A sailor swears the crown was smuggled out.", tags=["setup_1"])),
            _act("Open the chest", "Gate", Outcome("The smuggled crown glints inside.", tags=["payoff_clue"])),
            _act("Pick a lock", "Gate", Outcome("The door swings open.", tags=["payoff_clue"]))]
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        _choose(state, "Overhear")
        state._move_to("Yard")
        state.actions_taken += 1  # a later turn
        state.apply(Outcome("The smuggled crown glints inside.", tags=["payoff_clue"]))
        self.assertTrue(any(line.startswith("[CALLBACK]") and "smuggled out" in line for line in state.history))
        unrelated = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        _choose(unrelated, "Overhear")
        unrelated._move_to("Yard")
        unrelated.actions_taken += 1
        unrelated.apply(Outcome("The door swings open.", tags=["payoff_clue"]))
        self.assertEqual(unrelated._open_clues(), [])  # the clue is closed all the same
        self.assertFalse(any(line.startswith("[CALLBACK]") for line in unrelated.history))  # not tied together

    def test_someone_already_in_view_is_not_noticed_again(self):
        world = self._gate()
        world.add_character(Character("Sentry", ["gate"], known_locations=["Gate"], actions=[]))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        state.ranked_available_actions()
        state._reveal_npc("Sentry")
        state._reveal_npc("Sentry")
        self.assertLessEqual(sum(1 for line in state.history if line.startswith("[DISCOVERY] You noticed Sentry")), 1)

    def test_no_aimless_return_to_a_place_with_nothing_left(self):
        world = self._gate()
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        state.outcome_counts["You rest."] = 2      # the Yard holds nothing new...
        state.location_counts["Yard"] = 1          # ...and was visited
        state.current.connect(world.locations["Yard"])
        travel = next(a for a in state.ranked_available_actions() if a.owner == "Yard")
        self.assertFalse(state._place_promise("Yard"))
        with patch.dict(config._config["director"], {"dead_end_penalty": 0.0}):
            free = state._rank_actions([travel])[0][0]
        self.assertLess(state._rank_actions([travel])[0][0], free)

    def test_an_idle_quest_pulls_harder(self):
        from core_engine import Mission

        world = self._gate()
        world.add_mission(Mission("go", {"location": "Yard"}, title="Go to the yard"))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0), mission="go")
        state.actions_taken = 15
        with patch.dict(config._config["mission_settings"], {"pursuit_start_step": 50, "pursuit_full_step": 90,
                                                              "momentum_after": 10}):
            self.assertGreater(state._goal_drive(), 0)          # idle 15 turns: momentum
            state.last_quest_progress = 14
            self.assertEqual(state._goal_drive(), 0)            # it just moved

    def test_each_turn_keeps_the_alternatives(self):
        state = GameState(self._gate(), _genome(), start="Gate", rng=random.Random(0))
        state.current.connect(state.world.locations["Yard"])
        state.step()
        turn = state.turn_log[-1]
        self.assertNotIn(turn["action"], turn["options"])
        self.assertTrue(turn["options"])

    def test_the_vault_alarm_brings_the_guards(self):
        import main as engine

        config.reload()
        genome = _genome()
        world = engine.build_world(genome)
        state = GameState(world, genome, start="Harbor", rng=random.Random(0))
        safe = next(a for a in world.locations["Vault"].actions if a.name == "Crack the main safe")
        alarm = next(o for o in safe.outcomes if "alarm" in o.desc)
        state.apply(alarm, action=safe)
        state._start_pending_event()
        self.assertEqual(state.active_event.name, "guards_alerted")
        self.assertIn("Slip down the drain into the Sewers", _names(state))


if __name__ == "__main__":
    unittest.main()
