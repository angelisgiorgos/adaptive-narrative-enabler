"""One-time and exclusive options, customisable story text, and genome-driven choices."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
from backend import content, sessions, store
import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Outcome, WorldGraph
from core_engine.narration import DEFAULT_MESSAGES, Narrator, validate_messages
from utils.config_loader import config


def _genome():
    random.seed(33)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _npc_action(name, group=None, repeat=None, succeeds=True, npc="Guard"):
    return Action(
        f"({npc}) {name}", [Outcome(f"{name}!", success_prob=1.0 if succeeds else 0.0)],
        character_name=npc, exclusive_group=group, repeat=repeat, source="npc", owner=npc,
    )


def _world(unique=False, **actions):
    world = WorldGraph()
    for name in ("Gate", "Yard"):
        location = Location(name, ["gate"])
        location.actions = [Action(f"Wait at the {name}", [Outcome("You wait.")], source="location", owner=name)]
        world.add_location(location)
    world.locations["Gate"].connect(world.locations["Yard"])
    world.add_character(Character("Guard", ["gate"], unique=unique, actions=[
        _npc_action("Bribe with wine", group="get_past"),
        _npc_action("Bribe with coins", group="get_past"),
        _npc_action("Fight", group="get_past", succeeds=actions.get("fight_succeeds", True)),
        _npc_action("Salute", repeat="always"),
        _npc_action("Ask the time", repeat="once"),
        _npc_action("Chat about the weather"),  # per_visit (default for NPCs)
    ]))
    return world


def _state(**kwargs):
    return GameState(_world(**kwargs), _genome(), start="Gate", rng=random.Random(1))


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class RepeatAndExclusiveTests(unittest.TestCase):
    def test_a_successful_choice_removes_the_other_options(self):
        state = _state()
        _choose(state, "(Guard) Bribe with wine")
        names = _names(state)
        for gone in ("(Guard) Bribe with wine", "(Guard) Bribe with coins", "(Guard) Fight"):
            self.assertNotIn(gone, names)
        _choose(state, "Move to Yard")
        _choose(state, "Return to Gate")
        self.assertNotIn("(Guard) Bribe with coins", _names(state))  # the choice stays made

    def test_a_failed_option_keeps_the_others(self):
        state = _state(fight_succeeds=False)
        _choose(state, "(Guard) Fight")
        names = _names(state)
        self.assertIn("(Guard) Bribe with coins", names)
        self.assertNotIn("(Guard) Fight", names)  # tried this visit

    def test_repeat_rules(self):
        state = _state()
        _choose(state, "(Guard) Ask the time")
        _choose(state, "(Guard) Salute")
        _choose(state, "(Guard) Chat about the weather")
        names = _names(state)
        self.assertIn("(Guard) Salute", names)  # always
        self.assertNotIn("(Guard) Ask the time", names)  # once
        self.assertNotIn("(Guard) Chat about the weather", names)  # per_visit
        _choose(state, "Move to Yard")
        # The director hides a chat whose only outcome was told a turn ago (a loop)...
        self.assertNotIn("(Guard) Chat about the weather", _names(state))
        with patch.dict(config._config["director"], {"enabled": False}):  # ...the repeat rules alone:
            names = _names(state)
        self.assertIn("(Guard) Chat about the weather", names)  # a new visit (another guard, same rules)
        self.assertIn("(Guard) Ask the time", names)  # generic NPC: a different guard here

    def test_one_time_choices_of_a_unique_npc_hold_everywhere(self):
        state = _state(unique=True)
        state.npc_homes["Guard"] = "Gate"
        _choose(state, "(Guard) Ask the time")
        state.npc_homes["Guard"] = "Yard"  # follow the guard around
        _choose(state, "Move to Yard")
        self.assertNotIn("(Guard) Ask the time", _names(state))

    def test_shipped_rules(self):
        world = engine.build_world(_genome())
        guard = next(c for c in world.characters if c.name == "Gate Guard")
        groups = {a.name: a.exclusive_group for a in guard.actions}
        self.assertEqual(
            {name for name, group in groups.items() if group == "get_past_gate_guard"},
            {"(Gate Guard) Give him the {alcohol_desc}", "(Gate Guard) Show him your official's face",
             "(Gate Guard) Fight the guard", "(Gate Guard) Offer to settle the guard's debt quietly"},
        )
        study = next(a for o in world.objects if o.name == "Ancient Map" for a in o.actions)
        self.assertEqual(GameState(world, _genome())._repeat_rule(study), "once")

    def test_invalid_repeat_rule_is_rejected(self):
        with self.assertRaises(ValueError):
            engine._build_action({"name": "x", "repeat": "sometimes"}, [Outcome("x")], {})


class MessageTests(unittest.TestCase):
    def test_customised_messages_and_variants(self):
        with patch.dict(config._config, {"messages": {"voyage": ["You head towards the {location}.", "The {location} beckons."]}}):
            narrate = Narrator()
            self.assertEqual(narrate("voyage", location="Dungeon"), "You head towards the Dungeon.")
            self.assertEqual(narrate("voyage", location="Dungeon"), "The Dungeon beckons.")
            self.assertEqual(narrate("voyage", location="Dungeon"), "You head towards the Dungeon.")
            self.assertEqual(narrate("spawn", location="Vault"), "[SPAWN] New path discovered: Vault")  # default

    def test_option_names_are_stable_between_turns(self):
        with patch.dict(config._config, {"messages": {"travel_new": ["Walk to {location}", "Head for {location}"]}}):
            state = _state()
            self.assertEqual(_names(state), _names(state))
            self.assertIn(Narrator()("travel_new", stable="Yard", location="Yard"), _names(state))

    def test_validation(self):
        self.assertEqual(validate_messages({"voyage": "You head towards the {location}."}), [])
        self.assertEqual(validate_messages({"arrival_item": "You notice the {item}."}), [])  # extra engine field
        self.assertTrue(validate_messages({"voyge": "x"}))
        self.assertTrue(validate_messages({"voyage": "Toward {destination}"}))
        self.assertTrue(validate_messages({"voyage": []}))

    def test_malformed_text_falls_back_to_the_default(self):
        with patch.dict(config._config, {"messages": {"voyage": "Broken {location"}}):
            self.assertEqual(Narrator()("voyage", location="Vault"), DEFAULT_MESSAGES["voyage"].format(location="Vault"))

    def test_story_uses_custom_text_and_scoring_ignores_it(self):
        state = _state()
        state.actions_taken = 1
        state.consecutive_steps_in_location = config.get("narrative_end.max_consecutive_steps_in_location")
        with patch.dict(config._config, {"messages": {"end_stalled": "Nothing happens any more."}}):
            self.assertTrue(state._finalize_if_ended())
        self.assertEqual(state.end_kind, "stalled")
        self.assertEqual(state.history[-1], "[END] Nothing happens any more.")
        self.assertEqual(state.end_reason, "Nothing happens any more.")
        penalty = config.get("fitness.base.stall_penalty")
        self.assertLessEqual(engine._fitness_breakdown(state)["ending"], config.get("fitness.base.completion_bonus") - penalty)

    def test_shipped_file_matches_defaults(self):
        shipped = yaml.safe_load((store.CONFIG_DIR / "messages.yaml").read_text())["messages"]
        self.assertEqual(shipped, DEFAULT_MESSAGES)


class MessageEditingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "messages.yaml"
        self.path.write_text("messages:\n  voyage: \"The opening draws you toward {location}.\"\n")
        for patcher in (patch.object(content, "MESSAGES_PATH", self.path), patch.object(store.config, "reload")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_editor_validates_before_saving(self):
        good = "# mine\nmessages:\n  voyage: \"You head towards the {location}.\"\n"
        self.assertEqual(content.save_messages_yaml(good)[0], good)
        self.assertEqual(self.path.read_text(), good)  # comments kept
        with self.assertRaises(Exception):
            content.save_messages_yaml("messages:\n  voyage: \"To {place}\"\n")
        self.assertEqual(self.path.read_text(), good)

    def test_api(self):
        client = TestClient(api.api)
        body = client.get("/v1/messages").json()
        self.assertEqual(body["placeholders"]["voyage"], ["location"])
        self.assertEqual(client.put("/v1/messages", json={"messages": {"voyage": "Go to {nowhere}"}}).status_code, 400)
        self.assertEqual(client.put("/v1/messages", json={"messages": {"voyage": "Onward to {location}."}}).status_code, 200)
        self.assertEqual(yaml.safe_load(self.path.read_text())["messages"], {"voyage": "Onward to {location}."})


class GenomeDrivenChoiceTests(unittest.TestCase):
    def test_only_skill_checks_roll_dice(self):
        """Every decision is deterministic except the dice of skill checks."""
        class NoDice(random.Random):
            def random(self):
                raise AssertionError("only skill checks may roll dice")

        genome = _genome()
        world = engine.build_world(genome)
        rolls = []

        def fixed_roll(combat, stat, modifier=0, inventory=(), step=0):
            from core_engine.combat import CheckResult
            rolls.append(stat)
            return CheckResult(stat, 1, combat.target(stat, modifier, inventory, step), True, step)

        balanced = {name: 50 // 4 + (1 if i < 50 % 4 else 0) for i, name in enumerate(["attack", "defence", "stealth", "persuasion"])}
        with patch("core_engine.combat.CombatSystem.roll", fixed_roll):
            # The mission and start are fixed: drawing them is setup, not a storytelling decision.
            state = GameState(world, genome, rng=NoDice(0), mission=next(iter(world.missions)), stats=balanced,
                              start="Harbor")
            for _ in range(60):
                if not state.step():
                    break
        self.assertGreater(state.actions_taken, 0)

    def test_novelty_bias_discourages_repeating_a_choice(self):
        state = _state()
        salute = next(a for a in state.get_available_actions() if a.name == "(Guard) Salute")
        before = dict((a.name, w) for w, a in state._rank_actions([salute]))[salute.name]
        state.choice_counts[state._novelty_key(salute)] += 3
        after = dict((a.name, w) for w, a in state._rank_actions([salute]))[salute.name]
        power = config.get("story_rules.repetition_power")  # every further use costs more than the last
        self.assertAlmostEqual(before - after,
                               3 ** power * state.genome.novelty_bias * config.get("action_selection.repetition_penalty"))

    def test_variety_fitness_penalises_repeated_outcomes(self):
        state = _state()
        _choose(state, "(Guard) Salute")
        once = engine._fitness_breakdown(state)["variety"]
        _choose(state, "(Guard) Salute")
        self.assertLess(engine._fitness_breakdown(state)["variety"], once)

    def test_old_bundles_without_novelty_bias_load(self):
        values = {key: 0.5 for key in vars(Genome()) if key not in ("novelty_bias", "goal_bias")}
        genome = sessions._validated_genome(values)
        low, high = config.get("genome_bounds.novelty_bias")
        self.assertAlmostEqual(genome.novelty_bias, (low + high) / 2)


class ConversationTests(unittest.TestCase):
    """The people present are approached one at a time (conversations): on the scene each is one
    choice; in a conversation, their choices and a way to leave. Approaching and leaving take no
    turn; a decisive choice ends the conversation."""

    def setUp(self):
        patcher = patch.dict(config._config.setdefault("conversations", {}), {
            "enabled": True, "decisive_tags": ["combat", "threat", "guards", "escape"], "max_free_steps": 6})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _offered(state):
        return {option.name: option for option in state.player_options(limit=8)}

    def _approach(self, state, person="Guard"):
        return next(o for o in state.player_options(limit=8) if o.source == "approach" and o.character_name == person)

    def test_a_person_is_approached_and_left_without_a_turn(self):
        state = _state()
        offered = self._offered(state)
        self.assertFalse([name for name in offered if name.startswith("(Guard)")])  # behind "Approach"
        self.assertIn("Wait at the Gate", offered)
        state.take_option(self._approach(state))
        self.assertEqual((state.conversation(), state.actions_taken), ("Guard", 0))  # no turn
        talk = list(self._offered(state).values())
        self.assertTrue(all(o.character_name == "Guard" for o in talk))
        self.assertEqual(talk[-1].source, "leave")                                  # leaving is always a choice
        self.assertIn("(Guard) Bribe with wine", [o.name for o in talk])
        state.take_option(self._offered(state)["(Guard) Ask the time"])          # settles nothing: it goes on
        self.assertEqual((state.conversation(), state.actions_taken), ("Guard", 1))
        self.assertNotIn("(Guard) Ask the time", self._offered(state))
        state.take_option(next(o for o in state.player_options() if o.source == "leave"))
        self.assertEqual((state.conversation(), state.actions_taken), (None, 1))
        self.assertNotIn("Approach", " ".join(self._offered(state)))  # not again before something happens
        state.take_option(self._offered(state)["Wait at the Gate"])
        self.assertTrue(any(o.source == "approach" for o in state.player_options(limit=8)))
        self.assertTrue(any(line.startswith("You approach") for line in state.history))

    def test_a_decisive_choice_ends_the_conversation(self):
        state = _state()
        state.take_option(self._approach(state))
        state.take_option(self._offered(state)["(Guard) Bribe with wine"])  # one of an exclusive choice
        self.assertIsNone(state.conversation())
        self.assertEqual(state.actions_taken, 1)

    def _greeter(self, when):
        world = _world(unique=True)
        world.characters[0].associated_tags.add("doorman")
        rules = {"default": {}, "rules": [{"tags": ["doorman"], "starts_conversation": when}]}
        patcher = patch.dict(config._config, {"npc_behaviour": rules})
        patcher.start()
        self.addCleanup(patcher.stop)
        state = GameState(world, _genome(), start="Gate", rng=random.Random(1))
        state.npc_homes["Guard"] = "Gate"
        return state

    def test_someone_may_start_the_conversation_by_their_tags(self):
        state = self._greeter("first_meeting")
        state.player_options()
        self.assertEqual(state.conversation(), "Guard")
        self.assertTrue(any("comes up to you" in line for line in state.history))
        state.take_option(next(o for o in state.player_options() if o.source == "leave"))
        state.take_option(self._offered(state)["Move to Yard"])
        state.take_option(self._offered(state)["Return to Gate"])
        state.player_options()
        self.assertIsNone(state.conversation())  # only the first time they meet

        state = self._greeter("every_visit")
        state.player_options()
        state.take_option(next(o for o in state.player_options() if o.source == "leave"))
        state.take_option(self._offered(state)["Move to Yard"])
        state.take_option(self._offered(state)["Return to Gate"])
        state.player_options()
        self.assertEqual(state.conversation(), "Guard")  # again, on the next visit

    def test_conversations_off_and_a_story_that_always_moves_on(self):
        with patch.dict(config._config["conversations"], {"enabled": False}):
            self.assertIn("(Guard) Salute", self._offered(_state()))  # every choice on the scene, as before
        with patch.dict(config._config["conversations"], {"max_free_steps": 2}):
            state = _state()
            state.take_option(self._approach(state))
            state.take_option(next(o for o in state.player_options() if o.source == "leave"))
            self.assertIn("(Guard) Salute", self._offered(state))  # two free steps: everything on the scene
        # The storyteller's own list is never behind conversations.
        self.assertIn("(Guard) Salute", _names(_state()))

    def test_simulated_players_and_the_runtime_take_conversations(self):
        from core_engine import players

        state = _state()
        players.play(state, 12, players.Explorer(random.Random(0)))
        self.assertEqual(state.actions_taken, 12)                      # 12 turns, whatever was approached
        self.assertTrue(any(line.startswith("You approach") for line in state.history))

        from narrative_runtime.engine import Game

        game = Game(None, _state())
        approach = next(name for name in game.actions() if name.startswith("Approach"))
        view = game.choose(approach)
        self.assertEqual((view["talking_to"], view["turn_number"]), ("Guard", 0))
        self.assertEqual(view["actions"][-1], "Leave the conversation")


class NpcBehaviourTests(unittest.TestCase):
    """What people do comes from their tags (npc_behaviour), not from each character."""

    def _state(self, appearances, mission=None):
        from core_engine import Mission

        world = _world()
        world.characters[0].associated_tags.add("drifter")
        if mission:
            world.add_mission(Mission("bribe", {"action": "Bribe with wine"}))
        rules = {"default": {"appearances": "always"}, "rules": [{"tags": ["drifter"], "appearances": appearances}]}
        patcher = patch.dict(config._config, {"npc_behaviour": rules})
        patcher.start()
        self.addCleanup(patcher.stop)
        return GameState(world, _genome(), start="Gate", rng=random.Random(1), mission=mission)

    @staticmethod
    def _visits_seen(state, visits):
        seen = 0
        for _ in range(visits):
            state.ranked_available_actions()
            seen += "Guard" in state.visible_inhabitants
            there = "Yard" if state.current.name == "Gate" else "Gate"
            ranked = state.ranked_available_actions()
            state.step(next(i for i, a in enumerate(ranked) if a.source == "navigation" and a.owner == there))
        return seen

    def test_how_often_someone_appears(self):
        self.assertEqual(self._visits_seen(self._state("once"), 6), 1)
        self.assertEqual(self._visits_seen(self._state(2), 6), 2)
        self.assertEqual(self._visits_seen(self._state("always"), 6), 6)
        # The quest needs him: no limit (as often as with none; a quest's target is revealed as the story goes).
        self.assertEqual(self._visits_seen(self._state("once", mission="bribe"), 6),
                         self._visits_seen(self._state("always", mission="bribe"), 6))
        self.assertGreater(self._visits_seen(self._state("once", mission="bribe"), 6), 1)

    def test_the_rules_are_checked(self):
        from core_engine import rules

        self.assertEqual(rules.validate_npc_behaviour(config.get("npc_behaviour")), [])
        self.assertEqual(rules.npc_behaviour({"prisoner"}, "starts_conversation"), "first_meeting")
        self.assertEqual(rules.npc_behaviour({"nobody"}, "appearances"), "always")
        for bad in ({"rules": [{"tags": ["x"], "appearances": 0}]}, {"rules": [{"tags": ["x"], "starts_conversation": "maybe"}]},
                    {"rules": [{"appearances": "once"}]}, {"rules": [{"tags": ["x"], "smiles": True}]}):
            with self.subTest(rules=bad):
                self.assertTrue(rules.validate_npc_behaviour(bad))
        with patch.dict(config._config, {"npc_behaviour": {"rules": [{"tags": ["x"], "appearances": "never"}]}}):
            with self.assertRaises(ValueError):
                engine.build_world(_genome())


class QuestionAndGuideTests(unittest.TestCase):
    """Choices that make sense: a question is asked once, and a guide leads toward the quest."""

    def _world(self):
        from core_engine import Mission

        world = WorldGraph()
        for name, tags in (("Gate", ["gate"]), ("Yard", ["yard", "oracle"]), ("Tower", ["tower"])):
            world.add_location(Location(name, tags))
        world.locations["Gate"].connect(world.locations["Yard"])
        world.locations["Gate"].connect(world.locations["Tower"])
        oracle = Character("Oracle", ["gate", "oracle"], unique=True, known_locations=["Gate", "Yard", "Tower"], actions=[
            Action("(Oracle) Ask about the crown", [Outcome("It lies in the Tower.", reveal_location="Tower")],
                   character_name="Oracle", source="npc", owner="Oracle"),
            Action("(Oracle) Ask again and again", [Outcome("It lies in the Tower.", reveal_location="Tower")],
                   character_name="Oracle", source="npc", owner="Oracle", repeat="always"),
            Action("(Oracle) Sell a riddle", [Outcome("Two coins.", coin_change=2)],
                   character_name="Oracle", source="npc", owner="Oracle"),
            Action("(Oracle) Show the way", [Outcome("Follow me.", lead_to_known=True)],
                   character_name="Oracle", source="npc", owner="Oracle", repeat="always"),
        ])
        world.add_character(oracle)
        world.add_mission(Mission("climb", {"location": "Tower"}, title="Climb the tower"))
        return world

    def _state(self, mission=None):
        return GameState(self._world(), _genome(), start="Gate", rng=random.Random(1), mission=mission)

    def test_a_question_is_asked_once_and_an_empty_answer_comes_back(self):
        state = self._state()
        state.npc_homes["Oracle"] = "Gate"
        oracle = state.world.characters[0]
        ask, again, sell, _ = oracle.actions
        self.assertTrue(state._is_question(ask))
        self.assertFalse(state._is_question(again))   # its own `repeat` wins
        self.assertFalse(state._is_question(sell))    # it changes something: a purchase
        self.assertEqual(state._repeat_rule(ask), "once")
        _choose(state, "(Oracle) Ask about the crown")
        self.assertNotIn("(Oracle) Ask about the crown", _names(state))
        _choose(state, "Move to Yard")
        state.npc_homes["Oracle"] = "Yard"
        self.assertNotIn("(Oracle) Ask about the crown", _names(state))  # answered: not asked again anywhere
        # An answer that told nothing does not use the question up (it comes back on the next visit).
        fresh = self._state()
        scope = fresh._action_scope(ask)
        fresh._record_choice(ask, True, scope, told=Outcome("The oracle only hums."))
        self.assertNotIn((scope, ask.name), fresh.spent_actions)
        fresh._record_choice(ask, True, scope, told=ask.outcomes[0])
        self.assertIn((scope, ask.name), fresh.spent_actions)
        with patch.dict(config._config.setdefault("action_rules", {}), {"questions_once": False}):
            self.assertFalse(self._state()._is_question(ask))

    def test_a_guide_leads_toward_the_quest(self):
        state = self._state(mission="climb")
        self.assertGreater(state._quest_pull("Tower"), state._quest_pull("Yard"))
        free = self._state()
        free.mission = None
        self.assertEqual(free._quest_pull("Tower"), 0.0)  # no quest, no pull
        state.npc_homes["Oracle"] = "Gate"
        with patch.dict(config._config.setdefault("npc_leads", {}), {"quest_weight": 50.0}):
            _choose(state, "(Oracle) Show the way")
        self.assertEqual(state.current.name, "Tower")  # not the Yard, whose tags suit the oracle better
        alone = self._state(mission="climb")
        alone.npc_homes["Oracle"] = "Gate"
        with patch.dict(config._config.setdefault("npc_leads", {}), {"quest_weight": 0.0}):
            _choose(alone, "(Oracle) Show the way")
        self.assertEqual(alone.current.name, "Yard")

    def test_the_quest_log_says_what_to_do_in_words(self):
        from core_engine import Mission

        world = self._world()
        world.add_mission(Mission("brawl", {}, stages=[
            {"title": "Win a brawl in the Yard", "goal": {"outcome_tag": "brawl_won"}},
            {"title": "Find a way", "goal": {"condition": {"any": [{"action_done": "Ask about the crown"},
                                                                  {"has_item": "Map"}]}}}]))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(1), mission="brawl")
        self.assertEqual(state.quest_log()["todo"], ["Win a brawl in the Yard"])  # the step's title, not its tag
        state.mission_stage = 1
        self.assertEqual(state.quest_log()["todo"], ["Ask about the crown or Get the Map"])


if __name__ == "__main__":
    unittest.main()
