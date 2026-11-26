"""Play games in a running studio's ▶ Play section with a browser (Playwright): live UI playtests.

    pip install playwright && playwright install chromium
    python -m evaluator.live_playtest --url https://ane-ui.hamlet.iti.gr/?section=play \\
        --name P1 --strategy main --turns 25 --quest "Find a shield" --engine 3a7c8120 --out live-out

It opens the Play section, chooses the player profile, the trained AI (or ``--recorded`` engine) and the
quest, starts a new game, then each turn reads what the page shows (the status bar, the scene, the
turn card, the quest card, the "You" card and the choice buttons), takes a choice by a simple strategy
and waits for the page to change. Every turn is logged to ``<out>/runs/<name>.json`` (choices shown, the
choice taken, what followed, the time it took) with screenshots in ``<out>/shots/``. ``--save-at N``
saves the game at turn N, opens a fresh browser and continues from the file.

It only clicks what a player can click: the studio and its API run the game. Strategies read only the
page: ``main`` (the quest card's words), ``explore`` (talk, travel, look, side quests), ``fight``,
``avoid``. Games are played on the server it points at (they are saved there like anyone's), so use a
profile of its own (``--profile``).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time
from pathlib import Path

DEFAULT_URL = "https://ane-ui.hamlet.iti.gr/?section=play"
FIGHT = re.compile(r"\b(fight|attack|brawl|challenge|confront|duel|strike|ambush|bout|beat|corner|"
                   r"draw (your|a) (blade|weapon|sword)|stand your ground|hit|punch|charge|battle|threaten)\b", re.I)
AVOID = re.compile(r"\b(flee|run|sneak|hide|slip|avoid|retreat|escape|back away|leave|evade|bribe|calm|"
                   r"talk (them|him|her) down|pay)\b", re.I)
TALK = re.compile(r"^\(|\b(talk|ask|chat|greet|approach|speak|listen|gossip)\b", re.I)
SIDE = re.compile(r"\b(side quest|take it on|accept|agree|help (him|her|them)|offer|job|favour|favor|task)\b", re.I)
MOVE = re.compile(r"\b(go to|walk to|head to|travel|enter|return to|take the|leave for|visit)\b", re.I)
STOP_WORDS = {"with", "from", "them", "make", "until", "their", "that", "this", "talk"}


def _text(locator):
    try:
        if locator.count() and locator.first.is_visible():
            return locator.first.inner_text().strip()
    except Exception:
        pass
    return ""


class Studio:
    """A browser on the studio's Play section."""

    def __init__(self, playwright, url, shots, headless=True):
        self.url, self.shots = url, Path(shots)
        self.browser = playwright.chromium.launch(headless=headless)
        self.context = self.browser.new_context(viewport={"width": 1500, "height": 1100}, accept_downloads=True)
        self.page = self.context.new_page()
        self.console_errors = []
        self.page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text[:300]))
        self.page.on("pageerror", lambda e: self.console_errors.append("pageerror: " + str(e)[:300]))

    def open(self):
        """Load the page (seconds it took)."""
        started = time.time()
        self.page.goto(self.url, timeout=150000)
        self.page.wait_for_selector("text=New game", timeout=150000)
        self.page.wait_for_timeout(4000)
        return round(time.time() - started, 1)

    def fresh(self):
        """A new browser context (no kept session), the page loaded again."""
        self.context.close()
        self.context = self.browser.new_context(viewport={"width": 1500, "height": 1100}, accept_downloads=True)
        self.page = self.context.new_page()
        return self.open()

    def shot(self, name):
        path = self.shots / f"{name}.png"
        try:
            self.page.screenshot(path=str(path), timeout=60000)
        except Exception as exc:
            print("screenshot failed", exc)
        return str(path)

    def dropdown(self, label, text, by_label=True):
        """Choose the option containing ``text`` in a Gradio dropdown: (chosen, every option)."""
        field = self.page.get_by_label(label).first if by_label else self.page.locator(label).first
        field.click()
        self.page.wait_for_timeout(600)
        options = self.page.locator("ul[role=listbox] li:visible")
        names = [o.strip().lstrip("✓").strip() for o in options.all_inner_texts()]
        for index, name in enumerate(names):
            if text.lower() in name.lower():
                options.nth(index).click()
                self.page.wait_for_timeout(1200)
                return name, names
        self.page.keyboard.press("Escape")
        raise RuntimeError(f"No option {text!r} in {names}")

    def set_profile(self, profile):
        field = self.page.get_by_label("Player profile").first
        field.click()
        field.fill(profile)
        field.press("Enter")
        self.page.wait_for_timeout(2000)

    def screen(self):
        """What the page shows now."""
        page = self.page
        return {"ending": _text(page.locator(".ending")), "hud": _text(page.locator(".play-hud")),
                "scene": _text(page.locator(".scene-card")), "turn": _text(page.locator(".turn-card")),
                "quest": _text(page.locator(".quest-card")), "you": _text(page.locator(".you-card")),
                "choices": [c.strip() for c in page.locator("button.choice:visible").all_inner_texts()],
                "toasts": [t.strip() for t in page.locator(".toast-wrap").all_inner_texts() if t.strip()]}

    def wait_change(self, before, timeout=120):
        """(screen, seconds, changed): wait until the page settles on something new after a click."""
        started = time.time()
        old = (before["hud"], before["turn"], tuple(before["choices"]), before["ending"])
        last, stable = None, 0
        while time.time() - started < timeout:
            self.page.wait_for_timeout(400)
            now = self.screen()
            key = (now["hud"], now["turn"], tuple(now["choices"]), now["ending"])
            pending = self.page.locator(".pending:visible, .generating:visible").count()
            if key != old and not pending:
                stable = stable + 1 if key == last else 0
                if stable >= 2:
                    return now, round(time.time() - started, 2), True
                last = key
            if now["toasts"] and now["toasts"] != before["toasts"] and not pending and time.time() - started > 3:
                return now, round(time.time() - started, 2), key != old
        return self.screen(), round(time.time() - started, 2), False


def quest_words(card):
    """The words of the quest card's current step (what a ``main`` player looks for)."""
    found = re.search(r"To do now:(.*)", card)
    source = found.group(1) if found else ""
    current = re.search(r"▶️\s*(.*)\n(.*)", card)
    if current:
        source += " " + current.group(1) + " " + current.group(2)
    return {w.lower() for w in re.findall(r"[A-Za-z']{4,}", source)} - STOP_WORDS


def choose(strategy, choices, screen, counts, rng):
    """(index, name, ranking): the choice a strategy takes among the buttons shown."""
    wanted = quest_words(screen["quest"])
    scored = []
    for index, label in enumerate(choices):
        name = re.split(r"\s+·\s+🎲", re.sub(r"^\d+\.\s*", "", label))[0].strip()
        low = name.lower()
        on_quest = sum(1 for w in re.findall(r"[a-z']{4,}", low) if w in wanted)
        score = rng.random() * 0.6 - 2.0 * counts.get(name, 0) - (1.2 if "leave the conversation" in low else 0.0)
        if strategy == "main":
            score += 1.5 * on_quest
        elif strategy == "explore":
            score += 2.5 * bool(SIDE.search(name)) + 1.2 * bool(TALK.search(name)) + 1.0 * bool(MOVE.search(name))
            score += 0.8 * bool(re.search(r"explore|look|search|inspect|examine|investigate", low))
        elif strategy == "fight":
            score += 3.0 * bool(FIGHT.search(name)) + 0.5 * on_quest
        elif strategy == "avoid":
            score += -3.0 * bool(FIGHT.search(name)) + 1.5 * bool(AVOID.search(name)) + 0.8 * on_quest
            score -= 1.0 if "attack" in label.lower() and "🎲" in label else 0.0
        scored.append((score, index, name))
    scored.sort(reverse=True)
    return scored[0][1], scored[0][2], [(round(s, 2), n) for s, _, n in scored]


def _save_and_continue(studio, args, out, turn, screen):
    """Save the game, open a fresh browser and continue from the file: whether it came back the same."""
    event = {"turn": turn, "kind": "save_and_continue"}
    path = out / "runs" / f"{args.name}_save_t{turn}.json"
    studio.page.get_by_role("button", name="💾 Save game").click()
    studio.page.wait_for_timeout(4000)
    links = studio.page.locator("a[download]:visible")
    if not links.count():
        event["error"] = "no save file was offered"
        return event, screen
    with studio.page.expect_download(timeout=60000) as download:
        links.first.click()
    download.value.save_as(str(path))
    studio.shot(f"{args.name}_saved")
    studio.fresh()
    studio.set_profile(args.profile)
    before = studio.screen()
    studio.page.locator("input[type=file][accept='.json']").first.set_input_files(str(path))
    after, seconds, changed = studio.wait_change(before, timeout=120)
    event.update({"file": str(path), "continue_s": seconds, "continued": changed,
                  "same_hud": after["hud"] == screen["hud"], "same_choices": after["choices"] == screen["choices"]})
    studio.shot(f"{args.name}_continued")
    return event, after


def play(args):
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    log = {"name": args.name, "url": args.url, "strategy": args.strategy, "profile": args.profile, "seed": args.seed,
           "turns": [], "events": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    path = out / "runs" / f"{args.name}.json"

    def dump():
        path.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")

    with sync_playwright() as playwright:
        studio = Studio(playwright, args.url, out / "shots", headless=not args.headful)
        log["page_load_s"] = studio.open()
        studio.set_profile(args.profile)
        if args.engine:
            log["engine"], log["engine_options"] = studio.dropdown("Trained AI", args.engine)
        else:
            log["engine"] = studio.page.get_by_label("Trained AI").first.input_value()
        if args.quest:
            log["quest"], log["quest_options"] = studio.dropdown("#play-quest input", args.quest, by_label=False)
        before = studio.screen()
        if args.recorded:
            studio.page.get_by_role("button", name=re.compile("Another engine")).click()
            studio.page.wait_for_timeout(800)
            log["engine"], _ = studio.dropdown("Recorded engine", args.recorded)
            studio.page.get_by_role("button", name="▶ Play with it").click()
        else:
            studio.page.get_by_role("button", name="▶ New game").click()
        screen, seconds, _ = studio.wait_change(before, timeout=150)
        log.update(start_s=seconds, start_screen=screen)
        studio.shot(f"{args.name}_t00")
        counts = {}
        for turn in range(1, args.turns + 1):
            if screen["ending"] or not screen["choices"]:
                log["events"].append({"turn": turn, "kind": "ended_or_no_choices", "ending": screen["ending"]})
                break
            screen = studio.screen()  # read again just before clicking: the buttons may have changed
            index, name, ranking = choose(args.strategy, screen["choices"], screen, counts, rng)
            counts[name] = counts.get(name, 0) + 1
            try:
                studio.page.locator("button.choice:visible").nth(index).click(timeout=15000)
            except Exception as exc:
                log["events"].append({"turn": turn, "kind": "click_failed", "index": index, "error": str(exc)[:600]})
                studio.shot(f"{args.name}_t{turn:02d}_click_failed")
                continue
            after, seconds, changed = studio.wait_change(screen)
            record = {"turn": turn, "choices": screen["choices"], "chosen": name, "chosen_index": index,
                      "ranking": ranking, "secs": seconds, "changed": changed, **{k: after[k] for k in
                      ("hud", "turn", "scene", "quest", "you", "ending", "toasts")}}
            if not changed:
                record["problem"] = "the page did not change after the click"
                studio.shot(f"{args.name}_t{turn:02d}_nochange")
            log["turns"].append(record)
            print(f"T{turn} [{seconds}s] {name} | {after['hud'][:120]}")
            if turn % 5 == 0 or after["ending"] or after["toasts"]:
                studio.shot(f"{args.name}_t{turn:02d}")
            screen = after
            dump()
            if args.save_at and turn == args.save_at:
                event, screen = _save_and_continue(studio, args, out, turn, screen)
                log["events"].append(event)
                dump()
        log["console_errors"] = studio.console_errors[:50]
        log["final_screen"] = studio.screen()
        studio.shot(f"{args.name}_final")
        dump()
        studio.browser.close()
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="the studio's Play section")
    parser.add_argument("--name", required=True, help="the playthrough's name (files are named after it)")
    parser.add_argument("--strategy", default="main", choices=["main", "explore", "fight", "avoid"])
    parser.add_argument("--turns", type=int, default=25)
    parser.add_argument("--quest", help="part of the quest's name in the quest list")
    parser.add_argument("--engine", help="part of the trained AI's label")
    parser.add_argument("--recorded", help="part of a recorded engine's label (Another engine)")
    parser.add_argument("--profile", default="playtest", help="the player profile games are saved under")
    parser.add_argument("--seed", type=int, default=1, help="the strategy's own dice (the story's are the server's)")
    parser.add_argument("--save-at", type=int, default=0, help="save at this turn, then continue from the file")
    parser.add_argument("--out", default="live-playtest")
    parser.add_argument("--headful", action="store_true")
    print("log:", play(parser.parse_args(argv)))


if __name__ == "__main__":
    main()
