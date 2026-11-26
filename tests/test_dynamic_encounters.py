"""The encounter director (core_engine/encounters.py, an event's `encounter` block, `encounters` in
the tuning): eligibility from the world state, cooldowns and caps, safe places, the player's choices,
losing and recovering, side quests and factions, what it must not interrupt, saves, journal and
author diagnostics, deterministic replay and no reward farming; the two example encounters."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import random
import unittest
from unittest.mock import patch

import yaml

from backend import store
from core_engine import (Action, Character, Event, GameState, Genome, Location, Outcome, SideQuest, WorldGraph, choices,
                         encounters, snapshot)
from core_engine.builder import build_world, event_policy_errors
from core_engine.context import EngineContext
from core_engine.diagnostics import decision_trace
from utils.config_loader import config

FORCED = {"rate": 1.0, "max_chance": 1.0, "opening_turns": 0}  # an attack whenever one may come


def _genome():
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _rest(place):
    return Action(f"Rest at {place}", [Outcome("You rest.", tags=["neutral"])], source="location", owner=place,
                  repeat="always")


def _template(name="thieves", fight=None, encounter=None, **event):
    """An ambush: fight them (or as given), pay them (needs coins), or give in (no check: a loss)."""
    actions = [
        fight or Action("Fight them", [Outcome("You drive them off.", tags=["combat", "success"])],
                        source="event", owner=name),
        Action("Pay them", [Outcome("They take the coins and let you pass.", tags=["trade", "success"],
                                    effects=[{"change_coins": -3}])], source="event", owner=name, required_coins=3),
        Action("Give in", [Outcome("They take your purse.", tags=["failure"], effects=[{"change_coins": -1}])],
               source="event", owner=name),
    ]
    spec = {"category": "ambush", "tags_any": ["glacier"], **(encounter or {})}
    return Event(name, title=f"The {name}", tags=["threat"], actions=actions, encounter=spec, **event)


def _world(*events, npcs=()):
    """A safe camp, a glacier pass, a ridge and a tower: Camp - Pass - Ridge - Tower."""
    world = WorldGraph()
    for name, tags in (("Camp", ["safe", "camp"]), ("Pass", ["glacier", "threat"]), ("Ridge", ["mountain"]),
                       ("Tower", ["glacier", "tower"])):
        location = Location(name, tags)
        location.actions = [_rest(name)]
        world.add_location(location)
    world.connect("Camp", "Pass")
    world.connect("Pass", "Ridge")
    world.connect("Ridge", "Tower")
    for event in events:
        world.add_event(event)
    for npc in npcs:
        world.add_character(npc)
    return world


def _state(world, start="Camp", seed=3, **policy):
    """A story whose settings are fixed when it starts (its engine context), the encounter policy
    changed by ``policy`` (forced: an attack whenever one may come, unless given)."""
    settings = {"encounters": {**config.get("encounters"), **FORCED, **policy},
                # the encounter director's own rules (the side-story policy on top: tests/test_side_story.py)
                "side_story": {**config.get("side_story"), "enabled": False},
                "unexpected_events": {**config.get("unexpected_events"), "enabled": False},
                "npc_roles": {**(config.get("npc_roles") or {}), "enabled": False},
                "conversations": {**config.get("conversations"), "enabled": False}}
    with patch.dict(config._config, settings):
        state = GameState(world, _genome(), start=start, rng=random.Random(seed))
    for char in world.characters:
        state.npc_homes[char.name] = start
    state._scene_dirty = True
    return state


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _ids(state):
    return choices.assign_ids(state.player_options(), state)


def _take(state, prefix):
    options = state.player_options()
    ids = choices.assign_ids(options, state)
    state.take_option(next(option for option, action_id in zip(options, ids) if action_id.startswith(prefix)))
    return state


def _attacked(state):
    return state.active_event is not None and state.active_event.encounter is not None


class EligibilityTests(unittest.TestCase):
    def test_where_it_happens_and_the_safe_places(self):
        state = _state(_world(_template()))
        _ids(state)
        self.assertFalse(_attacked(state))                                             # the camp: safe
        self.assertEqual(encounters.explain(state)["gate"], "safe_place")
        _take(state, "go:pass")
        _ids(state)
        self.assertTrue(_attacked(state))                                              # the glacier pass
        self.assertIn("[AMBUSH] The thieves! In the Pass, they were waiting for you.", state.history)
        ridge = _state(_world(_template()), start="Ridge")
        _ids(ridge)
        self.assertFalse(_attacked(ridge))                                             # no glacier tag
        self.assertEqual(encounters.explain(ridge)["candidates"][0]["status"], "not_here")
        excluded = _state(_world(_template(encounter={"tags_none": ["threat"]})), start="Pass")
        _ids(excluded)
        self.assertFalse(_attacked(excluded))

    def test_the_world_state_decides(self):
        def status(state):
            return encounters.explain(state)["candidates"][0]["status"]

        cases = (({"min_alert": 2}, lambda s: None, "alert", lambda s: setattr(s, "alert", 3)),
                 ({"min_tension": 0.6}, lambda s: None, "tension", lambda s: setattr(s, "tension", lambda: 0.9)),
                 ({"min_power": 50.0}, lambda s: None, "too_weak",
                  lambda s: setattr(s.world.events["thieves"].encounter, "min_power", 0.0)),  # Power is computed
                 ({"min_turn": 2}, lambda s: None, "too_early", lambda s: setattr(s, "actions_taken", 2)),
                 ({"stages": ["late"]}, lambda s: None, "wrong_stage", lambda s: setattr(s, "actions_taken", 90)))
        for spec, _, blocked, unlock in cases:
            with self.subTest(spec=spec):
                state = _state(_world(_template(encounter=spec)), start="Pass")
                self.assertEqual(status(state), blocked)
                unlock(state)
                self.assertEqual(status(state), "eligible")
        hurt = _state(_world(_template()), start="Pass")
        hurt.health_player.health = 1
        self.assertEqual(encounters.explain(hurt)["gate"], "low_health")               # no attack on the wounded

    def test_completed_quests_and_previous_actions_through_conditions(self):
        gated = _template(enabled_if={"side_quest_done": "errand"})
        errand = SideQuest("errand", {"location": "Ridge"}, title="An errand", accept="auto")
        world = _world(gated)
        world.add_side_quest(errand)
        state = _state(world, start="Pass", min_separation=0)
        self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "event_blocked")
        state.side_quests["errand"] = {"status": "completed", "stage": 0}
        self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "eligible")

    def test_factions_and_their_hostility(self):
        policy = {"factions": {"Thieves": {"standing": 1, "hostile_below": 0}}}
        state = _state(_world(_template(encounter={"category": "faction_attack", "faction": "Thieves"})),
                       start="Pass", **policy)
        with patch.dict(config._config, {"encounters": {**config.get("encounters"), **policy}}):
            self.assertFalse(encounters.hostile(state, "Thieves"))
            self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "faction_not_hostile")
            state.faction_shift["Thieves"] = -2                                      # the story turned them
            self.assertTrue(encounters.hostile(state, "Thieves"))
            self.assertTrue(state.holds({"faction_hostile": "Thieves"}))
            self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "eligible")

    def test_an_npc_assault_needs_its_npc_here(self):
        brute = Character("Brute", ["glacier"], unique=True, actions=[
            Action("(Brute) Nod", [Outcome("A grunt.")], character_name="Brute", source="npc", owner="Brute")])
        state = _state(_world(_template(encounter={"category": "npc_assault", "npc": "Brute"}), npcs=(brute,)),
                       start="Pass")
        state.npc_homes["Brute"] = "Ridge"
        _ids(state)
        self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "npc_absent")

    def test_the_policy_switches(self):
        state = _state(_world(_template()), start="Pass", enabled=False)
        _ids(state)
        self.assertEqual((encounters.explain(state)["gate"], _attacked(state)), ("disabled", False))
        off = {**config.get("encounters")["categories"], "ambush": {"enabled": False, "weight": 1}}
        state = _state(_world(_template()), start="Pass", categories=off)
        self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "category_off")
        rare = _state(_world(_template()), start="Pass", rate=0.0)
        _ids(rare)
        self.assertFalse(_attacked(rare))


class PacingTests(unittest.TestCase):
    """Rare and spread out: no attack spam, no repeated identical ambush, no loop of losses."""

    def _many(self, **policy):
        return _template(encounter={"max_per_story": 5, "cooldown": 0}, **policy)

    def test_separation_caps_and_no_repeat(self):
        state = _state(_world(self._many()), start="Pass", min_separation=3, max_per_session=2, no_repeat_last=False)
        _ids(state)
        self.assertTrue(_attacked(state))
        _take(state, "event:thieves:fight-them")
        for _ in range(2):
            _take(state, "location:pass:rest")
            self.assertFalse(_attacked(state))                                         # min_separation
        self.assertEqual(encounters.explain(state)["gate"], "separation")
        _take(state, "location:pass:rest")
        _ids(state)
        self.assertTrue(_attacked(state))                                              # 3 turns later
        _take(state, "event:thieves:fight-them")
        for _ in range(6):  # back and forth (resting again and again would be filtered as a loop)
            _take(state, "go:ridge" if state.current.name == "Pass" else "go:pass")
        self.assertEqual(len(state.dynamic_encounters), 2)
        self.assertEqual(encounters.explain(state)["gate"], "cap_reached")             # max_per_session
        state = _state(_world(self._many()), start="Pass", min_separation=0)
        _ids(state)
        _take(state, "event:thieves:fight-them")
        self.assertEqual(encounters.explain(state)["candidates"][0]["status"], "repeat_last")
        once = _state(_world(_template()), start="Pass", min_separation=0, no_repeat_last=False)
        _ids(once)
        _take(once, "event:thieves:fight-them")
        self.assertEqual(encounters.explain(once)["candidates"][0]["status"], "max_per_story")

    def test_a_hostile_npc_s_encounter_counts_and_comes_first(self):
        wolf_event = Event("wolf", title="A wolf", actions=[
            Action("Fight the wolf", [Outcome("It flees.", tags=["combat", "success"], remove_npc=True)],
                   source="event", owner="wolf"),
            Action("Back away", [Outcome("You back away.")], source="event", owner="wolf")])
        wolf = Character("Wolf", ["glacier"], encounter_event="wolf", actions=[])
        state = _state(_world(_template(), wolf_event, npcs=(wolf,)), start="Pass", min_separation=4)
        _ids(state)
        self.assertEqual(state.active_event.name, "wolf")                              # the mandatory one first
        _take(state, "event:wolf:fight-the-wolf")
        _take(state, "location:pass:rest")
        self.assertEqual(encounters.explain(state)["gate"], "separation")              # it was an attack too

    def test_what_must_not_be_interrupted(self):
        state = _state(_world(_template()), start="Pass")
        cases = (("conversation", lambda s: setattr(s, "talking_to", "Someone")),
                 ("climax", lambda s: s.__dict__.setdefault("climax_state", {}).update(armed="Boss")),
                 ("finale", lambda s: setattr(s, "in_finale", lambda: True)),
                 ("held", lambda s: setattr(s, "_location_is_locked", lambda *a: True)),
                 ("choice_pending", lambda s: s.side_quests.update(q={"status": "offered"})))
        for code, make in cases:
            with self.subTest(code=code):
                probe = copy.deepcopy(state)
                make(probe)
                self.assertEqual(encounters.explain(probe)["gate"], code)
        with patch.dict(config._config, {"encounters": {**config.get("encounters"), "in_finale": True}}):
            allowed = _state(_world(_template()), start="Pass")
        allowed.in_finale = lambda: True
        self.assertIsNone(encounters.explain(allowed)["gate"])                          # unless allowed


class ChoiceAndLossTests(unittest.TestCase):
    def test_the_choices_follow_their_conditions(self):
        state = _state(_world(_template()), start="Pass")
        state.coins = 1
        ids = _ids(state)
        self.assertIn("event:thieves:give-in", ids)                                    # always a way out
        self.assertNotIn("event:thieves:pay-them", ids)                                # not enough coins
        rich = _state(_world(_template()), start="Pass")
        rich.coins = 10
        self.assertIn("event:thieves:pay-them", _ids(rich))

    def test_losing_does_not_end_the_story_and_the_next_attack_waits(self):
        deadly = Action("Fight them", [Outcome("A blade finds you.", tags=["combat", "failure"], health_change=-99)],
                        source="event", owner="thieves")
        state = _state(_world(_template(fight=deadly, encounter={"max_per_story": 3, "cooldown": 0,
                                                                  "on_loss": {"text": "They leave you in the snow."}})),
                       start="Pass", min_separation=1, after_loss_separation=5, no_repeat_last=False)
        _ids(state)
        _take(state, "event:thieves:fight-them")
        self.assertFalse(state.ended)
        self.assertEqual(state.health, 1)                                              # spared
        self.assertIn("[SPARED] You go down, but not for good: The thieves leaves you bruised, at 1 health.",
                      state.history)
        self.assertIn("They leave you in the snow.", state.history)
        self.assertEqual(state.dynamic_encounters[-1]["result"], "failure")
        state.health_player.health = state.max_health
        for _ in range(3):
            _take(state, "location:pass:rest")
        self.assertEqual(encounters.explain(state)["gate"], "recovering")              # longer after a loss
        lethal = _state(_world(_template(fight=deadly)), start="Pass", lethal=True)
        _ids(lethal)
        _take(lethal, "event:thieves:fight-them")
        self.assertTrue(lethal.ended)                                                  # only when allowed

    def test_giving_in_counts_as_a_failure_in_any_event(self):
        """story_rules.failure_counts: an action with no check that tells a failure outcome (giving
        in, being caught) is a failure, in an encounter or in any other event; false: as before."""
        for counts, result in ((True, "failure"), (False, "success")):
            with self.subTest(failure_counts=counts):
                toll = Event("toll", title="The toll", tags=["threat"], actions=[
                    Action("Give in", [Outcome("They take your purse.", tags=["failure"])], source="event",
                           owner="toll"),
                    Action("Walk on", [Outcome("You walk on.", tags=["success"])], source="event", owner="toll")])
                with patch.dict(config._config, {"story_rules": {**config.get("story_rules"), "failure_counts": counts}}):
                    state = _state(_world(toll), start="Ridge", enabled=False)
                state._enter_event("toll")
                _take(state, "event:toll:give-in")
                self.assertEqual(state.event_log[-1]["result"], result)
                self.assertIn("RESULT: They take your purse.", state.history)         # told either way

    def test_a_lost_encounter_never_leaves_the_player_in_a_cell(self):
        """A loss that sends the player to a cell: let out after encounters.captive_release turns,
        even with the director (and its release_after) off; 0 leaves it to the director."""
        caught = Action("Fight them", [Outcome("They drag you to their cell.", tags=["combat", "failure"],
                                               send_to="Cell")], source="event", owner="thieves")

        def story(**policy):
            world = _world(_template(fight=caught))
            cell = Location("Cell", ["prison"])
            cell.actions = [_rest("Cell")]
            world.add_location(cell)
            world.connect("Cell", "Ridge")
            held = {"location_constraints": {**(config.get("location_constraints") or {}),
                                             "Cell": {"requires_escape": True, "lock_when": "sent"}},
                    "director": {**config.get("director"), "enabled": False}}
            with patch.dict(config._config, held):
                state = _state(world, start="Pass", **policy)
            _ids(state)
            _take(state, "event:thieves:fight-them")
            return state

        state = story(captive_release=3, min_separation=0, after_loss_separation=0)
        self.assertEqual((state.current.name, state.confined_in), ("Cell", "Cell"))   # locked in
        self.assertEqual(state.dynamic_encounters[-1]["result"], "failure")
        self.assertEqual(encounters.explain(state)["gate"], "held")                   # no attack in the cell
        for _ in range(4):
            if state.confined_in is None:
                break
            _take(state, "location:cell:rest")
        self.assertIsNone(state.confined_in)                                          # let out
        self.assertIn("Cell", state.unlocked_locations)
        self.assertFalse(state.ended)
        self.assertIsNone(state.dynamic_encounters[-1]["held_in"])
        kept = story(captive_release=0)
        for _ in range(8):
            _take(kept, "location:cell:rest")
        self.assertEqual(kept.confined_in, "Cell")                                    # off: the cell's own way out

    def test_difficulty_makes_its_checks_harder(self):
        checked = Action("Fight them", [Outcome("Won.", tags=["combat", "success"]),
                                        Outcome("Lost.", tags=["combat", "failure"], on_check="fail")],
                         source="event", owner="thieves", skill_check={"stat": "attack"})
        state = _state(_world(_template(fight=checked, encounter={"difficulty": 1.5})), start="Pass")
        options = state.player_options()
        fight = next(o for o in options if o.name == "Fight them")
        self.assertEqual(encounters.check_penalty(state, fight), 5)                    # (1.5 - 1) x 10
        hint = state.check_hint(fight)
        plain = _state(_world(_template(fight=checked)), start="Pass")
        plain_fight = next(o for o in plain.player_options() if o.name == "Fight them")
        self.assertEqual(plain.check_hint(plain_fight)["bonus"] - hint["bonus"], 5)


class StoryHookTests(unittest.TestCase):
    def test_side_quests_standing_and_consequences(self):
        hideout = SideQuest("hideout", {"location": "Ridge"}, title="The hideout",
                            start={"event_done": "thieves"}, accept="choice")
        event = _template(encounter={"faction": "Thieves", "on_win": {
            "text": "A scrap of map falls from a fleeing thief.", "standing": {"Thieves": -1},
            "side_quest": "hideout"}})
        world = _world(event)
        world.add_side_quest(hideout)
        state = _state(world, start="Pass")
        _ids(state)
        _take(state, "event:thieves:fight-them")
        self.assertIn("A scrap of map falls from a fleeing thief.", state.history)
        self.assertEqual(state.side_quests["hideout"]["status"], "offered")             # a hook, put to the player
        self.assertEqual(state.faction_shift, {"Thieves": -1.0})

    def test_no_reward_farming(self):
        event = _template(encounter={"max_per_story": 3, "cooldown": 0,
                                     "on_win": {"text": "A pouch.", "effects": [{"change_coins": 5}]}})
        state = _state(_world(event), start="Pass", min_separation=0, no_repeat_last=False)
        gains = []
        while len(gains) < 3:
            _ids(state)
            if not _attacked(state):
                _take(state, "location:pass:rest")
                continue
            before = state.coins
            _take(state, "event:thieves:fight-them")
            gains.append(state.coins - before)
        self.assertEqual(gains, [5, 0, 0])                                             # paid once (rewards_once)
        self.assertEqual(state.history.count("A pouch."), 3)                           # told each time
        self.assertEqual(state.encounter_rewards, ["thieves"])


class IntegrationTests(unittest.TestCase):
    def _played(self, seed):
        state = _state(_world(_template(encounter={"max_per_story": 2, "cooldown": 0})), start="Camp", seed=seed,
                       rate=0.5, min_separation=2, no_repeat_last=False)
        for step in ("go:pass", "location:pass:rest", "location:pass:rest", "location:pass:rest", "go:ridge",
                     "go:pass", "location:pass:rest"):
            options = state.player_options()
            ids = choices.assign_ids(options, state)
            if _attacked(state):
                state.take_option(next(o for o, i in zip(options, ids) if i == "event:thieves:give-in"))
                continue
            state.take_option(next((o for o, i in zip(options, ids) if i.startswith(step)), options[0]))
        return state

    def test_deterministic_replay_and_reading_changes_nothing(self):
        first, second = self._played(5), self._played(5)
        self.assertEqual(first.dynamic_encounters, second.dynamic_encounters)
        self.assertEqual(first.history, second.history)
        self.assertEqual(first.rng.getstate(), second.rng.getstate())
        self.assertTrue(any(self._played(seed).dynamic_encounters for seed in range(6)))
        state = _state(_world(_template()), start="Pass")
        before = (json.dumps(snapshot.dump(state), sort_keys=True), state.rng.getstate(), random.getstate())
        encounters.explain(state)
        trace = decision_trace(state)
        self.assertIn("candidates", trace["encounters"])
        self.assertEqual((json.dumps(snapshot.dump(state), sort_keys=True), state.rng.getstate(), random.getstate()),
                         before)

    def test_saves_journal_and_transcript(self):
        state = _state(_world(_template()), start="Pass")
        _ids(state)
        _take(state, "event:thieves:give-in")
        saved = json.loads(json.dumps(snapshot.dump(state)))       # plain data (the round trip: ExampleWorldTests)
        self.assertEqual(saved["attributes"]["dynamic_encounters"], state.dynamic_encounters)
        entry = state.journal()[-1]
        self.assertEqual((entry["encounter"]["category"], entry["encounter"]["result"]), ("ambush", "failure"))
        self.assertIn("[AMBUSH] The thieves!", state.get_full_story())
        from core_engine import resume

        params = json.loads(json.dumps(resume.parameters(state)))["encounters"]   # the UNP's parameters
        rebuilt = _state(_world(_template()), start="Pass")
        encounters.restore(rebuilt, params)
        self.assertEqual(rebuilt.dynamic_encounters, [{key: record.get(key) for key in (
            "turn", "event", "category", "location", "faction", "npc", "result")} for record in state.dynamic_encounters])

    def test_templates_are_checked(self):
        world = {"characters": [{"name": "Brute"}], "side_quests": {"q": {}}}
        good = {"actions": [{"name": "Fight", "skill_check": {"stat": "attack"}}, {"name": "Flee"}],
                "encounter": {"category": "ambush", "on_win": {"side_quest": "q", "standing": {"X": 1}}}}
        self.assertEqual(event_policy_errors(good, "Event 'e'", world), [])
        for data, words in (({**good, "encounter": {"category": "picnic"}}, "category"),
                            ({**good, "encounter": {"category": "ambush", "colour": 1}}, "unknown encounter field"),
                            ({**good, "trigger": "random", "chance": 0.1}, "encounter director"),
                            ({**good, "actions": [{"name": "Fight"}]}, "two choices"),
                            ({**good, "actions": [{"name": "A", "skill_check": {"stat": "attack"}},
                                                  {"name": "B", "skill_check": {"stat": "stealth"}}]},
                             "without a skill check"),
                            ({**good, "encounter": {"category": "ambush", "npc": "Nobody"}}, "not an NPC"),
                            ({**good, "encounter": {"category": "ambush", "on_loss": {"side_quest": "zz"}}},
                             "not a side quest"),
                            ({**good, "encounter": {"category": "ambush", "stages": ["dawn"]}}, "stages")):
            with self.subTest(data=data):
                errors = event_policy_errors(data, "Event 'e'", world)
                self.assertTrue(any(words in error for error in errors), errors)


class ExampleWorldTests(unittest.TestCase):
    """The two example encounters, under their worlds' own configuration."""

    def _world_state(self, path, start, **policy):
        cfg, errors, _ = store.complete_config(yaml.safe_load(_read(path)))
        self.assertEqual(errors, [])
        cfg["encounters"] = {**cfg["encounters"], **policy}
        cfg["unexpected_events"]["enabled"] = False
        genome = Genome(rng=random.Random(2))
        context = EngineContext(cfg, genome=genome)
        with context.bind():
            return GameState(build_world(genome), genome, start=start, rng=random.Random(4), context=context), context

    def test_frozen_monastery_thieves_on_the_glacier_route(self):
        state, context = self._world_state("examples/frozen_monastery.yaml", "Glacier Path", **FORCED,
                                           min_separation=0)
        with context.bind():
            seen = []
            for _ in range(8):
                seen.append(encounters.explain(state)["candidates"][0]["status"])
                options = state.player_options()
                ids = choices.assign_ids(options, state)
                if _attacked(state):
                    self.assertTrue({"event:glacier-thieves:fight-them-off", "event:glacier-thieves:surrender-your-pack",
                                     "event:glacier-thieves:hold-the-narrow-ledge"} <= set(ids))
                    self.assertNotIn("event:glacier-thieves:call-for-the-monks", ids)  # trust not earned
                    state.take_option(next(o for o, i in zip(options, ids) if i.endswith("surrender-your-pack")))
                    break
                stay = next((o for o, i in zip(options, ids) if i.startswith(("location:glacier", "event:"))), options[0])
                state.take_option(stay)
            self.assertEqual(state.dynamic_encounters[-1]["event"], "glacier_thieves")
            restored = snapshot.load(json.loads(json.dumps(snapshot.dump(state))))   # under its own configuration
            self.assertEqual((restored.dynamic_encounters, restored.faction_shift, restored.side_quests),
                             (state.dynamic_encounters, state.faction_shift, state.side_quests))
            self.assertIn("too_early", seen)                                           # min_turn: 3
            self.assertEqual(state.dynamic_encounters[-1]["result"], "failure")
            self.assertEqual(state.side_quests["the_thieves_hideout"]["status"], "offered")  # the hook
            self.assertTrue(encounters.hostile(state, "Glacier Thieves"))

    def test_orbital_station_drones_answer_the_alert(self):
        state, context = self._world_state("examples/orbital_station.yaml", "Maintenance Shafts", opening_turns=0,
                                           max_chance=1.0)
        with context.bind():
            row = encounters.explain(state)["candidates"][0]
            self.assertEqual((row["event"], row["status"]), ("security_sweep", "alert"))  # nobody looks for you yet
            state.alert = 2
            low = encounters.explain(state)["candidates"][0]["chance"]
            state.alert = 5
            high = encounters.explain(state)["candidates"][0]["chance"]
            self.assertGreater(low, 0)
            self.assertGreater(high, low)                                               # the higher, the likelier
        forced, context = self._world_state("examples/orbital_station.yaml", "Maintenance Shafts", **FORCED)
        with context.bind():
            forced.alert = 3
            ids = choices.assign_ids(forced.player_options(), forced)
            self.assertEqual(forced.active_event.name, "security_sweep")
            self.assertIn("event:security-sweep:raise-your-hands", ids)
            self.assertNotIn("event:security-sweep:flash-the-data-shard-as-clearance", ids)  # no shard carried
            self.assertIn("[PATROL] A security drone sweep: a patrol bars your way in the Maintenance Shafts.",
                          forced.history)


if __name__ == "__main__":
    unittest.main()
