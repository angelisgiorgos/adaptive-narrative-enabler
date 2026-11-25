"""Side-story orchestration (core_engine/side_story.py, ``side_story`` in the tuning) and the side-story
objective terms (fitness version 2, ``fitness.side_story``): main-only worlds stay as they were, optional
content comes beside the main objective (never instead of it, never forced), pacing holds, relevance and
connections (reveals, rescues, allies, callbacks), and the objective: encounter spam and repeated contracts
cannot inflate it, meaningful side quests are told from filler, fixed seeds stay comparable."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import random
import unittest
from unittest.mock import patch

from core_engine import (Action, Character, GameState, Genome, Location, Mission, Object, Outcome, SideQuest, WorldGraph,
                         choices, fitness, side_quests, side_story)
from core_engine.context import with_state_context
from utils.config_loader import config

hold = with_state_context(side_story.hold)


def _genome(seed=9):
    random.seed(seed)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _action(label, text, owner, npc=None, outcome=None, **kwargs):
    outcome = outcome or Outcome(text, tags=["social" if npc else "neutral"])
    action = Action(f"({npc}) {label}" if npc else label, [outcome], character_name=npc,
                    source="npc" if npc else "location", owner=npc or owner, repeat="always", **kwargs)
    action.key = label
    return action


def _world(*quests, mission=True):
    """Gate (the Priest) - Cellar (wine) - Tower; Gate - Yard. The mission: reach the Tower."""
    world = WorldGraph()
    for name in ("Gate", "Cellar", "Yard", "Tower"):
        world.add_location(Location(name, [name.lower()]))
    world.connect("Gate", "Cellar")
    world.connect("Gate", "Yard")
    world.connect("Cellar", "Tower")
    world.locations["Gate"].actions = [_action("Wait", "You wait.", "Gate")]
    world.locations["Cellar"].actions = [_action("Take a bottle", "You take a bottle.", "Cellar", outcome=Outcome(
        "You take a bottle.", tags=["success"], effects=[{"give_item": "Vintage Wine"}]))]
    world.locations["Yard"].actions = [_action("Rest", "You rest.", "Yard")]
    world.locations["Tower"].actions = [_action("Look out", "You look out.", "Tower")]
    world.add_object(Object("Vintage Wine", ["drink"]))
    world.add_object(Object("Old Key", ["key"]))
    world.add_character(Character("Priest", ["gate"], unique=True, actions=[
        _action("Greet", "The Priest nods.", "Gate", npc="Priest", outcome=Outcome("The Priest nods.", tags=["chat"]))]))
    for quest in quests:
        world.add_side_quest(quest)
    if mission:
        world.add_mission(Mission("tower", {"location": "Tower"}, title="Reach the tower"))
    return world


def _settings(**policy):
    return {"side_story": {**config.get("side_story"), "enabled": True, **policy},
            "side_quest_settings": {**config.get("side_quest_settings"), "accept": "choice", "offer_after": 0,
                                    "offer_cooldown": 0},
            "unexpected_events": {**config.get("unexpected_events"), "enabled": False},
            "npc_roles": {**(config.get("npc_roles") or {}), "enabled": False},
            "conversations": {**config.get("conversations"), "enabled": False},
            "npc_leads": {**config.get("npc_leads"), "every_nth_interaction": 999},   # no lead straight to the Tower
            "climax": {**config.get("climax"), "enabled": False}}


def _state(world, seed=3, fitness_cfg=None, **policy):
    settings = _settings(**policy)
    if fitness_cfg is not None:
        settings["fitness"] = fitness_cfg
    with patch.dict(config._config, settings):
        state = GameState(world, _genome(), start="Gate", rng=random.Random(seed),
                          mission="tower" if world.missions else None)
    for char in world.characters:
        state.npc_homes[char.name] = "Gate"
    state._scene_dirty = True
    return state


def _take(state, prefix):
    options = state.player_options()
    ids = choices.assign_ids(options, state)
    state.take_option(next(option for option, action_id in zip(options, ids) if action_id.startswith(prefix)))
    return state


def _ids(state):
    return choices.assign_ids(state.player_options(), state)


def _relevant():
    """Bears on the quest: it helps (lead), reveals something, calls back near the climax."""
    return SideQuest("ledger", {"item": "Vintage Wine"}, title="The keeper's ledger", giver="Priest",
                     aids_mission="lead", rewards=[{"give_item": "Old Key"}],
                     texts={"reveal": "The ledger names the tower's old keeper.",
                            "callback": "The keeper's name, from the ledger, opens the tower door."})


def _filler():
    """Unrelated: a few coins, nothing for the quest."""
    return SideQuest("errand", {"location": "Yard"}, title="An errand", aids_mission="none",
                     rewards=[{"change_coins": 2}])


SIDE_TERMS = ("side_relevance", "side_payoff", "side_completion", "side_filler", "side_unresolved",
              "encounter_repetition", "side_interruptions")


class CompatibilityTests(unittest.TestCase):
    def test_a_main_only_world_tells_the_same_story_with_or_without_the_policy(self):
        def play(enabled):
            state = _state(_world(), enabled=enabled)
            for step in ("location:gate:wait", "go:yard", "location:yard:rest", "go:gate", "go:cellar", "go:tower"):
                _take(state, step)
            return state

        on, off = play(True), play(False)
        self.assertEqual(on.history, off.history)
        self.assertEqual(on.rng.getstate(), off.rng.getstate())
        self.assertTrue(on.won and off.won)                                                # the mission, as before
        self.assertEqual((on.end_kind, off.end_kind), ("mission", "mission"))
        breakdown = fitness._fitness_breakdown(on)
        self.assertTrue(all(breakdown[term] == 0.0 for term in SIDE_TERMS))               # no side content: nothing
        self.assertEqual(on.side_story_log if hasattr(on, "side_story_log") else [], [])

    def test_version_1_scores_as_before(self):
        version_one = {**config.get("fitness"), "version": 1}
        state = _state(_world(_filler()), fitness_cfg=version_one)
        for step in ("go:yard", "side-quest:errand:accept", "go:gate", "go:cellar"):
            _take(state, step)
        breakdown = fitness._fitness_breakdown(state)
        for term in SIDE_TERMS + ("main_drift", "decision_diversity"):
            self.assertNotIn(term, breakdown)                                              # no version 2 terms
        self.assertEqual(fitness.version(), 2)                                             # the built-in scoring
        self.assertEqual(fitness.version({"fitness": {}}), 1)                              # written before versions
        self.assertEqual(fitness.version({"fitness": {"version": 7}}), 1)                  # unknown: as before
        cfg = config.get("fitness.side_quests")
        self.assertEqual(breakdown["side_quests"], cfg["complete_bonus"])                  # the version 1 formula

    def test_a_policy_section_missing_means_off(self):
        with patch.dict(config._config, {"side_story": None}):
            self.assertFalse(side_story.settings()["enabled"])                              # older configurations
        self.assertTrue(config.get("side_story")["enabled"])                                # the built-in one: on
        self.assertEqual(side_story.validate_settings({"max_side_share": 2, "callbacks": "loud", "agency": 1}), [
            "`side_story.agency` must be true or false.", "`side_story.max_side_share` must be between 0 and 1.",
            "`side_story.callbacks` must be one of authored, all, none."])


class AgencyTests(unittest.TestCase):
    def test_optional_content_comes_beside_the_main_objective(self):
        state = _state(_world(_relevant(), _filler()))
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["ledger"]["status"], "offered")                # offered, not taken on
        _take(state, "side-quest:ledger:accept")
        for step in ("go:cellar", "location:cellar:take-a-bottle"):
            _take(state, step)
        self.assertEqual(state.side_quests["ledger"]["status"], "completed")
        self.assertFalse(state.ended)                                                      # a side quest never ends it
        _take(state, "go:tower")
        self.assertTrue(state.won)
        self.assertEqual(state.end_kind, "mission")                                        # the main quest's ending

    def test_what_the_story_sets_up_is_an_offer_never_forced(self):
        auto = SideQuest("rescue", {"location": "Yard"}, title="The rescue", accept="auto")
        state = _state(_world(auto))
        with_state_context(side_quests.hook)(state, "rescue")
        self.assertEqual(state.side_quests["rescue"]["status"], "offered")                # agency: an offer
        _take(state, "side-quest:rescue:decline")
        self.assertEqual(state.side_quests["rescue"]["status"], "declined")
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["rescue"]["status"], "declined")               # never completed for them
        forced = _state(_world(SideQuest("rescue", {"location": "Yard"}, title="The rescue", accept="auto")),
                        agency=False)
        with_state_context(side_quests.hook)(forced, "rescue")
        self.assertEqual(forced.side_quests["rescue"]["status"], "active")                # agency off: as written


class PacingTests(unittest.TestCase):
    def test_holds_while_the_main_story_needs_the_room(self):
        state = _state(_world(_relevant()))
        self.assertIsNone(hold(state, "offer"))
        cases = (
            ("climax", lambda s: s.__dict__.setdefault("climax_state", {})["armed"] if False else
             s.__dict__.update(climax_state={"armed": "_climax_1", "done": False})),
            ("main_idle", lambda s: setattr(s, "actions_taken", 12 + int(config.get("mission_settings.pursuit_start_step")))),
            ("deadline", lambda s: s.side_quests.update(x={"status": "active", "deadline": s.actions_taken + 2})),
            ("threads_full", lambda s: s.side_quests.update({f"q{n}": {"status": "active"} for n in range(4)})),
            ("gap", lambda s: s.__dict__.update(side_story_log=[{"turn": s.actions_taken, "kind": "offer"}])),
        )
        for code, make in cases:
            with self.subTest(code=code):
                held = _state(_world(_relevant()))
                make(held)
                self.assertEqual(hold(held, "offer"), code)
                self.assertEqual(side_story.plan(held)["hold"], code)
        idle = _state(_world(_relevant()))
        idle.actions_taken = 40
        self.assertEqual(with_state_context(side_story.pull_factor)(idle), 0.5)            # the pull eases (idle_pull)
        self.assertIsNone(hold(idle, "hook"))  # a hook is a consequence: only the main story's own moments count
        idle.__dict__["climax_state"] = {"armed": "_climax_1"}
        self.assertEqual(hold(idle, "hook"), "climax")

    def test_offers_wait_for_room_and_turned_down_offers_make_the_story_choosy(self):
        state = _state(_world(_relevant()))
        state.actions_taken = 12 + int(config.get("mission_settings.pursuit_start_step"))  # the main quest stalled
        _take(state, "npc:priest:greet")
        self.assertNotIn("ledger", state.side_quests)                                      # held: main_idle
        state.last_quest_progress = state.actions_taken
        _take(state, "location:gate:wait")
        self.assertEqual(state.side_quests["ledger"]["status"], "offered")                # room again
        choosy = _state(_world(_filler()))
        choosy.side_quest_events = [{"turn": 0, "quest": "a", "what": "declined"},
                                    {"turn": 0, "quest": "b", "what": "lapsed"}]
        self.assertEqual(hold(choosy, "offer", relevance=0.0), "declined")
        self.assertIsNone(hold(choosy, "offer", relevance=0.8))                            # bears on the quest

    def test_what_bears_on_the_quest_is_offered_first(self):
        unrelated = SideQuest("chores", {"coins": 50}, title="Chores", giver="Priest", aids_mission="none")
        state = _state(_world(unrelated, _relevant()), max_open_threads=1)
        _take(state, "npc:priest:greet")
        self.assertEqual([key for key, r in state.side_quests.items() if r["status"] == "offered"], ["ledger"])
        rows = {row["key"]: row for row in side_story.plan(state)["candidates"]}
        self.assertEqual(rows["chores"]["status"], "threads_full")
        off = _state(_world(SideQuest("chores", {"coins": 50}, title="Chores", giver="Priest",
                                      aids_mission="none"), _relevant()), enabled=False)
        _take(off, "npc:priest:greet")
        self.assertIn("chores", off.side_quests)                                            # off: written order first


class ConnectionTests(unittest.TestCase):
    def test_a_side_quest_reveals_and_calls_back_near_the_climax(self):
        state = _state(_world(_relevant()))
        for step in ("npc:priest:greet", "side-quest:ledger:accept", "go:cellar", "location:cellar:take-a-bottle"):
            _take(state, step)
        self.assertIn("[SIDE STORY] The keeper's ledger turned up something about your quest: The ledger names "
                      "the tower's old keeper.", state.history)
        told = with_state_context(side_story.callbacks)(state, "finale")
        self.assertEqual(told, 1)
        self.assertIn("[CALLBACK] The keeper's ledger: The keeper's name, from the ledger, opens the tower door.",
                      state.history)
        self.assertEqual(with_state_context(side_story.callbacks)(state, "climax"), 0)      # once a story
        what = {entry["what"] for entry in state.side_story_links if entry["key"] == "ledger"}
        self.assertTrue({"change", "aid", "reveal", "callback"} <= what)

    def test_helping_someone_makes_an_ally_who_is_called_back(self):
        friend = SideQuest("friend", {"location": "Yard"}, title="A friend in need", giver="Priest",
                           aids_mission="ally")
        state = _state(_world(friend))
        for step in ("npc:priest:greet", "side-quest:friend:accept", "go:yard"):
            _take(state, step)
        self.assertIn("Priest", state.allies)                                              # helps in checks, the climax
        with_state_context(side_story.callbacks)(state, "climax")
        self.assertTrue(any(line.startswith("[CALLBACK] A friend in need:") and "stands with you" in line
                            for line in state.history))
        none = _state(_world(SideQuest("friend", {"location": "Yard"}, title="A friend in need", giver="Priest",
                                       aids_mission="ally")), callbacks="authored")
        for step in ("npc:priest:greet", "side-quest:friend:accept", "go:yard"):
            _take(none, step)
        self.assertEqual(with_state_context(side_story.callbacks)(none, "climax"), 0)      # authored only


class ObjectiveTests(unittest.TestCase):
    def _played(self, quest, steps):
        state = _state(_world(quest))
        for step in steps:
            _take(state, step)
        return state

    def test_a_side_quest_with_consequences_is_told_from_filler(self):
        meaningful = self._played(_relevant(), ("npc:priest:greet", "side-quest:ledger:accept", "go:cellar",
                                                "location:cellar:take-a-bottle"))
        filler = self._played(_filler(), ("go:yard", "side-quest:errand:accept", "go:gate", "go:cellar"))
        labels = {item["key"]: item["label"] for item in side_story.classify(meaningful) + side_story.classify(filler)}
        self.assertEqual(labels, {"ledger": "meaningful", "errand": "filler"})
        good = side_story.objective_terms(meaningful, config.get("fitness.side_story"))
        bad = side_story.objective_terms(filler, config.get("fitness.side_story"))
        self.assertGreater(good["side_payoff"], bad["side_payoff"])
        self.assertGreater(good["side_relevance"], bad["side_relevance"])
        self.assertLess(bad["side_filler"], good["side_filler"])
        self.assertGreater(good["side_payoff"] + good["side_relevance"] + good["side_filler"],
                           bad["side_payoff"] + bad["side_relevance"] + bad["side_filler"])

    def test_repeating_a_contract_cannot_inflate_fitness(self):
        def score(count, version):
            contracts = [SideQuest(f"bounty{n}", {"location": "Yard"}, title=f"Bounty {n}", giver="Priest",
                                   repeatable=True) for n in range(count)]
            state = _state(_world(*contracts))
            state.__dict__["side_quests"] = {f"bounty{n}": {"status": "completed", "stage": 0, "ended": n}
                                             for n in range(count)}
            state.__dict__["side_quest_events"] = [{"turn": n, "quest": f"bounty{n}", "what": "stage"}
                                                   for n in range(count)]
            return with_state_context(side_quests.fitness_term)(state, config.get("fitness.side_quests"), version)

        cfg = config.get("fitness.side_quests")
        bound = (cfg["complete_bonus"] + cfg["step_bonus"] + cfg["mission_aid_bonus"]) / (1 - cfg["diminishing"])
        scores = [score(n, 2) for n in (1, 2, 4, 8, 16, 32)]
        self.assertTrue(all(value < bound for value in scores))                            # bounded
        gains = [b - a for a, b in zip(scores, scores[1:])]
        self.assertTrue(all(later < earlier for earlier, later in zip(gains, gains[1:])))   # less for each one more
        self.assertLess(scores[-1] - scores[-2], 0.01)
        self.assertEqual(score(32, 1), 32 * (cfg["complete_bonus"] + cfg["step_bonus"]))     # version 1 grew without end

    def test_encounter_spam_lowers_fitness(self):
        state = self._played(_filler(), ("go:yard", "side-quest:errand:accept", "go:gate", "go:cellar"))
        state.__dict__["dynamic_encounters"] = [{"turn": 1, "event": "thieves", "category": "ambush", "result": "success"}]
        once = fitness.fitness(state)
        spam = copy.deepcopy(state)
        spam.__dict__["dynamic_encounters"] = [{"turn": turn, "event": "thieves", "category": "ambush",
                                                "result": "success"} for turn in range(4)]
        self.assertLess(fitness.fitness(spam), once)
        terms = side_story.objective_terms(spam, config.get("fitness.side_story"))
        self.assertLess(terms["encounter_repetition"],
                        side_story.objective_terms(state, config.get("fitness.side_story"))["encounter_repetition"])

    def test_interruptions_are_measured_even_with_the_policy_off(self):
        state = _state(_world(SideQuest("late", {"location": "Yard"}, title="Late", giver="Priest")), enabled=False)
        state.actions_taken = 12 + int(config.get("mission_settings.pursuit_start_step"))  # the main quest stalled
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["late"]["status"], "offered")                  # off: it comes anyway
        self.assertEqual(state.side_story_log[-1]["hold"], "main_idle")
        self.assertTrue(state.side_story_log[-1]["interrupting"])
        self.assertLess(fitness._fitness_breakdown(state)["side_interruptions"], 0)

    def test_main_drift_and_decision_diversity(self):
        cfg = config.get("fitness.side_story")
        state = _state(_world())
        for _ in range(4):
            _take(state, "location:gate:wait")
        state.actions_taken = cfg["drift_allowed"] + 10                                     # long without progress
        self.assertLess(side_story.objective_terms(state, cfg)["main_drift"], 0)
        state.main_progress_turns = [cfg["drift_allowed"] // 2, cfg["drift_allowed"]]
        self.assertEqual(side_story.objective_terms(state, cfg)["main_drift"], 0.0)
        varied = copy.deepcopy(state)
        varied.turn_log = [{"source": "event", "options": ["x"], "tags": [tag], "check": None}
                           for tag in ("combat", "stealth", "social", "trade", "escape", "discovery")]
        same = copy.deepcopy(state)
        same.turn_log = [{"source": "event", "options": ["x"], "tags": ["combat"], "check": None}] * 6
        self.assertGreater(side_story.objective_terms(varied, cfg)["decision_diversity"],
                           side_story.objective_terms(same, cfg)["decision_diversity"])
        self.assertEqual(side_story.objective_terms(same, cfg)["decision_diversity"], 0.0)

    def test_fixed_seeds_stay_comparable(self):
        def story(seed):
            state = _state(_world(_relevant(), _filler()), seed=seed)
            for _ in range(10):
                options = [o for o in state.player_options() if o.source != "side_quest"]
                if state.ended or not options:
                    break
                state.take_option(options[0])
            return state

        first, again = story(5), story(5)
        self.assertEqual(first.history, again.history)
        self.assertEqual(fitness._fitness_breakdown(first), fitness._fitness_breakdown(again))
        self.assertEqual(first.side_story_log, again.side_story_log)


if __name__ == "__main__":
    unittest.main()


class AuditFixTests(unittest.TestCase):
    """What the API audit found: the trace's side-story section and codes, the condition reference's
    side quests, whose recorded stories one may read, encounters in the session's parameters."""

    def test_the_decision_trace_explains_side_content(self):
        from core_engine.diagnostics import decision_trace

        state = _state(_world(_relevant()))
        trace = decision_trace(state)
        self.assertEqual(trace["side_story"]["candidates"][0]["key"], "ledger")
        self.assertIn("main_idle", trace["side_story"]["codes"])
        self.assertIn("safe_place", trace["encounters"]["codes"])                       # every gate code explained
        self.assertIn(trace["encounters"]["gate"], trace["encounters"]["codes"])
        import app

        _take(state, "npc:priest:greet")                                                  # the ledger now on offer
        state.actions_taken = 12 + int(config.get("mission_settings.pursuit_start_step"))
        shown = app.decisions_markdown(decision_trace(state))                            # the studio's author view
        self.assertIn("**Side content:** held (`main_idle`: The main quest has stood still", shown)

    def test_the_condition_reference_names_the_side_quests(self):
        from fastapi.testclient import TestClient

        import api

        facts = TestClient(api.api).get("/v1/conditions/reference").json()["facts"]
        self.assertEqual(facts["side_quest_done"]["refers_to"], "side_quests")
        self.assertEqual(set(facts["side_quest_done"]["names"]), set(config.get("world_definition.side_quests")))

    def test_someone_else_s_playthrough_is_not_theirs_to_read(self):
        import types

        import api
        from backend import records, sessions

        owners = {"mine": "u1", "theirs": "u2", "anonymous": None}

        def owner(token):
            if token not in owners:
                raise LookupError(token)
            return owners[token]

        def asks(user):
            return types.SimpleNamespace(headers={"x-ane-user": user} if user else {})

        with patch.object(sessions, "_owner", owner):
            visible = {session: api._story_visible({"session_id": session}, asks("u1"))
                       for session in ("mine", "theirs", "anonymous", "gone", None)}
            self.assertEqual(visible, {"mine": True, "theirs": False, "anonymous": True, "gone": False, None: True})
            studio = types.SimpleNamespace(headers={"x-ane-client": "studio"})
            self.assertTrue(api._story_visible({"session_id": "theirs"}, studio))       # the studio reads all
            self.assertEqual(records.DEFAULT_USER, "test")

    def test_the_session_parameters_carry_the_encounters(self):
        from core_engine import encounters, resume

        state = _state(_world())
        state.__dict__.update(dynamic_encounters=[{"turn": 2, "event": "thieves", "category": "ambush",
                                                   "location": "Gate", "faction": "Thieves", "npc": None,
                                                   "result": "failure", "held_in": None}],
                              faction_shift={"Thieves": -1.0}, encounter_rewards=["thieves"])
        params = resume.parameters(state)
        self.assertEqual(params["encounters"]["factions"], {"Thieves": -1.0})
        self.assertNotIn("held_in", params["encounters"]["attacks"][0])                 # what a UNP needs only
        fresh = _state(_world())
        encounters.restore(fresh, params["encounters"])
        self.assertEqual(fresh.dynamic_encounters, [])                                  # not a template of this world
        self.assertEqual(fresh.faction_shift, {"Thieves": -1.0})
        encounters.restore(fresh, None)                                                 # older parameters: nothing
        self.assertEqual(fresh.faction_shift, {"Thieves": -1.0})
