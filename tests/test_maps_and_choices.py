"""The map (which places border which), the choices a player sees, quests to play, and the
people a player meets."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import random
import unittest
from unittest.mock import patch

import main as engine
from backend import sessions
from backend.errors import AuthoringError
from core_engine import Action, Character, GameState, Genome, Location, Mission, Object, Outcome, WorldGraph
from core_engine.builder import route_errors, route_map
from utils.config_loader import config


def _genome():
    random.seed(5)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _loc(name, *actions):
    location = Location(name, [name.lower()])
    location.actions = list(actions)
    return location


def _act(name, owner, *outcomes, **kwargs):
    return Action(name, list(outcomes), source=kwargs.pop("source", "location"), owner=owner, **kwargs)


def _line_world():
    """Gate - Square - Tower in a line, and a Cell one is only sent to (it borders the Square)."""
    world = WorldGraph()
    world.add_location(_loc(
        "Gate",
        _act("Run for the tower", "Gate", Outcome("You set off.", move_to="Tower")),
        _act("Hear of the tower", "Gate", Outcome("Someone mentions the tower.", reveal_location="Tower"),
             repeat="always"),
        _act("Get arrested", "Gate", Outcome("Seized!", send_to="Cell")),
        _act("Wait", "Gate", Outcome("You wait."), repeat="always"),
    ))
    world.add_location(_loc("Square", _act("Wait here", "Square", Outcome("You wait."), repeat="always")))
    world.add_location(_loc("Tower", _act("Look out", "Tower", Outcome("You look out."), repeat="always")))
    world.add_location(_loc(
        "Cell", _act("Pick the lock", "Cell", Outcome("Click.", tags=["escape"], move_to="Square"), repeat="always")))
    world.routes = {"Gate": {"Square"}, "Square": {"Gate", "Tower", "Cell"}, "Tower": {"Square"}, "Cell": {"Square"}}
    world.add_character(Character(
        "Porter", ["gate", "square"], known_locations=["Gate", "Square"],
        actions=[Action("(Porter) Carry me to the tower", [Outcome("He hoists you up.", move_to="Tower")],
                        character_name="Porter", source="npc", owner="Porter", repeat="always")],
    ))
    return world


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class MapTests(unittest.TestCase):
    def setUp(self):
        constraints = {"Cell": {"requires_escape": True, "lock_when": "sent", "entry": "sent", "escape_tags": ["escape"]}}
        for patcher in (
            patch.dict(config._config, {"location_constraints": constraints}),
            patch.dict(config._config["game_state"], {"arrest_location": "Cell", "auto_travel_on_spawn": False}),
            patch.dict(config._config["unexpected_events"], {"enabled": False}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = GameState(_line_world(), _genome(), start="Gate", rng=random.Random(0))

    def test_a_far_place_is_reached_through_the_places_between(self):
        _choose(self.state, "Run for the tower")
        self.assertEqual(self.state.current.name, "Square")
        self.assertIn("[ROUTE] No road runs straight from the Gate to the Tower; you set out by way of the Square.",
                      self.state.history)
        self.assertEqual(self.state.heading, "Tower")
        self.assertEqual(self.state._heading_step(), "Tower")  # the way on is known: next stop, the Tower
        self.assertIn("Move to Tower", _names(self.state))
        _choose(self.state, "Move to Tower")
        self.assertEqual(self.state.current.name, "Tower")
        self.assertIsNone(self.state.heading)

    def test_a_far_place_revealed_opens_the_way_toward_it_and_is_told_once(self):
        _choose(self.state, "Hear of the tower")
        self.assertIn("Square", self.state.current.connected)
        self.assertNotIn("Tower", self.state.current.connected)
        self.assertIn("[LEAD] The way to the Tower runs through the Square.", self.state.history)
        self.assertIn("Tower", self.state.world.locations["Square"].connected)  # the way is known
        _choose(self.state, "Hear of the tower")
        self.assertEqual(self.state.history.count("[LEAD] The way to the Tower runs through the Square."), 1)

    def test_travel_options_and_discoveries_follow_the_map(self):
        world = self.state.world
        for _ in range(5):
            found = world.spawn(self.state.current, state=self.state)
            self.assertIn(found.name if found else "Square", {"Square"})
        found = world.spawn(self.state.current, character=world.characters[0], state=self.state)
        self.assertIn(found.name if found else "Square", {"Square"})
        self.assertFalse(any(name in ("Move to Tower", "Move to Cell") for name in _names(self.state)))

    def test_people_cannot_take_you_where_this_place_does_not_lead(self):
        porter = self.state.world.characters[0]

        def meet_porter():
            self.state._refresh_scene()
            if porter not in self.state.current_inhabitants:
                self.state.current_inhabitants.append(porter)
            self.state.visible_inhabitants.add("Porter")

        meet_porter()
        self.assertNotIn("(Porter) Carry me to the tower", _names(self.state))  # at the Gate
        self.state._move_to("Square")
        meet_porter()
        self.assertIn("(Porter) Carry me to the tower", _names(self.state))  # the Square borders the Tower

    def test_a_cell_is_only_reached_by_being_sent(self):
        self.assertIsNone(self.state._route_path("Gate", "Cell"))
        self.assertFalse(self.state._move_to("Cell"))
        _choose(self.state, "Get arrested")
        self.assertEqual(self.state.current.name, "Cell")  # an arrest is the only jump
        _choose(self.state, "Pick the lock")
        self.assertEqual(self.state.current.name, "Square")
        self.assertNotIn("Move to Cell", _names(self.state))
        self.assertNotIn("Return to Cell", _names(self.state))

    def test_a_world_without_spawn_rules_has_no_map(self):
        world = WorldGraph()
        for name in ("A", "B"):
            world.add_location(_loc(name, _act("Go to B", name, Outcome("Off you go.", move_to="B"))))
        state = GameState(world, _genome(), start="A", rng=random.Random(0))
        self.assertIsNone(state.routes)
        _choose(state, "Go to B")
        self.assertEqual(state.current.name, "B")


class RouteConfigTests(unittest.TestCase):
    def test_routes_hold_both_ways_and_name_real_places(self):
        world = {"locations": {"A": {}, "B": {}, "C": {}}, "routes": {"A": ["B"], "C": ["B"]}}
        self.assertEqual(route_map(world), {"A": ["B"], "B": ["A", "C"], "C": ["B"]})
        self.assertEqual(route_errors(world), [])
        bad = {"locations": {"A": {}}, "routes": {"A": ["Nowhere"], "Elsewhere": "A"}}
        errors = route_errors(bad)
        self.assertTrue(any("Nowhere" in e for e in errors))
        self.assertTrue(any("'Elsewhere' is not a location" in e for e in errors))
        self.assertTrue(any("routes.Elsewhere must be a list" in e for e in errors))

    def test_the_city_map(self):
        config.reload()
        wd = config.get("world_definition")
        routes = route_map(wd)
        self.assertEqual(set(routes), set(wd["locations"]))
        self.assertNotIn("Palace", routes["Dungeon"])  # no way from the cells to the throne room
        self.assertEqual(set(routes["Dungeon"]), {"Alley", "Sewers"})
        self.assertIn("Palace", routes["Sewers"])  # "a path that leads directly into the Palace basement"
        self.assertEqual(config.get("location_constraints.Dungeon.entry"), "sent")
        # Every place's own move leads next door.
        for name, data in wd["locations"].items():
            for action in data.get("actions") or []:
                for outcome in action.get("outcomes") or []:
                    if outcome.get("move_to"):
                        self.assertIn(outcome["move_to"], routes[name], f"{name}: {action.get('name')}")

    def test_city_stories_stay_on_the_map(self):
        config.reload()
        engine.refresh_runtime_settings()
        for seed in range(3):
            genome = Genome(rng=random.Random(seed))
            state = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            for _ in range(40):
                for action in state.player_options():
                    if action.source == "navigation":
                        self.assertIn(action.owner, state.neighbours(state.current.name))
                path = len(state.path)
                if not state.step():
                    break
                for a, b in zip(state.path[path - 1:], state.path[path:]):
                    if b != config.get("game_state.arrest_location"):
                        self.assertIn(b, state.neighbours(a), f"seed {seed}: {a} -> {b}")


def _busy_world(*missions):
    world = WorldGraph()
    hall = _loc("Hall", *[_act(f"Chore {n}", "Hall", Outcome(f"Chore {n} done."), repeat="always") for n in range(8)],
                _act("Open the vault door", "Hall", Outcome("It opens.", tags=["neutral"]), repeat="always"))
    world.add_location(hall)
    for name in ("North", "South", "East", "West", "Up"):
        world.add_location(_loc(name, _act(f"Rest in {name}", name, Outcome("Rest."), repeat="always")))
        hall.connect(world.locations[name])
    for mission in missions:
        world.add_mission(mission)
    return world


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.dict(config._config["unexpected_events"], {"enabled": False}),
            patch.dict(config._config, {"player_options": {
                "limit": 6, "min": 3, "relevance": 0.25, "focus_relevance": 0.5, "travel_limit": 3,
                "talk_limit": 2, "focus_limit": 4, "tense_tags": ["threat", "combat"], "order": "ranked"}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_choices_are_limited_and_fewer_when_tense(self):
        state = GameState(_busy_world(), _genome(), start="Hall", rng=random.Random(0))
        calm = state.player_options()
        self.assertLessEqual(len(calm), 6)
        self.assertGreaterEqual(len(calm), 3)
        self.assertLessEqual(sum(1 for a in calm if a.source == "navigation"), 3)
        self.assertTrue(any(a.source == "navigation" for a in calm))  # always a way on
        self.assertEqual(state.tension(), 0.0)
        state.turn_log.append({"tags": ["threat"], "succeeded": True})
        self.assertGreaterEqual(state.tension(), 0.7)
        tense = state.player_options()
        self.assertLessEqual(len(tense), 4)
        self.assertLess(len(tense), len(calm))

    def test_the_quest_step_is_always_offered(self):
        mission = Mission("vault", {"action": "Open the vault door"}, title="Open the vault")
        state = GameState(_busy_world(mission), _genome(), start="Hall", rng=random.Random(0), mission="vault")
        state.turn_log.append({"tags": ["threat"], "succeeded": True})  # even when few are shown
        self.assertIn("Open the vault door", [a.name for a in state.player_options()])

    def test_without_the_filter_the_best_limit_options(self):
        with patch.dict(config._config["player_options"], {"relevance_filter": False}):
            state = GameState(_busy_world(), _genome(), start="Hall", rng=random.Random(0))
            self.assertEqual([a.name for a in state.player_options()],
                             [a.name for a in state.ranked_available_actions(limit=6)])

    def test_a_player_can_take_only_what_is_offered(self):
        state = GameState(_busy_world(), _genome(), start="Hall", rng=random.Random(0))
        token = "choice-test"
        with sessions.SESSION_LOCK:
            sessions.SESSIONS[token] = state
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        offered = [a.name for a in sessions._player_options(state)]
        hidden = [a.name for a in state.ranked_available_actions() if a.name not in offered]
        if hidden:
            with self.assertRaises(AuthoringError):
                sessions.play_action(token, hidden[-1])
        sessions.play_action(token, offered[-1])
        self.assertEqual(state.actions_taken, 1)


class FailureAndHostilityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(config._config["unexpected_events"], {"enabled": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_theft_gets_nothing(self):
        world = WorldGraph()
        world.add_location(_loc("Hall"))
        slip, grab = Outcome("Your hand slips.", tags=["failure"]), Outcome("You grab it.")
        action = Action("[Gem] Grab the gem", [grab, slip], collects_object="Gem", source="item", owner="Gem")
        world.add_object(Object("Gem", ["hall"], actions=[action], collectible=True))
        state = GameState(world, _genome(), start="Hall", rng=random.Random(0))
        state.apply(slip, action=action)
        self.assertNotIn("Gem", state.inventory)
        state.apply(Outcome("", tags=["failure"], send_to="Hall"), action=action)  # an arrest for it
        self.assertNotIn("Gem", state.inventory)
        state.apply(grab, action=action)
        self.assertIn("Gem", state.inventory)

    def test_an_attacker_gives_no_directions(self):
        world = _line_world()
        bandit = Character("Bandit", ["gate"], known_locations=["Gate"], encounter_event="ambush", actions=[])
        world.add_character(bandit)
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        action = Action("Run", [Outcome("You get away.", tags=["success", "social"])], character_name="Bandit",
                        source="event", owner="Bandit")
        state.character_interactions["Bandit"] = 1
        state.apply(action.outcomes[0], action=action)
        self.assertFalse(any(line.startswith("[LEAD] Bandit") for line in state.history))


class QuestAndPeopleTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(config._config["unexpected_events"], {"enabled": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_quest_log_follows_the_stages(self):
        mission = Mission("heist", None, title="The heist", description="Rob the tower.", stages=[
            {"title": "Reach the square", "goal": {"location": "Square"}},
            {"title": "Reach the tower", "goal": {"location": "Tower"}},
        ])
        world = _line_world()
        world.add_mission(mission)
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0), mission="heist")
        log = state.quest_log()
        self.assertEqual([s["state"] for s in log["stages"]], ["current", "todo"])
        self.assertEqual(log["todo"], ["Reach the Square"])
        self.assertEqual(log["status"], "in progress")
        state._move_to("Square")
        state._check_end_conditions()
        log = state.quest_log()
        self.assertEqual([s["state"] for s in log["stages"]], ["done", "current"])
        self.assertEqual(log["todo"], ["Reach the Tower"])

    def test_people_here_and_met(self):
        world = _line_world()
        world.add_character(Character("Sentry", ["gate"], known_locations=["Gate"], goals=["Keep the gate"],
                                      descriptions=["A bored sentry."], actions=[]))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(0))
        state.ranked_available_actions()  # sets the scene
        state.visible_inhabitants.update({"Sentry", "Porter"})
        for name in ("Sentry", "Porter"):
            if all(c.name != name for c in state.current_inhabitants):
                state.current_inhabitants.append(next(c for c in world.characters if c.name == name))
        state._announced_visit = None
        state._announce_arrival()
        people = {p["name"]: p for p in state.people_here()}
        self.assertEqual(people["Sentry"]["wants"], "Keep the gate")
        self.assertEqual(people["Sentry"]["description"], "A bored sentry.")
        self.assertFalse(people["Sentry"]["hostile"])
        met = {p["name"]: p for p in state.people_met()}
        self.assertEqual(met["Sentry"]["where"], ["Gate"])

    def test_ways_on_are_the_known_neighbours(self):
        state = GameState(_line_world(), _genome(), start="Gate", rng=random.Random(0))
        state.current.connect(state.world.locations["Square"])
        self.assertEqual(state.ways_on(), [{"name": "Square", "visited": False, "toward_quest": False}])


class ModelTagTests(unittest.TestCase):
    def test_every_tag_a_story_records_is_exported_with_models(self):
        # A tag missing from a model's tag vectors scores as neutral once the model is loaded, so a
        # loaded engine would tell a different story (the quest-stage tag `milestone` was missing).
        from core_engine.model import world_tags
        from utils.tag_similarity import _normalize_tag_text

        config.reload()
        engine.refresh_runtime_settings()
        exported = {_normalize_tag_text(tag) for tag in world_tags(config.as_dict())}
        for seed in range(4):
            genome = Genome(rng=random.Random(seed))
            state = GameState(engine.build_world(genome), genome, rng=random.Random(seed),
                              mission="break_into_the_vault")
            while not state.ended and state.step():
                pass
            for entry in state.tags_seen:
                for tag in ([entry] if isinstance(entry, str) else entry):
                    self.assertIn(_normalize_tag_text(tag), exported, f"seed {seed}")


class PlayableQuestTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        config.reload()
        engine.refresh_runtime_settings()
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-saves-tests-")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_world_quests_and_choosing_one(self):
        quests = {q["key"]: q for q in sessions.playable_missions("quest-test")}
        self.assertEqual(quests["break_into_the_vault"]["stages"], ["Get into the Vault", "Crack the main safe"])
        self.assertTrue(quests["break_into_the_vault"]["available"])
        token, state = sessions._start_game(Genome(rng=random.Random(1)), "quest-test", None, None,
                                            mission="Break into the Vault")
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        self.assertEqual(state.mission.name, "break_into_the_vault")
        snapshot = sessions.play_state(state)
        self.assertEqual(snapshot["quest"]["title"], "Break into the Vault")
        self.assertIn("people", snapshot)
        self.assertIn("ways_on", snapshot)
        scene, _ = sessions._scene(state)
        self.assertIn("**Quest:** Break into the Vault · stage 1/2", scene)

    def test_unknown_or_closed_quests_are_refused(self):
        with self.assertRaises(AuthoringError):
            sessions._start_game(Genome(rng=random.Random(1)), "quest-test", None, None, mission="No such quest")
        closed = [q for q in sessions.playable_missions("quest-test") if not q["available"]]
        if closed:
            with self.assertRaises(AuthoringError):
                sessions._start_game(Genome(rng=random.Random(1)), "quest-test", None, None, mission=closed[0]["key"])


if __name__ == "__main__":
    unittest.main()
