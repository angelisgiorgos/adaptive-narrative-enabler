"""One-time and exclusive options, customisable story text, and genome-driven choices."""
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
import app as studio
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
            {"(Gate Guard) Give him the {alcohol_desc}", "(Gate Guard) Bribe with coins", "(Gate Guard) Fight the guard"},
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
        shipped = yaml.safe_load((studio.CONFIG_DIR / "messages.yaml").read_text())["messages"]
        self.assertEqual(shipped, DEFAULT_MESSAGES)


class MessageEditingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "messages.yaml"
        self.path.write_text("messages:\n  voyage: \"The opening draws you toward {location}.\"\n")
        for patcher in (patch.object(studio, "MESSAGES_PATH", self.path), patch.object(studio.config, "reload")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_editor_validates_before_saving(self):
        good = "# mine\nmessages:\n  voyage: \"You head towards the {location}.\"\n"
        self.assertEqual(studio.save_messages_yaml(good)[0], good)
        self.assertEqual(self.path.read_text(), good)  # comments kept
        with self.assertRaises(Exception):
            studio.save_messages_yaml("messages:\n  voyage: \"To {place}\"\n")
        self.assertEqual(self.path.read_text(), good)

    def test_api(self):
        client = TestClient(api.api)
        body = client.get("/v1/messages").json()
        self.assertEqual(body["placeholders"]["voyage"], ["location"])
        self.assertEqual(client.put("/v1/messages", json={"messages": {"voyage": "Go to {nowhere}"}}).status_code, 400)
        self.assertEqual(client.put("/v1/messages", json={"messages": {"voyage": "Onward to {location}."}}).status_code, 200)
        self.assertEqual(yaml.safe_load(self.path.read_text())["messages"], {"voyage": "Onward to {location}."})


class GenomeDrivenChoiceTests(unittest.TestCase):
    def test_no_dice_rolls_during_play(self):
        class NoDice(random.Random):
            def random(self):
                raise AssertionError("play must not roll dice")

        genome = _genome()
        world = engine.build_world(genome)
        state = GameState(world, genome, rng=NoDice(0), mission=next(iter(world.missions)))
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
        self.assertAlmostEqual(before - after, 3 * state.genome.novelty_bias * config.get("action_selection.repetition_penalty"))

    def test_variety_fitness_penalises_repeated_outcomes(self):
        state = _state()
        _choose(state, "(Guard) Salute")
        once = engine._fitness_breakdown(state)["variety"]
        _choose(state, "(Guard) Salute")
        self.assertLess(engine._fitness_breakdown(state)["variety"], once)

    def test_old_bundles_without_novelty_bias_load(self):
        values = {key: 0.5 for key in vars(Genome()) if key not in ("novelty_bias", "goal_bias")}
        genome = studio._validated_genome(values)
        low, high = config.get("genome_bounds.novelty_bias")
        self.assertAlmostEqual(genome.novelty_bias, (low + high) / 2)


if __name__ == "__main__":
    unittest.main()
