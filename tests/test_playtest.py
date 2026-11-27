"""Gameplay playtests (evaluator/playtest.py, evaluator/replay.py) and the playtest player strategies
(core_engine/players.py STRATEGIES): reproducible scenarios, the automated measures and checks, the
author notes and their links, the matched-seed comparison, the judge's view kept apart, the report,
the replay page, and regression scenarios on the example worlds. No training is run: random
storytellers drawn from the seeds, short stories."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import json
import os
import random
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import main as engine
from core_engine import Action, Outcome, players
from evaluator import playtest, replay
from evaluator.game_judge import CRITERIA, PLAYTEST_CRITERIA, build_prompt

ROOT = Path(__file__).resolve().parent.parent
MONASTERY = str(ROOT / "examples" / "frozen_monastery.yaml")
STATION = str(ROOT / "examples" / "orbital_station.yaml")


class _Sandbox(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-playtest-"),
                                          "ANE_RECORDS": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        engine.refresh_runtime_settings()


def _scenario(**kwargs):
    values = {"name": "t", "world": MONASTERY, "quest": "return_the_relic", "start": "Mountain Gate",
              "player": "quester", "seed": 3, "max_turns": 15}
    values.update(kwargs)
    return playtest.Scenario(**values)


def _option(name, source="location", owner=None, tags=()):
    return Action(name, [Outcome("It happens.", tags=list(tags))], source=source, owner=owner)


class _Seen:
    """What a strategy sees of a story: no check, full health, nobody talked to, no side quest."""

    def __init__(self, **more):
        self.health, self.max_health = 10, 10
        self.character_interactions, self.location_counts = {}, {}
        self.__dict__.update(more)
        self.world = types.SimpleNamespace(locations={})
        self.current = types.SimpleNamespace(name="Here")

    def check_hint(self, action):
        return None


class StrategyTests(unittest.TestCase):
    def test_the_playtest_strategies_leave_training_unchanged(self):
        self.assertEqual(set(players.PLAYERS), {"follower", "explorer", "quester", "cautious", "random"})
        self.assertEqual(players.DEFAULT_MIX, {"follower": 2, "explorer": 1, "quester": 2, "cautious": 1, "random": 1})
        self.assertTrue({"side_quester", "fighter", "conflict_avoider"} <= set(players.STRATEGIES))
        self.assertTrue(players.mix_errors({"fighter": 1}))  # not a training player

    def test_every_named_strategy_exists_and_is_reproducible(self):
        for alias in ("main_quest", "explorer", "side_quest", "combat", "cautious", "conflict_avoiding", "random"):
            with self.subTest(alias=alias):
                first, second = players.strategy(alias, 7), players.strategy(alias, 7)
                self.assertEqual(first.name, players.STRATEGY_ALIASES[alias])
                self.assertEqual(first.rng.random(), second.rng.random())
        self.assertEqual(players.strategy("main_quest", 1).name, "quester")  # the training player, reused
        with self.assertRaises(ValueError):
            players.strategy("wizard", 1)

    def test_the_fighter_seeks_fights_and_the_avoider_shuns_them(self):
        seen = _Seen()
        options = [_option("Look around"), _option("Fight them off", "event", tags=["combat"]),
                   _option("Surrender your pack", "event")]
        fighter = players.strategy("combat", 1).weights(seen, options)
        avoider = players.strategy("conflict_avoiding", 1).weights(seen, options)
        self.assertEqual(max(range(3), key=fighter.__getitem__), 1)
        self.assertEqual(min(range(3), key=avoider.__getitem__), 1)
        self.assertEqual(max(range(3), key=avoider.__getitem__), 2)  # giving way first
        self.assertTrue(players.is_combat(seen, _option("Ambush the patrol")))  # by name, untagged
        self.assertFalse(players.is_combat(seen, _option("Read the scroll")))

    def test_the_side_quester_takes_on_the_side_quests_offered(self):
        accept = _option("Take it on", "side_quest")
        accept.key = "accept"
        decline = _option("Turn it down", "side_quest")
        decline.key = "decline"
        weights = players.strategy("side_quest", 1).weights(_Seen(), [_option("Leave"), decline, accept])
        self.assertEqual(max(range(3), key=weights.__getitem__), 2)


class ScenarioTests(_Sandbox):
    def test_a_scenario_file_expands_players_and_seeds_and_resolves_paths(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "s.yaml").write_text(yaml.safe_dump({
            "checks": {"repeat_action": 3},
            "defaults": {"max_turns": 9},
            "scenarios": [{"name": "m", "world": "examples/frozen_monastery.yaml", "player": ["explorer", "combat"],
                           "seed": [1, 2]}, {"name": "one", "player": "random"}]}), encoding="utf-8")
        scenarios, checks = playtest.load(folder / "s.yaml")
        self.assertEqual([s.name for s in scenarios], ["m-explorer-s1", "m-explorer-s2", "m-combat-s1", "m-combat-s2", "one"])
        self.assertTrue(Path(scenarios[0].world).is_file())
        self.assertEqual((scenarios[0].max_turns, scenarios[-1].world), (9, "city"))
        self.assertEqual((checks["repeat_action"], checks["stall_turns"]), (3, playtest.DEFAULT_CHECKS["stall_turns"]))

    def test_the_shipped_scenario_file_loads(self):
        scenarios, _ = playtest.load(ROOT / "evaluator" / "scenarios" / "side_content.yaml")
        self.assertGreaterEqual(len(scenarios), 20)
        self.assertEqual({Path(s.world).name for s in scenarios}, {"frozen_monastery.yaml", "orbital_station.yaml"})
        self.assertTrue({s.player for s in scenarios} >= {"main_quest", "explorer", "side_quest", "combat",
                                                         "conflict_avoiding", "random"})

    def test_the_same_scenario_plays_the_same_story(self):
        first, second = playtest.run_scenario(_scenario()), playtest.run_scenario(_scenario())
        self.assertEqual(first.texts, second.texts)
        self.assertEqual(first.decisions, second.decisions)
        self.assertEqual(first.metrics, second.metrics)
        other = playtest.run_scenario(_scenario(seed=4))
        self.assertNotEqual(first.texts, other.texts)

    def test_a_held_player_is_not_told_the_same_turn_again(self):
        """LIMITATIONS #8: in the Ice Cell this story told the Warden's small talk four turns in a row, word
        for word; small talk is now once per visit, so no turn repeats the one before."""
        run = playtest.run_scenario(playtest.Scenario(name="monastery-held", world="examples/frozen_monastery.yaml",
                                                      player="fighter", seed=7, max_turns=100))
        self.assertTrue(any(entry.get("where") == "Ice Cell" for entry in run.journal))  # it was held
        repeated = [i for i in range(1, len(run.texts)) if run.texts[i] and run.texts[i] == run.texts[i - 1]]
        self.assertEqual(repeated, [])

    def test_clue_payoffs_are_reported_as_matched_and_strictly(self):
        """LIMITATIONS #1: a payoff rate is reported both ways, so `any` matching (a payoff closing an
        unrelated clue) is never read as coherence."""
        run = playtest.run_scenario(_scenario())
        clues = run.metrics["clues"]
        self.assertEqual(set(clues), {"matching", "planted", "paid", "paid_strict", "mismatched"})
        self.assertEqual(clues["mismatched"], max(0, clues["paid"] - clues["paid_strict"]))
        from core_engine import rules

        self.assertEqual(rules.clue_pairs({"setup_1": 1}, {"payoff_2": 3}, "any")[0][0][:3:2], ("setup_1", "payoff_2"))
        self.assertEqual(rules.clue_pairs({"setup_1": 1}, {"payoff_2": 3}, "numbered")[0], [])  # not its own clue

    def test_the_scenario_fixes_start_quest_and_length(self):
        run = playtest.run_scenario(_scenario(max_turns=6))
        self.assertEqual(run.state.start_location, "Mountain Gate")
        self.assertEqual(run.state.mission.name, "return_the_relic")
        self.assertLessEqual(len(run.journal), 6)
        self.assertIn(run.stopped, ("max_turns", "ended", "dead"))

    def test_the_player_only_chooses_among_the_choices_shown(self):
        run = playtest.run_scenario(_scenario(player="random", max_turns=12))
        self.assertTrue(run.decisions)
        for decision in run.decisions:
            self.assertTrue(0 <= decision["chosen"] < len(decision["options"]))
            self.assertTrue(all(option["id"] for option in decision["options"]))
        taken = [d for d in run.decisions if d["took_turn"]]
        self.assertEqual([d["options"][d["chosen"]]["name"] for d in taken], [e["action"] for e in run.journal])

    def test_overrides_and_an_explicit_storyteller(self):
        from core_engine import Genome
        from utils.config_loader import config

        traits = Genome(rng=random.Random(42)).traits()
        run = playtest.run_scenario(_scenario(genome=traits, overrides={"side_story.enabled": False}, max_turns=4))
        self.assertEqual(run.state.genome.traits(), traits)
        with run.context.bind():
            self.assertIs(config.get("side_story.enabled"), False)


class MeasureTests(_Sandbox):
    def test_the_measures_cover_every_area_and_stay_apart_from_the_judge(self):
        run = playtest.run_scenario(_scenario(player="side_quest", start="Glacier Path", quest=None, seed=5,
                                              max_turns=19))
        m = run.metrics
        for key in ("main_quest", "side_quests", "encounters", "choices", "movement", "npcs", "repetition",
                    "filler", "coherence"):
            self.assertIn(key, m)
        self.assertEqual(m["turns"], len(run.journal))
        self.assertEqual(m["filler"]["turns"] + sum(playtest._new_content(run.journal)), m["turns"])
        self.assertIsInstance(run.fitness.get("total"), float)
        data = run.to_dict()
        self.assertEqual(data["subjective"], {"judge": None})
        self.assertIn("fitness", data["automated"])
        self.assertNotIn("judge", data["metrics"])
        json.dumps(data, default=playtest._jsonable)

    def test_filler_and_streaks(self):
        journal = [{"turn": 1, "told": "A", "health": 5, "coins": 0}, {"turn": 2, "told": "A", "health": 5, "coins": 0},
                   {"turn": 3, "told": "A", "health": 5, "coins": 0}, {"turn": 4, "told": "B", "health": 5, "coins": 0},
                   {"turn": 5, "told": "B", "health": 4, "coins": 0}]
        self.assertEqual(playtest._new_content(journal), [True, False, False, True, True])
        self.assertEqual(playtest._streaks([True, False, False, True, False]), [(1, 2), (4, 1)])

    def test_element_links_name_the_entry_and_its_endpoint(self):
        self.assertEqual(playtest.element_link("location", "Abbot's Study")["api"], "/v1/entities/Locations/Abbot%27s%20Study")
        self.assertIn("World → NPCs → Warden", playtest.element_link("npc", "Warden")["studio"])
        self.assertEqual(playtest.element_link("side_quest", "x")["api"], "/v1/documents/side_quests")
        self.assertEqual(playtest.element_link("mission", "y")["api"], "/v1/documents/missions")


def _fake_run(journal, stopped="max_turns", attacks=(), removed_at=None, ally_since=None, reachable=None, **state):
    fields = {"won": False, "ended": False, "end_kind": None, "end_reason": None, "mission": None,
              "current": types.SimpleNamespace(name="Here"), "removed_npcs": set(removed_at or ()),
              "quest_log": lambda: {"key": "q"}, "dynamic_encounters": list(attacks), "allies": set(ally_since or ())}
    fields.update(state)
    fake = types.SimpleNamespace(**fields)
    return playtest.Playthrough(_scenario(), fake, None, [], stopped, journal, ["" for _ in journal],
                                removed_at=dict(removed_at or {}), ally_since=dict(ally_since or {}),
                                reachable=dict(reachable or {}))


def _turns(n, per_turn=None):
    return [{"turn": i, "where": "Hall", "action": f"act {i}", "told": f"t{i}", **(per_turn or {}).get(i, {})}
            for i in range(1, n + 1)]


class CheckTests(unittest.TestCase):
    checks = playtest.DEFAULT_CHECKS

    def codes(self, run):
        return [issue["code"] for issue in playtest.detect_issues(run, self.checks)]

    def test_a_story_without_choices_is_a_softlock(self):
        self.assertIn("softlock", self.codes(_fake_run(_turns(3), stopped="no_choices")))
        self.assertIn("softlock", self.codes(_fake_run(_turns(3), stopped="free_choice_loop")))
        self.assertIn("softlock", self.codes(_fake_run(_turns(3), end_kind="stalled", end_reason="nothing to do")))
        self.assertNotIn("softlock", self.codes(_fake_run(_turns(3), stopped="ended", ended=True)))

    def test_a_quest_out_of_reach_for_several_turns_is_flagged_not_a_moment(self):
        mission = types.SimpleNamespace(name="q")
        brief = _fake_run(_turns(8), reachable={3: False, 4: False}, mission=mission)
        self.assertNotIn("objective_unreachable", self.codes(brief))
        long = _fake_run(_turns(8), reachable={i: False for i in range(2, 8)}, mission=mission)
        issue = next(i for i in playtest.detect_issues(long, self.checks) if i["code"] == "objective_unreachable")
        self.assertEqual(issue["turn"], 2)
        self.assertEqual(issue["elements"][0]["api"], "/v1/documents/missions")

    def test_a_long_stretch_without_anything_new_is_a_stall(self):
        journal = [{"turn": i, "where": "Hall", "action": "Wait", "told": "Nothing."} for i in range(1, 15)]
        self.assertIn("stall", self.codes(_fake_run(journal)))

    def test_contradictions(self):
        journal = _turns(6, {5: {"with": "Abbot", "action": "Talk"}})
        self.assertIn("gone_npc_speaks", self.codes(_fake_run(journal, removed_at={"Abbot": 3})))
        self.assertNotIn("gone_npc_speaks", self.codes(_fake_run(journal, removed_at={"Abbot": 5})))
        reopened = _turns(5, {2: {"side_quests": [{"quest": "thieves", "what": "declined"}]},
                              4: {"side_quests": [{"quest": "thieves", "what": "started"}]}})
        self.assertIn("side_quest_reopened", self.codes(_fake_run(reopened)))
        again = _turns(5, {2: {"quest_complete": True}, 4: {"quest_done": ["Find it"]}})
        self.assertIn("quest_step_after_completion", self.codes(_fake_run(again)))
        attack = [{"turn": 4, "event": "raid", "npc": "Hermit"}]
        self.assertIn("ally_attacks", self.codes(_fake_run(_turns(5), attacks=attack, ally_since={"Hermit": 2})))
        self.assertNotIn("ally_attacks", self.codes(_fake_run(_turns(5), attacks=attack, ally_since={"Hermit": 4})))

    def test_excessive_encounter_repetition(self):
        same = [{"turn": t, "event": "thieves", "category": "ambush"} for t in (2, 6, 10)]
        self.assertIn("encounter_repetition", self.codes(_fake_run(_turns(12), attacks=same)))
        packed = [{"turn": t, "event": f"e{t}", "category": "raid"} for t in (1, 2, 4, 5)]
        codes = self.codes(_fake_run(_turns(10), attacks=packed))
        self.assertIn("encounter_frequency", codes)
        self.assertIn("encounters_back_to_back", codes)
        spaced = [{"turn": t, "event": f"e{t}"} for t in (2, 12)]
        self.assertFalse({"encounter_repetition", "encounter_frequency", "encounters_back_to_back"}
                         & set(self.codes(_fake_run(_turns(20), attacks=spaced))))

    def test_action_loops(self):
        journal = [{"turn": i, "where": "Gate", "action": "Ring the bell", "told": f"t{i}"} for i in range(1, 5)]
        issue = next(i for i in playtest.detect_issues(_fake_run(journal), self.checks) if i["code"] == "action_loop")
        self.assertEqual(issue["elements"][0]["name"], "Gate")


class WorldCheckTests(_Sandbox):
    def _world(self, change):
        data = yaml.safe_load(Path(MONASTERY).read_text(encoding="utf-8"))
        change(data["world_definition"])
        path = Path(tempfile.mkdtemp()) / "world.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return str(path)

    def test_the_example_worlds_have_no_impossible_objectives(self):
        for world in (MONASTERY, STATION):
            with self.subTest(world=world):
                run = playtest.play(_scenario(world=world, quest=None, start=None, max_turns=1))
                self.assertEqual([i for i in run.world_issues if i["severity"] == "error"], [])

    def test_missing_actions_and_outcomes_are_found(self):
        def break_it(world):
            world["missions"]["ring_the_bell"]["goal"] = {"action": "Sing to the stars"}
            world["missions"]["reach_the_hermit"]["goal"] = {"outcome_tag": "moonlit_dance"}

        run = playtest.play(_scenario(world=self._world(break_it), quest="return_the_relic", max_turns=1))
        found = {(i["code"], i["elements"][0]["name"]) for i in run.world_issues}
        self.assertIn(("missing_required_action", "ring_the_bell"), found)
        self.assertIn(("missing_required_action", "reach_the_hermit"), found)
        self.assertTrue(all(i["scope"] == "world" for i in run.world_issues))

    def test_a_step_naming_an_unknown_place_is_refused_by_the_world_already(self):
        def break_it(world):
            quest = world["side_quests"]["the_thieves_hideout"]
            (quest["stages"][0] if quest.get("stages") else quest)["goal"] = {"location": "Moon Base"}

        with self.assertRaisesRegex(ValueError, "Moon Base"):  # the builder's validation, before any play
            playtest.play(_scenario(world=self._world(break_it), max_turns=1))


class CompareTests(_Sandbox):
    def test_baseline_and_modified_storytellers_on_matched_seeds(self):
        from core_engine import Genome

        scenarios = [_scenario(seed=3, max_turns=10), _scenario(seed=4, max_turns=10, player="explorer")]
        modified = {"genome": Genome(rng=random.Random(99)).traits()}  # another storyteller than the seeds draw
        result = playtest.compare(scenarios, modified)
        self.assertEqual(len(result["pairs"]), 2)
        self.assertTrue(all(p["compatible"] for p in result["pairs"]))
        for pair in result["pairs"]:
            self.assertEqual(pair["baseline"].scenario.seed, pair["modified"].scenario.seed)
            self.assertEqual(pair["baseline"].state.start_location, pair["modified"].state.start_location)
        self.assertIn("main_quest.completed", result["summary"])
        self.assertIn("fitness.total", result["summary"])

    def test_different_worlds_are_refused_as_incompatible(self):
        result = playtest.compare([_scenario(quest=None, start=None, max_turns=3)], {"world": STATION})
        self.assertEqual(result["incompatible"], ["t"])
        self.assertEqual(result["summary"], {})


class _FakeRuntime:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def generate(self, prompt, max_new_tokens=512, temperature=0.3, **kwargs):
        self.prompts.append(prompt)
        return self.reply


class JudgeTests(_Sandbox):
    def test_the_judge_is_asked_for_the_playtest_aspects_and_kept_apart(self):
        scores = {name: 6 for name in CRITERIA}
        scores.update(agency=8, character_development=4)
        runtime = _FakeRuntime(json.dumps({"scores": scores, "engagement": 7, "verdict": "Fine.",
                                           "best_moment": "Turn 2", "weakest_moment": "Turn 5"}))
        run = playtest.run_scenario(_scenario(max_turns=5), judge=playtest.playtest_judge(runtime))
        self.assertIn("character_development", runtime.prompts[0])
        aspects = run.judge["aspects"]
        self.assertEqual(set(aspects), set(playtest.JUDGE_ASPECTS))
        self.assertEqual((aspects["choice_consequences"], aspects["character_development"], aspects["engagement"]),
                         (8, 4, 7))
        self.assertEqual(run.to_dict()["subjective"]["judge"]["verdict"], "Fine.")
        self.assertNotIn("judge", run.metrics)

    def test_other_judgements_keep_their_prompt(self):
        prompt = build_prompt("Turn 1: Hall: Look around -> A dusty hall.")
        self.assertNotIn("character_development", prompt)
        self.assertIn("character_development", build_prompt("Turn 1", PLAYTEST_CRITERIA))


class ReportTests(_Sandbox):
    def test_report_json_and_replay_are_written(self):
        runs = [playtest.run_scenario(_scenario(max_turns=8)),
                playtest.run_scenario(_scenario(world=STATION, quest="reboot_reactor", start="Docking Bay", max_turns=8))]
        out = Path(tempfile.mkdtemp())
        paths = playtest.write(out, runs, title="Test playtest")
        text = paths["report"].read_text(encoding="utf-8")
        for heading in ("## Scenarios", "## Automated measures", "## World checks", "## Issues found while playing",
                        "## Weak segments (for authors)", "## The judge's view (subjective)", "## Recommendations"):
            self.assertIn(heading, text)
        self.assertIn("Not run", text)
        self.assertEqual(text.count("The quest `reach_observatory` cannot"), 1)  # once, however many playthroughs
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(len(data["playthroughs"]), 2)
        page = paths["replay"].read_text(encoding="utf-8")
        self.assertIn('id="playtest-data"', page)
        self.assertIn("<title>Test playtest</title>", page)

    def test_the_replay_page_cannot_be_broken_by_story_text(self):
        page = replay.render({"title": "x", "playthroughs": [{"turns": [{"text": "</script><b>"}]}]})
        self.assertEqual(page.count("</script>"), 2)  # the data element's and the program's own
        self.assertIn("<\\/script>", page)

    def test_the_command_line(self):
        folder = Path(tempfile.mkdtemp())
        spec = {"scenarios": [{"name": "cli", "world": MONASTERY, "player": "random", "seed": 2, "max_turns": 5}]}
        (folder / "s.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
        with patch("sys.stdout"):
            playtest.main([str(folder / "s.yaml"), "--out", str(folder / "out"), "--compare-overrides",
                           json.dumps({"side_story.enabled": False})])
        self.assertTrue((folder / "out" / "report.md").is_file())
        self.assertIn("Baseline vs modified", (folder / "out" / "report.md").read_text(encoding="utf-8"))


class RegressionTests(_Sandbox):
    """The example worlds, played by every strategy on fixed seeds: no softlock, no contradiction, no
    quest out of reach for long, no excessive attacks. A failure here is a regression in the engine or
    the example worlds (run `python -m evaluator.playtest` on the scenario to see it turn by turn)."""

    def test_the_example_worlds_play_without_errors(self):
        scenarios, checks = playtest.load(ROOT / "evaluator" / "scenarios" / "side_content.yaml")
        for scenario in scenarios:
            run = playtest.run_scenario(scenario, checks)
            errors = [i for i in run.issues if i["severity"] == "error"]
            with self.subTest(scenario=scenario.name):
                self.assertEqual(errors, [])
                self.assertEqual(run.metrics["coherence"]["contradictions"], 0)
                self.assertIn(run.stopped, ("max_turns", "ended", "dead"))


if __name__ == "__main__":
    unittest.main()
