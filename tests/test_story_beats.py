"""The story's beats (core_engine/story_beats.py) and NPC roles: who matters how much (found
later, harder to beat), the climax every quest ends in, failures that show another way and
refunds, variation in what is told again, fast travel and first trips, the alert level and what
a failure costs, secrets, consequences and what happened last time, and the fitness they earn."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import main as engine
from backend import sessions
from core_engine import Action, Character, Event, GameState, Genome, Location, Mission, Outcome, WorldGraph
from core_engine.combat import CheckResult
from core_engine.fitness import _fitness_breakdown
from core_engine.story_beats import failure_lint
from core_engine.variables import VariableStore
from utils.config_loader import config


def _genome():
    random.seed(11)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _rolls(*passes):
    results = iter(passes)

    def roll(combat, stat, modifier=0, inventory=(), step=0):
        passed = next(results)
        return CheckResult(stat, 1 if passed else 100, combat.target(stat, modifier, inventory, step), passed, step)

    return patch("core_engine.combat.CombatSystem.roll", roll)


def _set(section, **values):
    """Patch a top-level config section with these values (the rest kept)."""
    return patch.dict(config._config, {section: {**copy.deepcopy(config.get(section) or {}), **values}})


def _names(state):
    return [action.name for action in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


def _place(name, tags, *actions):
    location = Location(name, list(tags))
    location.actions = [Action(text, [outcome], repeat="always", source="location", owner=name) for text, outcome in actions]
    return location


def _city(*missions, thief_tags=("market", "threat")):
    """Market - Alley, a thief and a vendor at the market."""
    world = WorldGraph()
    market = _place("Market", ["urban", "market"], ("Browse the stalls", Outcome("You browse.", tags=["neutral"])))
    alley = _place("Alley", ["urban", "dark"], ("Search the alley", Outcome("Nothing but rats.", tags=["neutral"])))
    market.connect(alley)
    world.add_location(market)
    world.add_location(alley)
    world.add_character(Character(
        "Thief", list(thief_tags), unique=True, known_locations=["Market"], descriptions=["A wiry figure."],
        actions=[Action("(Thief) Confront the thief", [Outcome("You step in.")], character_name="Thief",
                        triggers_event="confront")]))
    world.add_character(Character(
        "Vendor", ["market"], unique=True, known_locations=["Market"], descriptions=["A vendor."],
        actions=[Action("(Vendor) Haggle", [Outcome("A fair price.", tags=["social"])], character_name="Vendor",
                        repeat="always", source="npc", owner="Vendor")]))
    world.add_event(Event("confront", actions=[
        Action("Fight the thief", [Outcome("They flee.", tags=["combat", "success"], remove_npc=True)])]))
    for mission in missions:
        world.add_mission(mission)
    return world


def _roles(**changes):
    """npc_roles with some roles changed, e.g. nemesis={...}."""
    roles = copy.deepcopy(config.get("npc_roles.roles"))
    for name, settings in changes.items():
        roles[name] = settings
    return _set("npc_roles", roles=roles)


class _Beats(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.dict(config._config["unexpected_events"], {"enabled": False}),
                        patch.dict(config._config["game_state"], {"arrest_location": "Alley"}),
                        patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-beats-")})):
            patcher.start()
            self.addCleanup(patcher.stop)


class RoleTests(_Beats):
    def test_the_quest_target_is_the_nemesis_and_bystanders_are_easier(self):
        state = GameState(_city(Mission("thief", {"npc_removed": "Thief"})), _genome(), start="Market",
                          rng=random.Random(1))
        self.assertEqual(state.npc_roles, {"Thief": "nemesis", "Vendor": "bystander"})
        self.assertEqual((state.importance("Thief"), state.importance("Vendor")), (3, 0))
        thief, vendor = (next(a for c in state.world.characters if c.name == name for a in c.actions)
                         for name in ("Thief", "Vendor"))
        self.assertEqual(state._check_bonus(thief, "persuasion"), -15)  # harder to beat
        self.assertEqual(state._check_bonus(vendor, "persuasion"), 10)  # easier
        # Without the quest, a unique threat is a lieutenant; an NPC's own role wins over the rules.
        world = _city()
        world.characters[1].role = "patron"
        state = GameState(world, _genome(), start="Market", rng=random.Random(1))
        self.assertEqual(state.npc_roles, {"Thief": "lieutenant", "Vendor": "patron"})

    def test_someone_who_matters_is_found_later_and_revealed(self):
        with _roles(nemesis={"label": "nemesis", "importance": 3, "check_modifier": -15, "reveal_turns": 2}):
            state = GameState(_city(Mission("thief", {"npc_removed": "Thief"})), _genome(), start="Market",
                              rng=random.Random(1))
            self.assertNotIn("(Thief) Confront the thief", _names(state))  # out of sight at first
            self.assertIn("(Vendor) Haggle", _names(state))
            _choose(state, "Move to Alley")
            _choose(state, "Return to Market")
            self.assertIn("(Thief) Confront the thief", _names(state))
            self.assertIn("[REVEAL] The Thief, the nemesis of this story: A wiry figure.", state.history)
            self.assertIn("reveal", [beat["kind"] for beat in state.beat_log])
            self.assertEqual(next(p for p in state.people_here() if p["name"] == "Thief")["importance"], 3)

    def test_a_lead_names_someone_who_matters(self):
        with _set("npc_leads", every_nth_interaction=1):
            state = GameState(_city(Mission("thief", {"npc_removed": "Thief"})), _genome(), start="Market",
                              rng=random.Random(1))
            _choose(state, "(Vendor) Haggle")
        self.assertIn("[LEAD] The Vendor speaks of the Thief, a nemesis in all this: look for them at the Market.",
                      state.history)
        self.assertIn("Thief", state.revealed_npcs)
        self.assertTrue(state._role_revealed(state.world.characters[0]))

    def test_beating_someone_who_matters_is_a_triumph_with_consequences(self):
        state = GameState(_city(), _genome(), start="Market", rng=random.Random(1))
        state._remove_npc("Thief")
        self.assertIn("[TRIUMPH] You have beaten the Thief, the lieutenant.", state.history)
        self.assertEqual([d["npc"] for d in state.defeated], ["Thief"])
        self.assertEqual(state.consequences_due[0]["kind"], "defeated")
        state.actions_taken += 2
        coins = state.coins
        state._tell_consequences()
        self.assertTrue(any(line.startswith("[CONSEQUENCE] ") and "Thief" in line for line in state.history))
        self.assertEqual(state.coins, coins + 3)
        self.assertEqual(state.consequences_due, [])


class ClimaxTests(_Beats):
    def _state(self, goal=None):
        world = _city(Mission("shop", goal or {"action": "Browse the stalls"}, title="Do the shopping"))
        world.locations["Market"].actions.append(Action(
            "Overhear a rumour", [Outcome("A rumour about a hidden door.", tags=["setup_clue"])], repeat="once",
            source="location", owner="Market"))
        return GameState(world, _genome(), start="Market", rng=random.Random(1))

    def test_a_quest_ends_in_a_confrontation(self):
        with _set("climax", min_turns=0):
            state = self._state()
            _choose(state, "Overhear a rumour")
            _choose(state, "Browse the stalls")
            self.assertFalse(state.won)  # the last step is a confrontation
            self.assertEqual(state.active_event.title, "Showdown: Do the shopping")
            self.assertIn("[CLIMAX] Do the shopping: it all comes down to this.", state.history)
            names = _names(state)
            self.assertIn("Face the Thief head-on", names)
            self.assertIn("Talk the Thief down", names)
            with _rolls(True):
                _choose(state, "Face the Thief head-on")
        self.assertTrue(state.won)
        self.assertIn("[CLIMAX] You prevail: Do the shopping is yours.", state.history)
        self.assertIn('[REVELATION] At last it makes sense, what you learned at the Market: '
                      '"A rumour about a hidden door"', state.history)
        self.assertEqual(state._open_clues(), [])  # every open thread closed
        self.assertTrue(any(b["kind"] == "climax" and b["won"] for b in state.beat_log))
        self.assertEqual(_fitness_breakdown(state)["climax"], 15)

    def test_a_setback_brings_it_back_and_it_is_won_at_a_price(self):
        with _set("climax", min_turns=0, max_attempts=2):
            state = self._state()
            _choose(state, "Browse the stalls")
            health = state.health
            with _rolls(False):
                _choose(state, "Outwit the Thief")
            self.assertFalse(state.won)
            self.assertIn("[SETBACK] The Thief throws you back. Catch your breath; this is not over.", state.history)
            self.assertLess(state.health, health)
            _choose(state, "Browse the stalls")  # the goal still holds: the climax comes back
            self.assertTrue(state.active_event.title.startswith("Showdown"))
            with _rolls(False):
                _choose(state, "Outwit the Thief")
        self.assertTrue(state.won)  # never lost at its last step
        self.assertIn("[CLIMAX] Bloodied and spent, you prevail at last; Do the shopping cost you dearly.", state.history)
        self.assertGreaterEqual(state.health, 1)
        self.assertEqual(_fitness_breakdown(state)["climax"], 7.5)

    def test_an_ally_can_be_called_and_a_short_quest_needs_no_showdown(self):
        with _set("climax", min_turns=0):
            state = self._state()
            state.allies = {"Vendor"}
            _choose(state, "Browse the stalls")
            self.assertIn("Call on the Vendor", _names(state))
        with _set("climax", min_turns=5):
            state = self._state()
            _choose(state, "Browse the stalls")
            self.assertTrue(state.won)  # won at once: too short for a showdown

    def test_beating_the_nemesis_is_the_climax(self):
        with _set("climax", min_turns=0), _roles(nemesis={"label": "nemesis", "importance": 3}):
            state = self._state({"npc_removed": "Thief"})
            _choose(state, "(Thief) Confront the thief")
            _choose(state, "Fight the thief")
        self.assertTrue(state.won)
        self.assertFalse(any(line.startswith("[CLIMAX] Do the shopping: it all") for line in state.history))
        self.assertIn("[TRIUMPH] You have beaten the Thief, the nemesis.", state.history)
        self.assertTrue(any(b["kind"] == "climax" for b in state.beat_log))


class FailureTests(_Beats):
    def _state(self, *actions, **director):
        world = _city(Mission("go", {"location": "Alley"}))
        world.locations["Market"].actions.extend(actions)
        state = GameState(world, _genome(), start="Market", rng=random.Random(1))
        return state

    def test_a_failure_shows_another_way_or_is_a_dead_end(self):
        lock = Action("Pick the lock", [Outcome("The lock opens.", tags=["success"])], skill_check={"stat": "stealth"},
                      repeat="always", source="location", owner="Market")
        with _set("director", **{**config.get("director"), "fail_forward": 0.0, "another_way": 1.0}):
            state = self._state(lock)
            with _rolls(False):
                _choose(state, "Pick the lock")
        self.assertIn("[ANOTHER WAY] The failure teaches you something: the answer may lie at the Alley.", state.history)
        with _set("director", **{**config.get("director"), "fail_forward": 0.0, "another_way": 0.0}):
            state = self._state(lock)
            with _rolls(False):
                _choose(state, "Pick the lock")
        self.assertEqual([b["kind"] for b in state.beat_log if b["kind"] in ("dead_end", "another_way")], ["dead_end"])
        self.assertLess(_fitness_breakdown(state)["failures_led_on"], 0)

    def test_a_failed_paid_attempt_returns_part_of_the_price_and_the_cost_shows_before(self):
        bribe = Action("Bribe the clerk", [
            Outcome("The clerk takes it.", tags=["success"], on_check="pass"),
            Outcome("The clerk keeps the coins and shouts.", tags=["failure"], on_check="fail", health_change=-1,
                    send_to="Alley"),
        ], required_coins=10, skill_check={"stat": "persuasion"}, repeat="always", source="location", owner="Market")
        state = self._state(bribe)
        state.coins = 20
        hint = state.check_hint(next(a for a in state.ranked_available_actions() if a.name == "Bribe the clerk"))
        self.assertEqual(hint["risk"], {"health": -1, "coins": -5, "sent_to": "Alley"})  # half the price comes back
        with _rolls(False):
            _choose(state, "Bribe the clerk")
        self.assertEqual(state.coins, 15)
        self.assertIn("[REFUND] Not everything is lost: you get 5 coins back (Balance: 15).", state.history)
        self.assertEqual(sessions.check_text(hint), "persuasion {}% (fail: -1 health, -5 coins, sent to the Alley)".format(
            hint["chance"]))

    def test_the_lint_lists_failures_a_world_leaves_as_dead_ends(self):
        world = _city()
        world.locations["Market"].actions.append(Action(
            "Pick the lock", [Outcome("It opens.", tags=["stealth", "success"])], skill_check={"stat": "stealth"}))
        self.assertEqual(failure_lint(world), [{"action": "Pick the lock", "owner": "Market",
                                                "why": "a failed check tells nothing"}])
        world.add_event(Event("watch", tags=["complication", "stealth"]))
        self.assertEqual(failure_lint(world), [])  # a complication fits it now
        response = TestClient(api.api).get("/v1/lint/failures")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json()["dead_ends"], list)


class VariationTests(_Beats):
    def test_variants_then_lines_that_know_it_happened_before(self):
        sea = Outcome("Waves.", tags=["neutral"])
        sea.variants = ["More waves."]
        world = _city()
        world.locations["Market"].actions.append(Action("Watch the sea", [sea], repeat="always", source="location",
                                                        owner="Market"))
        state = GameState(world, _genome(), start="Market", rng=random.Random(1))
        for _ in range(4):
            _choose(state, "Watch the sea")
        told = [line for line in state.history if line.startswith("RESULT:")]
        self.assertEqual(told, ["RESULT: Waves.", "RESULT: More waves.", "RESULT: Waves. It goes much as it did before.",
                                "RESULT: More waves. The same again; people here are starting to know your face."])
        self.assertEqual([turn["told"] for turn in state.turn_log[:2]], ["Waves.", "More waves."])
        self.assertEqual(state.turn_log[1]["outcome"], "Waves.")  # what fitness reads stays the outcome itself

    def test_what_worked_before_is_harder(self):
        state = GameState(_city(), _genome(), start="Market", rng=random.Random(1))
        lock = Action("Pick the lock", [Outcome("It opens.")], skill_check={"stat": "stealth"}, source="location")
        state.success_counts["Pick the lock"] = 2
        self.assertEqual(state._repeat_penalty(lock), 10)
        state.success_counts["Pick the lock"] = 9
        self.assertEqual(state._repeat_penalty(lock), 20)  # up to max_repeat_penalty

    def test_shipped_outcomes_have_variants(self):
        world = engine.build_world(_genome())
        outcomes = [o for loc in world.locations.values() for a in loc.actions for o in a.outcomes]
        self.assertTrue(any(o.variants for o in outcomes))


class TravelTests(_Beats):
    def _line(self):
        world = WorldGraph()
        names = ["Gate", "Road", "Bridge", "Harbor"]
        for name in names:
            world.add_location(_place(name, ["harbor"] if name == "Harbor" else ["road"],
                                      (f"Rest at the {name}", Outcome(f"You rest at the {name}.", tags=["neutral"]))))
        world.locations["Gate"].actions.append(Action("Dig by the gate", [Outcome("Something glints.", tags=["discovery"])],
                                                      repeat="once", source="location", owner="Gate"))
        world.locations["Gate"].actions[0].repeat = "once"
        for a, b in zip(names, names[1:]):
            world.locations[a].connect(world.locations[b])
        world.add_character(Character("Smuggler", ["harbor", "threat"], unique=True, known_locations=["Harbor"],
                                      actions=[Action("(Smuggler) Talk", [Outcome("Hm.")], character_name="Smuggler")]))
        return world

    def test_fast_travel_along_known_ways_does_not_count(self):
        state = GameState(self._line(), _genome(), start="Gate", rng=random.Random(1))
        for name in ("Road", "Bridge", "Harbor"):
            _choose(state, f"Move to {name}")
        limit = state._max_actions()
        self.assertIn("Travel by known ways to the Gate", _names(state))
        _choose(state, "Travel by known ways to the Gate")
        self.assertEqual(state.current.name, "Gate")
        self.assertEqual(state.path[-3:], ["Bridge", "Road", "Gate"])  # every step on the way
        self.assertIn("RESULT: By ways you know, through the Bridge, the Road, you reach the Gate.", state.history)
        self.assertEqual((state.free_turns, state._max_actions()), (1, limit + 1))
        self.assertTrue(state.turn_log[-1]["fast_travel"])
        _choose(state, "Return to Road")
        _choose(state, "Return to Bridge")
        self.assertFalse(any(name.startswith("Travel by known ways") for name in _names(state)))  # not again so soon

    def test_a_first_trip_has_a_scene_and_word_of_who_is_there(self):
        trip = {"enabled": True, "chance": 1.0, "road_word": True, "by_tag": {"harbor": [{"text": "Gulls.", "coins": 2}]},
                "default": []}
        with _set("travel", first_trip=trip):
            state = GameState(self._line(), _genome(), start="Bridge", rng=random.Random(1))
            coins = state.coins
            _choose(state, "Move to Harbor")
        self.assertIn("[ON THE WAY] Gulls.", state.history)
        self.assertEqual(state.coins, coins + 2)
        self.assertIn("[ON THE WAY] Word on the road: The Smuggler is to be found in the Harbor.", state.history)
        self.assertIn("Smuggler", state.revealed_npcs)


class StakesTests(_Beats):
    def test_the_alert_rises_warns_and_ends_in_a_manhunt(self):
        world = _city()
        world.add_event(Event("guards_alerted", title="Guards!", tags=["guards"], actions=[
            Action("Run", [Outcome("You run.")])]))
        state = GameState(world, _genome(), start="Market", rng=random.Random(1))
        fight = Action("Fight the watchman", [Outcome("Down he goes.", tags=["combat", "success"])], source="location")
        state._update_alert(fight, fight.outcomes[0], True)
        self.assertEqual(state.alert, 2)
        state._update_alert(fight, fight.outcomes[0], True)
        self.assertEqual(state.alert, 4)
        self.assertIn("[ALERT] The watch is on edge; guards look twice at strangers. (alert 4/6)", state.history)
        self.assertEqual(state.tension(), 0.7)
        self.assertEqual(state.stakes(), {"alert": 4, "max": 6, "warn_at": 4})
        self.assertEqual(state._alert_check_penalty(Action("Bribe the guard", [Outcome("x")])), 12)
        state._update_alert(fight, fight.outcomes[0], True)
        self.assertIn("[MANHUNT] The whole watch is after you!", state.history)
        self.assertEqual((state.alert, state._pending_event), (1, ("guards_alerted", None)))
        rest = Action("Rest", [Outcome("You rest.")], source="location")
        for _ in range(3):
            state._update_alert(rest, rest.outcomes[0], True)
        self.assertEqual(state.alert, 0)  # quiet turns calm it down
        self.assertEqual(sessions.play_state(state)["stakes"]["max"], 6)


class RevealTests(_Beats):
    def test_a_secret_comes_out_and_unlocks_an_action(self):
        world = _city()
        vendor = world.characters[1]
        vendor.secret = {"text": "{npc} is the fence.", "talks": 2, "effects": [{"change_coins": 4}]}
        vendor.actions.append(Action("(Vendor) Lean on the fence", [Outcome("They pay up.", tags=["social"])],
                                     character_name="Vendor", enabled_if={"secret_known": "Vendor"}))
        state = GameState(world, _genome(), start="Market", rng=random.Random(1))
        self.assertNotIn("(Vendor) Lean on the fence", _names(state))
        coins = state.coins
        _choose(state, "(Vendor) Haggle")
        _choose(state, "(Vendor) Haggle")
        self.assertIn("[SECRET] The Vendor is the fence.", state.history)
        self.assertEqual(state.coins, coins + 4)
        self.assertIn("(Vendor) Lean on the fence", _names(state))
        self.assertTrue(any(b["kind"] == "secret" for b in state.beat_log))

    def test_shipped_secrets(self):
        world = engine.build_world(_genome())
        merchant = next(c for c in world.characters if c.name == "Merchant")
        self.assertEqual(merchant.secret["talks"], 2)
        self.assertTrue(merchant.secret["effects"].checked)
        self.assertTrue(any(a.enabled_if for a in merchant.actions))

    def test_the_next_story_remembers_the_last(self):
        store = VariableStore(path=os.path.join(tempfile.mkdtemp(prefix="ane-memory-"), "variables.json"))
        world = _city(Mission("shop", {"action": "Browse the stalls"}, title="Do the shopping"))
        first = GameState(world, _genome(), start="Market", rng=random.Random(1), store=store, profile="ada")
        first.allies = {"Vendor"}
        _choose(first, "Browse the stalls")
        self.assertTrue(first.won and first.ended)
        second = GameState(_city(), _genome(), start="Market", rng=random.Random(2), store=store, profile="ada")
        _names(second)  # the first scene is set with the first options
        self.assertIn('[LAST TIME] Last time you completed "Do the shopping" in the Market; the Vendor became a friend.',
                      second.history)
        self.assertIn("[LAST TIME] The Vendor remembers you, and smiles.", second.history)
        # Simulations keep nothing.
        third = GameState(_city(), _genome(), start="Market", rng=random.Random(2))
        self.assertFalse(any(line.startswith("[LAST TIME]") for line in third.history))


class FitnessTests(_Beats):
    def test_beats_are_scored_and_terms_can_be_weighted(self):
        state = GameState(_city(), _genome(), start="Market", rng=random.Random(1))
        _choose(state, "Browse the stalls")
        state._remove_npc("Thief")  # a lieutenant (importance 2)
        state._beat("secret", npc="Vendor")
        breakdown = _fitness_breakdown(state)
        self.assertEqual(breakdown["npc_triumphs"], 8)
        self.assertEqual(breakdown["reveals"], 3)
        with patch.dict(config._config["fitness"], {"term_weights": {"npc_triumphs": 2.0}}):
            self.assertEqual(_fitness_breakdown(state)["npc_triumphs"], 16)


if __name__ == "__main__":
    unittest.main()
