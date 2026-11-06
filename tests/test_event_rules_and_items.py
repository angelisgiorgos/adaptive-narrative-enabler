"""Event start rules (eligibility, repeat, occurrences, cooldown, random schedule),
event goals that tell success from failure, and unique items."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import os
import random
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
import main as engine
from backend import content, store
from backend.errors import AuthoringError
from core_engine import Action, Character, Event, GameState, Genome, Location, Mission, Object, Outcome, WorldGraph
from core_engine.builder import logic_errors
from utils.config_loader import config


def _genome():
    random.seed(7)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _quiet():
    """No procedural unexpected events, no stalling and no dice of tag checks."""
    return [
        patch.dict(config._config["unexpected_events"], {"enabled": False}),
        patch.dict(config._config["narrative_end"], {"max_consecutive_steps_in_location": 100}),
        patch.dict(config._config["world_definition"]["character_stats"], {"tag_checks": {}}),
    ]


def _act(name, *outcomes, **kwargs):
    """An event action (offered every time, as the builder's event actions are by default)."""
    return Action(name, list(outcomes), source="event", **kwargs)


def _parade_world(*events, missions=(), drummer=True):
    """Market and Alley. Two Market actions and a Drummer's encounter (in the
    Alley) start the parade; an action of the fanfare chains into it."""
    world = WorldGraph()
    market, alley = Location("Market", ["market"]), Location("Alley", ["alley"])
    market.actions = [
        Action("Ring the bell", [Outcome("Ding.", tags=["event"])], triggers_event="parade"),
        Action("Blow the horn", [Outcome("Toot.", tags=["event"])], triggers_event="parade"),
        Action("Sound the fanfare", [Outcome("Ta-da.", tags=["event"])], triggers_event="fanfare"),
        Action("Ask the riddle", [Outcome("Hmm.", tags=["event"])], triggers_event="riddle"),
        Action("Wait", [Outcome("You wait.", tags=["neutral"])], repeat="always"),
    ]
    market.connect(alley)
    world.add_location(market)
    world.add_location(alley)
    if drummer:
        world.add_character(Character("Drummer", ["alley"], known_locations=["Alley"], unique=True,
                                      encounter_event="parade", encounter_repeat=True))
    world.add_object(Object("Festival Pass", ["pass"]))
    world.add_object(Object("Key", ["key"]))
    defaults = {
        "parade": _parade(),
        "fanfare": Event("fanfare", title="Fanfare", actions=[
            _act("Join the parade", Outcome("You fall in.", tags=["neutral"]), triggers_event="parade"),
            _act("Listen", Outcome("You listen.", tags=["neutral"])),
        ]),
        "riddle": Event("riddle", title="Riddle", actions=[
            _act("Unlock the answer", Outcome("Solved."), required_object="Key"),
        ]),
    }
    defaults.update({event.name: event for event in events})
    for event in defaults.values():
        world.add_event(event)
    for mission in missions:
        world.add_mission(mission)
    return world


def _names(state):
    return [action.name for action in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


def _parade(**rules):
    return Event("parade", title="Parade", actions=[
        _act("Cheer", Outcome("You cheer.", tags=["neutral"])),
        _act("Heckle", Outcome("Nobody laughs.", tags=["neutral"], success_prob=0.0)),
    ], **rules)


class _Quiet(unittest.TestCase):
    def setUp(self):
        for patcher in _quiet():
            patcher.start()
            self.addCleanup(patcher.stop)

    def _state(self, *events, start="Market", seed=1, drummer=True, **kwargs):
        return GameState(_parade_world(*events, missions=kwargs.pop("missions", ()), drummer=drummer), _genome(),
                         start=start, rng=random.Random(seed), **kwargs)

    def _parades(self, state):
        return [entry for entry in state.event_log if entry["event"] == "parade"]


class UniqueEventTests(_Quiet):
    def test_a_unique_event_cannot_be_repeated_through_another_way_in(self):
        state = self._state(_parade(repeat="once"))
        _choose(state, "Ring the bell")
        self.assertEqual(state.active_event.name, "parade")
        _choose(state, "Cheer")
        names = _names(state)
        self.assertNotIn("Blow the horn", names)                  # another triggering action
        self.assertFalse(state._enter_event("parade"))            # asked directly
        state.apply_effects([{"start_event": "parade"}])          # an effect
        self.assertIsNone(state.active_event)
        _choose(state, "Sound the fanfare")                       # a chain: its option is not offered
        self.assertEqual(_names(state), ["Listen"])
        _choose(state, "Listen")
        _choose(state, "Move to Alley")                           # an NPC encounter
        self.assertIsNone(state.active_event)
        self.assertEqual(len(self._parades(state)), 1)

    def test_without_the_rule_every_way_in_works(self):
        state = self._state()
        for how in ("Ring the bell", "Blow the horn"):
            _choose(state, how)
            _choose(state, "Cheer")
        _choose(state, "Sound the fanfare")
        _choose(state, "Join the parade")
        _choose(state, "Cheer")
        _choose(state, "Move to Alley")
        self.assertEqual([entry["cause"] for entry in self._parades(state)], ["action", "action", "chain", "encounter"])

    def test_repeat_per_visit_and_per_location(self):
        state = self._state(_parade(repeat="per_visit"))
        _choose(state, "Ring the bell")
        _choose(state, "Cheer")
        self.assertNotIn("Blow the horn", _names(state))           # not again in this visit
        _choose(state, "Move to Alley")
        _choose(state, "Cheer")                                    # the drummer's parade, a new place
        _choose(state, "Return to Market")
        self.assertIn("Blow the horn", _names(state))              # a new visit
        per_location = self._state(_parade(repeat="per_location"))
        _choose(per_location, "Ring the bell")
        _choose(per_location, "Cheer")
        _choose(per_location, "Move to Alley")
        self.assertEqual(per_location.active_event.name, "parade")  # another location
        _choose(per_location, "Cheer")
        _choose(per_location, "Return to Market")
        self.assertNotIn("Blow the horn", _names(per_location))    # never again at the Market

    def test_max_occurrences_and_cooldown(self):
        state = self._state(_parade(max_occurrences=2, cooldown=2))
        _choose(state, "Ring the bell")
        _choose(state, "Cheer")
        self.assertNotIn("Blow the horn", _names(state))           # cooling down
        _choose(state, "Wait")
        self.assertNotIn("Blow the horn", _names(state))
        _choose(state, "Wait")
        _choose(state, "Blow the horn")                            # 2 turns after it ended
        _choose(state, "Cheer")
        for _ in range(4):
            _choose(state, "Wait")
        self.assertNotIn("Blow the horn", _names(state))           # used up
        self.assertEqual(len(self._parades(state)), 2)


class DisabledEventTests(_Quiet):
    def test_a_disabled_event_cannot_start_in_any_way(self):
        state = self._state(_parade(enabled_if={"has_item": "Festival Pass"}))
        names = _names(state)
        self.assertNotIn("Ring the bell", names)                   # trigger actions
        self.assertNotIn("Blow the horn", names)
        self.assertFalse(state._enter_event("parade"))             # asked directly
        state.apply_effects([{"start_event": "parade"}])           # an effect
        self.assertIsNone(state.active_event)
        _choose(state, "Sound the fanfare")
        self.assertNotIn("Join the parade", _names(state))         # a chain
        _choose(state, "Listen")
        _choose(state, "Move to Alley")
        self.assertIsNone(state.active_event)                      # an encounter waits...
        self.assertEqual(self._parades(state), [])
        state.apply_effects([{"give_item": "Festival Pass"}])
        _names(state)
        self.assertEqual(state.active_event.name, "parade")        # ...and happens once it is enabled

    def test_a_chain_stops_when_the_action_itself_disables_the_next_event(self):
        """_enter_event re-checks the rules: an option offered while the event was
        enabled no longer starts it once its own effects switch it off."""
        fanfare = Event("fanfare", title="Fanfare", actions=[
            _act("Join the parade", Outcome("You hand in your pass.", tags=["neutral"],
                                            effects=[{"take_item": "Festival Pass"}]), triggers_event="parade"),
        ])
        state = self._state(_parade(enabled_if={"has_item": "Festival Pass"}), fanfare)
        state.apply_effects([{"give_item": "Festival Pass"}])
        _choose(state, "Sound the fanfare")
        _choose(state, "Join the parade")
        self.assertIsNone(state.active_event)
        self.assertEqual(self._parades(state), [])
        self.assertIn("[EVENT END] Fanfare is over. You are back in the Market.", state.history)


class RandomScheduleTests(_Quiet):
    def _storm(self, **rules):
        return Event("storm", title="Storm", actions=[_act("Take shelter", Outcome("You shelter."))],
                     trigger="random", **rules)

    def test_random_events_start_by_themselves_where_their_condition_holds(self):
        state = self._state(self._storm(chance=1.0, enabled_if={"at_location": "Alley"}), drummer=False)
        _names(state)
        self.assertIsNone(state.active_event)                      # not at the Market
        _choose(state, "Move to Alley")
        names = _names(state)
        self.assertEqual(state.active_event.name, "storm")
        self.assertEqual(names, ["Take shelter"])
        self.assertEqual(state.event_log[-1]["cause"], "random")

    def test_chance_per_turn_is_rolled_once_with_the_session_rng(self):
        started = 0
        for seed in range(1000):
            state = self._state(self._storm(chance=0.3), seed=seed)
            state.get_available_actions()
            rng_after_first = state.rng.getstate()
            state.get_available_actions()
            self.assertEqual(state.rng.getstate(), rng_after_first)  # one roll per turn
            started += state.active_event is not None
        self.assertAlmostEqual(started / 1000, 0.3, delta=0.04)
        same = [self._state(self._storm(chance=0.3), seed=4) for _ in range(2)]
        self.assertEqual(*[bool(s.get_available_actions() and s.active_event) for s in same])

    def test_weight_picks_between_events_that_hit_together(self):
        picked = Counter()
        for seed in range(800):
            state = self._state(self._storm(chance=1.0, weight=3),
                                Event("fog", title="Fog", actions=[_act("Wait it out", Outcome("Fog."))],
                                      trigger="random", chance=1.0, weight=1), seed=seed)
            state.get_available_actions()
            picked[state.active_event.name] += 1
        self.assertAlmostEqual(picked["storm"] / 800, 0.75, delta=0.05)

    def test_cooldown_and_max_occurrences_hold_for_random_events(self):
        state = self._state(self._storm(chance=1.0, cooldown=2, max_occurrences=3))
        steps = []
        for _ in range(16):
            if state.ended:
                break
            names = _names(state)
            if state.active_event:
                steps.append(state.actions_taken)
                _choose(state, "Take shelter")
            else:
                _choose(state, "Wait")
        self.assertEqual(steps, [0, 3, 6])                         # 2 turns of cooldown, then at most 3


class EventGoalTests(_Quiet):
    def _missions(self):
        return [Mission("succeed", {"event_succeeded": "parade"}), Mission("any", {"event": "parade"}),
                Mission("rich", {"coins": 999})]

    def test_event_goals_tell_success_from_failure(self):
        state = self._state(missions=self._missions(), mission="rich")
        succeed, any_result = state.world.missions["succeed"], state.world.missions["any"]
        _choose(state, "Ring the bell")
        _choose(state, "Heckle")                                   # the event action fails
        self.assertEqual(state.event_log[-1]["result"], "failure")
        self.assertTrue(state._goal_met(any_result))
        self.assertFalse(state._goal_met(succeed))
        self.assertTrue(state.holds({"event_failed": "parade"}))
        _choose(state, "Blow the horn")
        _choose(state, "Cheer")                                    # now it succeeds
        self.assertTrue(state._goal_met(succeed))
        self.assertTrue(state.holds({"event_succeeded": "parade"}))

    def test_stepping_away_is_neither_success_nor_failure(self):
        state = self._state(missions=self._missions(), mission="rich")
        _choose(state, "Ask the riddle")
        self.assertEqual(_names(state), ["Step away from the situation"])  # no Key: only the fallback
        _choose(state, "Step away from the situation")
        self.assertEqual(state.event_log[-1]["result"], "abandoned")
        self.assertTrue(state.holds({"event_done": "riddle"}))
        self.assertFalse(state.holds({"any": [{"event_succeeded": "riddle"}, {"event_failed": "riddle"}]}))

    def test_a_mission_needing_success_is_won_only_by_success(self):
        state = self._state(missions=self._missions(), mission="succeed")
        _choose(state, "Ring the bell")
        _choose(state, "Heckle")
        self.assertFalse(state.won)
        self.assertIn("Market", state._goal_sites())                # the planner sends the player back to try again
        _choose(state, "Blow the horn")
        _choose(state, "Cheer")
        self.assertTrue(state.won and state.ended)


def _shop_world():
    """A Lantern (generic), a Crown (unique, placed where it fits best) and a Key
    (unique, pinned to the Cellar although its tags fit the shops)."""
    world = WorldGraph()
    for name, tags in (("Market", ["shop", "market"]), ("Alley", ["shop", "alley"]), ("Cellar", ["cellar"])):
        world.add_location(Location(name, tags))
    world.locations["Market"].connect(world.locations["Alley"])
    world.locations["Alley"].connect(world.locations["Cellar"])

    def item(name, **kwargs):
        return Object(name, ["shop"], actions=[
            Action(f"[{name}] Take the {name.lower()}", [Outcome("Taken.", tags=["neutral"])], collects_object=name),
        ], **kwargs)

    world.add_object(item("Lantern"))
    world.add_object(item("Crown", unique=True))
    world.add_object(item("Key", unique=True, location="Cellar"))
    return world


class UniqueItemTests(_Quiet):
    def _state(self, start="Market"):
        return GameState(_shop_world(), _genome(), start=start, rng=random.Random(0))

    def _on_show(self, state):
        return {name: state._scene_objects(loc)[1] for name, loc in state.world.locations.items()}

    def test_a_unique_item_is_one_placed_instance(self):
        state = self._state()
        shown = self._on_show(state)
        self.assertEqual([name for name, items in shown.items() if "Lantern" in items], ["Market", "Alley"])  # generic
        self.assertEqual([name for name, items in shown.items() if "Crown" in items], [state.item_instances["Crown"].location])
        self.assertEqual([name for name, items in shown.items() if "Key" in items], ["Cellar"])
        self.assertEqual({entry["item"]: entry["location"] for entry in state.item_log if entry["what"] == "placed"},
                         {"Crown": state.item_instances["Crown"].location, "Key": "Cellar"})
        self.assertEqual(state.item_instances["Crown"].location, GameState(_shop_world(), _genome(), start="Alley").item_instances["Crown"].location)

    def test_collecting_and_using_a_unique_item(self):
        state = self._state()
        home = state.item_instances["Crown"].location
        if state.current.name != home:
            _choose(state, f"Move to {home}")
        _choose(state, "[Crown] Take the crown")
        self.assertEqual(state.item_instances["Crown"].state, "held")
        self.assertEqual(self._on_show(state)[home] & {"Crown"}, set())  # no longer lying anywhere
        self.assertNotIn("[Crown] Take the crown", _names(state))
        state.apply_effects([{"take_item": "Crown"}])                  # used up
        self.assertEqual(state.item_instances["Crown"].state, "gone")
        state.apply_effects([{"give_item": "Crown"}, {"reveal_item": "Crown"}])
        self.assertNotIn("Crown", state.inventory)                     # never a second copy
        self.assertNotIn("Crown", state.visible_objects)
        self.assertEqual(state._item_sites("Crown"), set())
        self.assertTrue(state.holds({"item_collected": "Crown"}))      # history, independent of the inventory
        self.assertFalse(state.holds({"has_item": "Crown"}))
        self.assertEqual([e["what"] for e in state.item_log if e["item"] == "Crown"], ["placed", "collected", "removed"])

    def test_a_generic_item_can_be_found_again(self):
        state = self._state()
        _choose(state, "[Lantern] Take the lantern")
        state.apply_effects([{"take_item": "Lantern"}])
        _choose(state, "Move to Alley")
        self.assertIn("[Lantern] Take the lantern", _names(state))

    def test_the_shipped_ledger_lies_only_in_the_sewers(self):
        genome = _genome()
        state = GameState(engine.build_world(genome), genome, start="Market", rng=random.Random(0),
                          variables={"fence_exposed": True})
        ledger = "Fence's Ledger"
        def placed():
            return [n for n, loc in state.world.locations.items()
                    if ledger in {obj.name for obj in state._scene_objects(loc)[0]}]

        self.assertEqual(placed(), ["Sewers"])
        generic = next(o for o in state.world.objects if o.name == ledger)
        generic.unique = False                                          # what its tags alone would allow
        self.assertGreater(len(placed()), 1)


class ValidationAndAuthoringTests(unittest.TestCase):
    def test_bad_rules_are_rejected(self):
        world = config.default_config()["world_definition"]
        bad_events = [
            {"trigger": "sometimes"}, {"trigger": "random"}, {"trigger": "random", "chance": 1.5},
            {"chance": 0.5}, {"trigger": "random", "chance": 0.5, "weight": 0}, {"cooldown": -1},
            {"max_occurrences": 0}, {"repeat": "twice"},
        ]
        for rules in bad_events:
            with self.subTest(rules=rules):
                candidate = {**world, "events": {**world["events"], "confront_thief": {**world["events"]["confront_thief"], **rules}}}
                self.assertTrue(logic_errors(candidate))
        for rules in ({"unique": "yes"}, {"location": "Sewers"}, {"unique": True, "location": "Atlantis"}):
            with self.subTest(rules=rules):
                objects = [{**world["objects"][0], **rules}] + world["objects"][1:]
                self.assertTrue(logic_errors({**world, "objects": objects}))
        self.assertEqual(logic_errors(world), [])

    def test_rules_editor_and_api(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        shutil.copy(store.CONFIG_DIR / "events.yaml", folder / "events.yaml")
        for patcher in (
            patch.dict(content.ENTITY_FILES, {"Events": (folder / "events.yaml", "events", "mapping")}),
            patch.object(store.config, "reload"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        client = TestClient(api.api)
        self.assertEqual(client.get("/v1/entities/Events/memory_theft/policy").json(), {"policy": {"repeat": "once"}})
        before = (folder / "events.yaml").read_bytes()
        response = client.put("/v1/entities/Events/bandit_ambush/policy", json={"policy": {"trigger": "random"}})
        self.assertEqual(response.status_code, 400)                          # no chance
        self.assertEqual((folder / "events.yaml").read_bytes(), before)      # nothing written
        rules = {"trigger": "random", "chance": 0.1, "cooldown": 5, "repeat": "per_visit"}
        self.assertEqual(client.put("/v1/entities/Events/bandit_ambush/policy", json={"policy": rules}).json(), {"policy": rules})
        saved = yaml.safe_load((folder / "events.yaml").read_text())["world_definition"]["events"]["bandit_ambush"]
        self.assertEqual({k: saved[k] for k in rules}, rules)
        self.assertTrue(saved["actions"])                                    # the rest of the event is kept
        with self.assertRaises(AuthoringError):
            content.load_entity_policy("Locations", "Harbor")


if __name__ == "__main__":
    unittest.main()
