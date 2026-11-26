"""The visual replay of playtests (evaluator/playtest.py): one self-contained HTML page.

    html = replay.render(data)     # data: playtest.write's playtest.json content

The page shows, for each playthrough, every turn: the choices shown (the one taken marked, with its
kind and stable id), the story text that followed, the move between places, the changes of health,
coins and items, quest and side-quest progress, attacks, the people met so far, and the issues and
weak segments found at that turn with the world entries to edit. It only displays what was recorded:
it plays nothing and decides nothing. No network access is needed (no libraries, no fonts).

Keyboard: ← / → previous / next turn, Home / End first / last.
"""
from __future__ import annotations

import html
import json

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playtest Replay</title>
<style>
:root { --bg:#f7f7f5; --panel:#ffffff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e3e0; --accent:#2f6fde;
        --good:#1f8a4c; --bad:#c0392b; --warn:#b7791f; --chip:#eef2fb; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#141416; --panel:#1e1e21; --ink:#ececef;
        --muted:#a0a0a8; --line:#2e2e33; --accent:#79a6ff; --good:#4cc27e; --bad:#ff6b5e; --warn:#e0a84a; --chip:#262b38; } }
:root[data-theme="dark"] { --bg:#141416; --panel:#1e1e21; --ink:#ececef; --muted:#a0a0a8; --line:#2e2e33;
        --accent:#79a6ff; --good:#4cc27e; --bad:#ff6b5e; --warn:#e0a84a; --chip:#262b38; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:16px; border-bottom:1px solid var(--line); background:var(--panel); }
header h1 { margin:0 0 8px; font-size:20px; }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
select, button { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line); border-radius:6px;
                 padding:6px 10px; }
button { cursor:pointer; } button:disabled { opacity:.4; cursor:default; }
main { display:grid; grid-template-columns: 280px 1fr; gap:16px; padding:16px; max-width:1400px; margin:0 auto; }
@media (max-width: 800px) { main { grid-template-columns: 1fr; } }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:0; }
.panel h2 { font-size:14px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 8px; }
.timeline { display:flex; flex-direction:column; gap:2px; max-height:70vh; overflow:auto; }
.timeline button { text-align:left; border:none; background:none; padding:4px 6px; border-radius:6px; display:flex; gap:6px; }
.timeline button[aria-current="true"] { background:var(--chip); color:var(--accent); font-weight:600; }
.dot { width:8px; height:8px; border-radius:50%; margin-top:8px; flex:none; background:var(--line); }
.dot.new { background:var(--good); } .dot.flagged { background:var(--bad); }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap:12px; }
@media (max-width: 1000px) { .grid { grid-template-columns: 1fr; } }
.text { white-space:pre-wrap; background:var(--bg); border-radius:8px; padding:10px; max-height:280px; overflow:auto; }
ol.choices { margin:0; padding-left:20px; } ol.choices li { margin:2px 0; }
ol.choices li.chosen { font-weight:600; color:var(--accent); }
.kind { font-size:12px; color:var(--muted); margin-left:4px; }
.chip { display:inline-block; background:var(--chip); border-radius:999px; padding:1px 8px; font-size:12px; margin:2px 2px 2px 0; }
.good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); } .muted { color:var(--muted); }
table { border-collapse:collapse; width:100%; font-size:14px; } td, th { border-bottom:1px solid var(--line); padding:4px 6px; text-align:left; }
.issue { border-left:3px solid var(--bad); padding:4px 8px; margin:6px 0; background:var(--bg); border-radius:0 6px 6px 0; }
.issue.warning { border-color:var(--warn); } .issue.info { border-color:var(--muted); }
code { font-size:13px; }
.move { font-size:16px; }
</style>
</head>
<body>
<header>
  <h1 id="title">Playtest Replay</h1>
  <div class="row">
    <label for="run">Playthrough</label>
    <select id="run" data-testid="run-select"></select>
    <button id="first" data-testid="first" aria-label="First turn">⏮</button>
    <button id="prev" data-testid="prev" aria-label="Previous turn">◀</button>
    <span id="position" data-testid="position" class="muted"></span>
    <button id="next" data-testid="next" aria-label="Next turn">▶</button>
    <button id="last" data-testid="last" aria-label="Last turn">⏭</button>
    <span id="summary" data-testid="summary" class="muted"></span>
  </div>
</header>
<main>
  <section class="panel">
    <h2>Turns</h2>
    <div id="timeline" class="timeline" data-testid="timeline"></div>
  </section>
  <section style="display:flex;flex-direction:column;gap:12px;min-width:0">
    <div class="panel" data-testid="turn">
      <h2 id="turn-title">Turn</h2>
      <div id="move" class="move" data-testid="move"></div>
      <div class="grid" style="margin-top:8px">
        <div><h2>Choices shown</h2><ol id="choices" class="choices" data-testid="choices"></ol>
             <div id="free" class="muted" data-testid="free-choices"></div></div>
        <div><h2>What happened</h2><div id="text" class="text" data-testid="story-text"></div></div>
      </div>
    </div>
    <div class="grid">
      <div class="panel"><h2>State changes</h2><div id="changes" data-testid="changes"></div></div>
      <div class="panel"><h2>Quest progress</h2><div id="quest" data-testid="quest"></div></div>
    </div>
    <div class="grid">
      <div class="panel"><h2>People and relationships</h2><div id="people" data-testid="people"></div></div>
      <div class="panel"><h2>Issues and author notes at this turn</h2><div id="issues" data-testid="issues"></div></div>
    </div>
  </section>
</main>
<script type="application/json" id="playtest-data">__DATA__</script>
<script>
(function () {
  const data = JSON.parse(document.getElementById("playtest-data").textContent);
  const runs = data.playthroughs || [];
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]));
  let run = 0, turn = 0;
  $("title").textContent = data.title || "Playtest Replay";
  runs.forEach((r, i) => { const o = document.createElement("option"); o.value = i;
    o.textContent = r.scenario.name + " · " + r.scenario.player + " · seed " + r.scenario.seed; $("run").appendChild(o); });

  function issuesAt(r, t) {
    const own = (r.issues || []).filter((i) => i.turn === t);
    const segs = (r.weak_segments || []).filter((s) => s.at ? s.at.indexOf(t) >= 0 : (s.turns[0] <= t && t <= s.turns[1]));
    return {own, segs};
  }
  function newAt(r, t) {
    const e = r.turns[t - 1] || {};
    return !!((e.new_places || []).length || e.check || (e.met || []).length || (e.quest_done || []).length ||
              (e.side_quests || []).length || e.encounter || (e.items_gained || []).length || e.health_change || e.coin_change);
  }
  function edits(elements) {
    return (elements || []).filter((e) => e.name).map((e) =>
      '<span class="chip" title="' + esc(e.studio) + '">' + esc(e.kind) + ': ' + esc(e.name) + ' · <code>' + esc(e.api) + '</code></span>').join(" ");
  }
  function drawTimeline() {
    const r = runs[run], box = $("timeline"); box.innerHTML = "";
    r.turns.forEach((e, i) => {
      const t = i + 1, b = document.createElement("button"), found = issuesAt(r, t);
      b.dataset.turn = t; b.setAttribute("data-testid", "timeline-turn");
      const dot = found.own.length || found.segs.length ? "flagged" : newAt(r, t) ? "new" : "";
      b.innerHTML = '<span class="dot ' + dot + '"></span><span>' + t + ". " + esc(e.action) + "</span>";
      b.onclick = () => { turn = i; draw(); };
      box.appendChild(b);
    });
  }
  function people(r, t) {
    const met = {};
    (r.people || []).forEach((p) => { if (p.first_turn != null && p.first_turn <= t) met[p.name] = {talks: 0, first: p.first_turn}; });
    r.turns.slice(0, t).forEach((e) => {
      (e.met || []).forEach((n) => { met[n] = met[n] || {talks: 0, first: e.turn}; });
      if (e.with) { met[e.with] = met[e.with] || {talks: 0, first: e.turn}; met[e.with].talks += 1; }
    });
    const final = {}; (r.people || []).forEach((p) => final[p.name] = p);
    const names = Object.keys(met);
    if (!names.length) return '<span class="muted">Nobody met yet.</span>';
    return "<table><tr><th>Person</th><th>Talks</th><th>Met</th><th>By the end</th></tr>" + names.map((n) => {
      const p = final[n] || {}, flags = [p.ally ? '<span class="good">ally</span>' : "", p.hostile ? '<span class="bad">hostile</span>' : "",
        p.gone ? '<span class="muted">gone</span>' : ""].filter(Boolean).join(", ");
      return "<tr><td>" + esc(n) + "</td><td>" + met[n].talks + "</td><td>" + (met[n].first ? "turn " + met[n].first : "at the start") + "</td><td>" + (flags || "–") + "</td></tr>";
    }).join("") + "</table>";
  }
  function draw() {
    const r = runs[run]; if (!r) return;
    const n = r.turns.length; turn = Math.max(0, Math.min(turn, n - 1));
    const e = r.turns[turn] || {}, t = turn + 1;
    $("position").textContent = n ? ("Turn " + t + " of " + n) : "No turns";
    const q = r.quest || {}, m = r.metrics || {};
    $("summary").textContent = (q.title ? "Quest: " + q.title + " · " : "") + "start " + r.start + " · stopped: " + r.stopped +
      " · " + ((m.main_quest || {}).completed ? "quest completed" : "quest not completed");
    ["first", "prev"].forEach((id) => $(id).disabled = turn <= 0);
    ["next", "last"].forEach((id) => $(id).disabled = turn >= n - 1);
    $("turn-title").textContent = "Turn " + t + (e.event ? " · during " + e.event : "");
    $("move").innerHTML = e.moved_to ? esc(e.where) + " → <strong>" + esc(e.moved_to) + "</strong>" +
      ((e.new_places || []).length ? ' <span class="chip">new place</span>' : "") : "At " + esc(e.where);
    const decision = (r.decisions || []).find((d) => d.turn === t && d.took_turn);
    const free = (r.decisions || []).filter((d) => d.turn === t && !d.took_turn);
    $("choices").innerHTML = decision ? decision.options.map((o, i) =>
      '<li class="' + (i === decision.chosen ? "chosen" : "") + '" data-testid="' + (i === decision.chosen ? "chosen-choice" : "choice") + '">' +
      esc(o.name) + '<span class="kind">' + esc(o.source) + (o.owner ? " · " + esc(o.owner) : "") + '</span></li>').join("")
      : "<li>" + esc(e.action) + ' <span class="kind">(no recorded choice list)</span></li>';
    $("free").innerHTML = free.length ? "Before it, without a turn: " + free.map((d) => esc(d.options[d.chosen].name)).join("; ") : "";
    $("text").textContent = e.text || e.told || "";
    const changes = [];
    if (e.check) changes.push("Check " + esc(e.check.stat) + ": rolled " + e.check.roll + " (needed ≤ " + e.check.needed + ") → " +
      (e.check.passed ? '<span class="good">passed</span>' : '<span class="bad">failed</span>'));
    if (e.health_change) changes.push("Health " + (e.health_change > 0 ? "+" : "") + e.health_change + " → " + e.health);
    if (e.coin_change) changes.push("Coins " + (e.coin_change > 0 ? "+" : "") + e.coin_change + " → " + e.coins);
    (e.items_gained || []).forEach((i) => changes.push('<span class="good">+ ' + esc(i) + "</span>"));
    (e.items_lost || []).forEach((i) => changes.push('<span class="bad">− ' + esc(i) + "</span>"));
    (e.met || []).forEach((p) => changes.push("Met " + esc(p)));
    if (e.encounter) changes.push('<span class="warn">Attack: ' + esc(e.encounter.event) + " (" + esc(e.encounter.category) + ")</span>");
    if (e.ended) changes.push("<strong>The story ended</strong>" + (e.end_reason ? ": " + esc(e.end_reason) : ""));
    $("changes").innerHTML = changes.length ? changes.map((c) => "<div>" + c + "</div>").join("") : '<span class="muted">Nothing changed (a filler turn).</span>';
    const quest = [];
    (e.quest_done || []).forEach((s) => quest.push('<div class="good">✓ ' + esc(s) + "</div>"));
    if (e.quest_complete) quest.push('<div class="good"><strong>Quest complete</strong></div>');
    if (e.quest_now) quest.push("<div>Now: " + esc(e.quest_now) + "</div>");
    (e.side_quests || []).forEach((s) => quest.push('<div><span class="chip">side quest</span>' + esc(s.title) + ": " + esc(s.what) + "</div>"));
    $("quest").innerHTML = quest.length ? quest.join("") : '<span class="muted">No quest progress this turn.</span>';
    $("people").innerHTML = people(r, t);
    const found = issuesAt(r, t), notes = [];
    found.own.forEach((i) => notes.push('<div class="issue ' + esc(i.severity) + '" data-testid="issue"><strong>' + esc(i.code) + "</strong>: " +
      esc(i.message) + "<div>" + edits(i.elements) + "</div></div>"));
    found.segs.forEach((s) => notes.push('<div class="issue warning" data-testid="weak-segment"><strong>' + esc(s.kind) + "</strong> (turns " +
      s.turns[0] + "–" + s.turns[1] + "): " + esc(s.why) + "<div class=\\"muted\\">" + esc(s.suggestion) + "</div><div>" + edits(s.elements) + "</div></div>"));
    $("issues").innerHTML = notes.length ? notes.join("") : '<span class="muted">None at this turn.</span>';
    document.querySelectorAll("#timeline button").forEach((b) => b.setAttribute("aria-current", String(Number(b.dataset.turn) === t)));
  }
  $("run").onchange = (ev) => { run = Number(ev.target.value); turn = 0; drawTimeline(); draw(); };
  $("first").onclick = () => { turn = 0; draw(); };
  $("prev").onclick = () => { turn -= 1; draw(); };
  $("next").onclick = () => { turn += 1; draw(); };
  $("last").onclick = () => { turn = 1e9; draw(); };
  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "SELECT") return;
    if (ev.key === "ArrowRight") { turn += 1; draw(); }
    else if (ev.key === "ArrowLeft") { turn -= 1; draw(); }
    else if (ev.key === "Home") { turn = 0; draw(); }
    else if (ev.key === "End") { turn = 1e9; draw(); }
  });
  if (runs.length) { drawTimeline(); draw(); } else { $("position").textContent = "No playthroughs in this file."; }
})();
</script>
</body>
</html>
"""


def render(data):
    """The replay page of playtest data (playtest.json's content), self-contained."""
    payload = json.dumps(data, default=_jsonable, ensure_ascii=False)
    # Inside <script>: never close the element early.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    return _TEMPLATE.replace("__DATA__", payload).replace("<title>Playtest Replay</title>",
                                                          f"<title>{html.escape(_short(data.get('title')))}</title>")


def _short(title):
    words = str(title or "Playtest Replay").split()
    return " ".join(words[:4]) or "Playtest Replay"


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)
