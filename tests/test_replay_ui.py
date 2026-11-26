"""Browser tests of the playtest replay page (evaluator/replay.py) with Playwright.

The page is rendered from a small fixed playtest (no engine needed) and driven in headless Chromium:
the turn timeline, previous/next and keyboard navigation, the choices shown with the one taken, the
story text, state changes, quest progress, people met, and the issues and author notes with the
entries to edit. Skipped when Playwright or its browser is not installed:

    pip install playwright && playwright install chromium
    python -m unittest tests.test_replay_ui
"""
import tempfile
import unittest
from pathlib import Path

from evaluator import replay

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None

DATA = {
    "title": "Replay test",
    "playthroughs": [
        {
            "scenario": {"name": "monastery", "player": "side_quester", "seed": 3},
            "start": "Mountain Gate", "stopped": "max_turns",
            "quest": {"key": "return_the_relic", "title": "Find the lost relic"},
            "metrics": {"main_quest": {"completed": False}},
            "people": [{"name": "Abbot", "first_turn": 0, "ally": False, "hostile": False, "gone": False},
                       {"name": "Warden", "ally": False, "hostile": False, "gone": False},
                       {"name": "Hermit", "ally": True, "hostile": False, "gone": False}],
            "turns": [
                {"turn": 1, "action": "(Warden) Slip past the warden", "where": "Mountain Gate", "moved_to": None,
                 "with": "Warden", "met": ["Warden"], "check": {"stat": "stealth", "roll": 80, "needed": 40, "passed": False},
                 "health": 8, "health_change": -2, "coins": 0, "coin_change": 0, "text": "The warden catches you.</script>"},
                {"turn": 2, "action": "Travel to the Glacier Path", "where": "Mountain Gate", "moved_to": "Glacier Path",
                 "new_places": ["Glacier Path"], "health": 8, "health_change": 0, "coins": 0, "coin_change": 0,
                 "encounter": {"event": "glacier_thieves", "category": "ambush"}, "text": "Thieves block the ledge."},
                {"turn": 3, "action": "(Hermit) Ask about the relic", "where": "Glacier Path", "with": "Hermit",
                 "met": ["Hermit"], "side_quests": [{"title": "The thieves' hideout", "what": "started"}],
                 "quest_done": ["Learn where the relic went"], "quest_now": "Bring the relic back",
                 "items_gained": ["Map"], "text": "The hermit tells you of a hideout."},
            ],
            "decisions": [
                {"turn": 1, "took_turn": True, "chosen": 1, "options": [
                    {"name": "Ring the gate bell", "source": "location"},
                    {"name": "(Warden) Slip past the warden", "source": "npc", "owner": "Warden"}]},
                {"turn": 2, "took_turn": True, "chosen": 0, "options": [
                    {"name": "Travel to the Glacier Path", "source": "navigation", "owner": "Glacier Path"},
                    {"name": "Return to Cloister", "source": "navigation"}]},
                {"turn": 3, "took_turn": False, "chosen": 0, "options": [{"name": "Approach Hermit", "source": "approach"}]},
                {"turn": 3, "took_turn": True, "chosen": 0, "options": [
                    {"name": "(Hermit) Ask about the relic", "source": "conversation", "owner": "Hermit"},
                    {"name": "Leave the conversation", "source": "leave"}]},
            ],
            "issues": [{"code": "encounters_back_to_back", "severity": "warning", "turn": 2,
                        "message": "Attacks came back to back.",
                        "elements": [{"kind": "event", "name": "glacier_thieves", "studio": "?section=world",
                                      "api": "/v1/entities/Events/glacier_thieves"}]}],
            "weak_segments": [{"kind": "dead_end_failure", "turns": [1, 1], "why": "Slipping past failed and led nowhere.",
                               "suggestion": "Give its failure a consequence.",
                               "elements": [{"kind": "npc", "name": "Warden", "studio": "?section=world",
                                             "api": "/v1/entities/NPCs/Warden"}]},
                              {"kind": "loop", "turns": [1, 3], "at": [1, 3], "why": "Asked twice.",
                               "suggestion": "Limit it.", "elements": []}],
        },
        {"scenario": {"name": "station", "player": "fighter", "seed": 4}, "start": "Docking Bay", "stopped": "ended",
         "quest": {"title": "Reboot the reactor"}, "metrics": {"main_quest": {"completed": True}}, "people": [],
         "turns": [{"turn": 1, "action": "Reboot the reactor", "where": "Reactor Core", "quest_complete": True,
                    "ended": True, "end_reason": "The quest is done.", "text": "The core hums back to life."}],
         "decisions": [], "issues": [], "weak_segments": []},
    ],
}


@unittest.skipIf(sync_playwright is None, "Playwright is not installed (pip install playwright)")
class ReplayPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page_path = Path(tempfile.mkdtemp()) / "replay.html"
        cls.page_path.write_text(replay.render(DATA), encoding="utf-8")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - the browser is not installed
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium for Playwright is not installed: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.page_path.as_uri())

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def t(self, test_id):
        return self.page.get_by_test_id(test_id)

    def test_the_first_turn_shows_choices_text_changes_and_author_notes(self):
        self.assertEqual(self.t("timeline-turn").count(), 3)
        self.assertEqual(self.t("position").inner_text(), "Turn 1 of 3")
        self.assertEqual(self.t("chosen-choice").inner_text().split("npc")[0].strip(), "(Warden) Slip past the warden")
        self.assertEqual(self.t("choice").count(), 1)
        self.assertIn("The warden catches you.</script>", self.t("story-text").inner_text())  # shown as text
        changes = self.t("changes").inner_text()
        self.assertIn("rolled 80", changes)
        self.assertIn("failed", changes)
        self.assertIn("Health -2", changes)
        self.assertIn("Slipping past failed", self.t("weak-segment").first.inner_text())
        self.assertIn("/v1/entities/NPCs/Warden", self.t("issues").inner_text())
        self.assertTrue(self.page.get_by_test_id("prev").is_disabled())

    def test_navigation_by_buttons_keyboard_and_timeline(self):
        self.t("next").click()
        self.assertEqual(self.t("position").inner_text(), "Turn 2 of 3")
        self.assertIn("Glacier Path", self.t("move").inner_text())
        self.assertIn("new place", self.t("move").inner_text())
        self.assertIn("Attack: glacier_thieves", self.t("changes").inner_text())
        self.assertIn("encounters_back_to_back", self.t("issue").inner_text())
        self.page.keyboard.press("ArrowRight")
        self.assertEqual(self.t("position").inner_text(), "Turn 3 of 3")
        self.assertTrue(self.page.get_by_test_id("next").is_disabled())
        self.page.keyboard.press("Home")
        self.assertEqual(self.t("position").inner_text(), "Turn 1 of 3")
        self.t("timeline-turn").nth(2).click()
        self.assertEqual(self.t("position").inner_text(), "Turn 3 of 3")
        self.assertEqual(self.page.locator('[data-testid="timeline-turn"][aria-current="true"]').count(), 1)

    def test_a_loop_note_marks_only_the_turns_it_was_taken(self):
        self.assertIn("Asked twice.", self.t("issues").inner_text())
        self.t("next").click()
        self.assertNotIn("Asked twice.", self.t("issues").inner_text())  # turn 2: not one of the loop's turns
        self.t("next").click()
        self.assertIn("Asked twice.", self.t("issues").inner_text())
        dot = self.page.locator('[data-testid="timeline-turn"] .dot').first.bounding_box()
        self.assertLessEqual(abs(dot["width"] - dot["height"]), 1)  # a round marker, not a bar

    def test_quest_side_quests_people_and_free_choices(self):
        self.t("last").click()
        quest = self.t("quest").inner_text()
        self.assertIn("Learn where the relic went", quest)
        self.assertIn("Now: Bring the relic back", quest)
        self.assertIn("The thieves' hideout: started", quest)
        self.assertIn("Approach Hermit", self.t("free-choices").inner_text())
        people = self.t("people").inner_text()
        self.assertIn("Warden", people)
        self.assertIn("at the start", people)  # the Abbot, met in the opening scene
        self.assertIn("ally", people)  # the Hermit, by the end

    def test_switching_playthroughs(self):
        self.t("run-select").select_option("1")
        self.assertEqual(self.t("position").inner_text(), "Turn 1 of 1")
        self.assertIn("Quest complete", self.t("quest").inner_text())
        self.assertIn("The story ended", self.t("changes").inner_text())
        self.assertIn("(no recorded choice list)", self.t("choices").inner_text())
        self.assertIn("quest completed", self.t("summary").inner_text())


if __name__ == "__main__":
    unittest.main()
