# Limitations review

Every limitation, caveat and open point reported in this chat, from 6 to 20 November 2026, checked
one by one against the code as it stands on 21 November 2026.

- **Code state:** the latest commit is `5491381` (it includes the job reattach and cancellation
  work). The 21 November fixes are in the working tree, not committed.
- **How each item was checked:** by reading the code and tests (file and line given), plus the full test
  suite (29 modules, 629 tests after the 21 November fixes, all passing) and the browser checks.
- **What was not re-checked:** the engine experiment figures (marked 🔍). Re-measuring them means
  training engines again, which CLAUDE.md rules out for validation.

**Status key**

| Mark | Meaning |
|---|---|
| ✅ | Solved: the fix is in the code and covered by a test or browser check |
| 🟡 | Partly solved: improved, but part of it remains |
| ⏳ | Open: still true and could be worked on |
| 📌 | Accepted: by design, inherent, or outside the code (nothing to fix, or not fixable here) |
| 🔍 | Not re-checked: needs an experiment re-run |

## Summary

| Status | Count |
|---|---|
| ✅ Solved | 42 |
| 🟡 Partly solved | 4 |
| ⏳ Open | 7 |
| 📌 Accepted | 20 |
| 🔍 Not re-checked | 7 |
| **Total** | **80** |

Ten of the open items were fixed on 21 November (#15, #21, #22, #23, #45, #58, #61, #62, #63 and
#65). What is still open needs you, or another session, or more testing:
- rebuilding the containers (#18) and committing the 21 November fixes (#80);
- the other session's FRONTEND.md view texts (#59);
- jobs lost on an API restart, as no persistence was added (#69);
- untested: a 390 px phone width (#19) and a real judge job in the browser (#71);
- Gradio's internal `.tab-wrapper` class (#20).

## 1. Engine and experiments (6–9 and 18 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 1 | Clue gain partly came from matching any payoff to any open clue (40% under strict matching) | 🔍 | A measurement caveat of that experiment; not re-measured |
| 2 | 10% of wins came before step 12 | 🔍 | No later change targeted it; not re-measured |
| 3 | Ambushes averaged 2.25 per story | 🔍 | Not re-measured |
| 4 | 7 stalls among random storytellers: prisoners who never escaped the Dungeon | 🔍 | Since 9 Nov a prisoner's escape is listed first among the choices, which should help; not re-measured |
| 5 | Studio limits rose (100 steps, 60,000 turns of work) and the suite took 2.3 min | 📌 | Superseded: the work limit is now 1,000,000 (`backend/store.py:193`). The suite is larger (625 tests) |
| 6 | Evaluation caveats: automated players as proxies, 10 games per player type (±15 points), one training run per variant | 📌 | Inherent to that experiment's design |
| 7 | Station traders still said "the reactor runs too hot" after the reboot | ✅ | The rumour stops once the reactor is fixed: `until: reactor_fixed: true` (`examples/orbital_station.yaml:70-76`) |
| 8 | One coherence finding left: a repeated turn in the Monastery (1 in 14,326 turns) | 🔍 | Not re-measured |
| 9 | In the small Monastery world, short training did not beat random storytellers | 🔍 | Needs training to re-check |
| 10 | Stories of 24–32 turns, short of the score's 35-turn target | 🔍 | Not re-measured |
| 11 | The Station's win rate dropped from 28 to 25 of 30 (longer routes) | 📌 | A consequence of the corrected map, not a defect |
| 12 | Engines, records and learning memory from before 18 Nov were scored with the storyteller playing, so their fitness is not comparable | 📌 | Still true of old artifacts. Use *Forget what the AI learned* before clean comparisons |
| 13 | CI: the C# (Unity) test had never run, and the retired `macos-14` runner cancelled a job | 🟡 | The runtime workflow now uses `macos-15` (`.github/workflows/runtime.yml:24`). CI results can't be seen from here (`gh` is not signed in) |

## 2. Studio and API: navigation, training and Train (19 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 14 | Suggestions and Grow the world need the judge service running | 📌 | A service requirement (`docker compose --profile judge up -d judge`) |
| 15 | `python main.py` augmentation still writes the config itself (not review-first) | ✅ | Review first since 21 Nov: `python main.py` keeps a proposal in `augmentations/run_*/proposal.yaml`; `--apply-augmentation FILE` adds it, checked as the studio checks it (tested) |
| 16 | A training run silently started an unused game | ✅ | The studio trains with `start_game: false` (`app.py:1187`) |
| 17 | `session_id` on the training response became nullable in the API schema | 📌 | A compatible change (`api.py:283`); clients that don't send `start_game` see no difference |
| 18 | The running containers serve the old code until rebuilt | ⏳ | Applies to all of this work. Needs your build and deploy of `hamlet-ane:latest` |
| 19 | A 390 px phone width was not tested (headless Firefox stops at 500 px) | ⏳ | Still untested |
| 20 | Hiding Gradio's own tab bar relies on its internal `.tab-wrapper` class | ⏳ | `app.py:248-249`; a Gradio upgrade could bring the old tab bar back |
| 21 | Switching destinations doesn't update the address bar (`?section=` works only when a page is opened) | ✅ | Fixed 21 Nov: the address follows the destination open, however it was opened (`DRAFT_HEAD` observer; checked in a browser) |
| 22 | The banner highlight updates after a server round trip | ✅ | Fixed 21 Nov: a choice is marked at once (`data-pending`), except with unsaved changes, where the guard decides (checked in a browser) |
| 23 | Fast-travel turns (`travel.max_free_turns`) can lengthen a story, but the work count leaves them out | ✅ | Fixed 21 Nov: `story_turns` counts them when the steps allow it; `GET /v1/simulations/work` adds `fast_travel_turns` (tested) |
| 24 | The app-parameters check used its own simpler work formula | ✅ | Obsolete: those parameters are retired (`backend/store.py:211`); one formula, `training_work`, remains |
| 25 | The saved training defaults (48,000,000 turns) exceeded the 1,000,000 limit | ✅ | Defaults are now 40 × 25 × 2 (`config/hyperparameters.yaml:2-4`) |
| 26 | The warm-start count was only a preview | ✅ | The run reports its real count (`warm_started`, `backend/sessions.py:634`) |
| 27 | ▶ Play this result only selected the engine | ✅ | It starts the game with that engine (`play_result_ui`, `app.py:1430`) |
| 28 | The result card was lost on reload | ✅ | Kept in the browser (`ane-train-result`, `app.py:6175`) |
| 29 | Records was a sub-tab of Train | ✅ | Split by purpose across Train, Evaluate, Export and ⚙ Advanced → 🗂 Records |

## 3. Editor drafts, deletion and tuning parameters (19–20 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 30 | Missions, Story rules and the Map had no draft tracking | ✅ | The form editors are tracked (`form_emit`, e.g. the map at `app.py:6970`) |
| 31 | Every edit sent the whole draft (about 66 values) to the server | ✅ | Each part's status sends only that part's values |
| 32 | The baseline was captured in a separate round trip (a keystroke could count as loaded) | ✅ | Set in the same event as the load (`entry_baselined`, `app.py:5426`) |
| 33 | Renaming an entry while another form had unsaved changes saved that draft later under the new name | ✅ | Superseded by **Save changes**, which saves the whole entry at once, rename last (`app.py:4230`) |
| 34 | Closing the tab can only show the browser's own Leave/Stay, not Save | 🟡 | Browsers allow nothing else. The draft is now kept in the browser (`ane-entry-draft`) and offered back with ↺ Restore |
| 35 | Undo was lost when the API restarted or the page reloaded | ✅ | Deletions persist in `deletions.json` (`backend/content.py:1498`); the browser keeps the tokens |
| 36 | Each panel could undo only its latest deletion | ✅ | Undo histories per panel (`ane-undo-*`, `app.py:6183`) |
| 37 | Undoing a mission deletion replaced unsaved mission edits | ✅ | Missions now have draft tracking (#30) |
| 38 | Deleting an entry didn't check what refers to it | ✅ | `entity_references` (`backend/content.py:375`), tested at `tests/test_studio_api.py:3951` |
| 39 | Tuning metadata is maintained by hand | 🟡 | A test now checks that each cited source line still reads its parameter (`tests/test_studio_api.py:1918-1925`). Out-of-date wording still can't be detected |
| 40 | Some unit descriptions were long | ✅ | All 229 are ≤ 45 characters, tested (`tests/test_studio_api.py:3985`) |
| 41 | Saving a weight rewrote `hyperparameters.yaml` without its comments | ✅ | Only the changed values are replaced, comments kept (`backend/store.py:339`) |
| 42 | Unused parameters (never read, or needing a character no caller passes) | ✅ | Removed, as you chose; tested (`tests/test_studio_api.py:3975-3980`) |
| 43 | The repeat penalty never applied; `crossover_probability` overrode `evolution.crossover_rate`; gated parameters looked active while off | ✅ | Fixed as you chose; gated ones are marked inactive (tested) |
| 44 | A negative exponent would crash a story mid-run (ZeroDivisionError) | ✅ | Minimum 0, and the world is refused at build (`core_engine/builder.py:552-554`) |
| 45 | Old pyflakes warnings | ✅ | Fixed 21 Nov: the three files are clean. Other files still have a few unused imports, not part of this item |

## 4. Tuning views, engine names and cards (20 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 46 | "Modified" meant different from the last save, not from the shipped values | ✅ | Shipped values are recorded (`config/default_history.json`) and listed; ↺ puts them back |
| 47 | Leaving with unsaved weights had only the browser's own Leave/Stay | ✅ | Save / Discard / Stay inside the studio (`app.py:4740`) |
| 48 | How strong a pacing or repetition control feels depends on the storyteller's traits | 🟡 | Inherent. Each control now shows its trait range, and each engine card shows its traits |
| 49 | The gap between events and the most events per story weren't in Basic | ✅ | Now Basic controls (`backend/tuning_basic.yaml:95-97`) |
| 50 | A slow first page render tripped Gradio's 3-second start-up check | ✅ | The studio waits up to 90 s (`patient_startup_check`, `app.py:8069`) |
| 51 | Evaluations judged before 20 Nov aren't on any card | 📌 | Historical |
| 52 | Engine names live in the saves folder, not the records database | 📌 | A design choice (works with recording off); another deployment sharing the records folder doesn't see them |
| 53 | A generation champion has no model file, so its card is shorter | 📌 | Inherent |
| 54 | After changing shipped config, `config/default_history.json` must be regenerated | 📌 | A process step; a test fails until it is done |
| 55 | The start-up fix replaces an internal Gradio function | 📌 | A test covers it, but a Gradio upgrade could change it |

## 5. Export and Play (20 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 56 | Unavailable platforms can still be selected | 📌 | Gradio radios can't disable one option. They are labelled "(not available here)", and Export is disabled with the reason (`app.py:2366-2370`) |
| 57 | "Python only" sources stay selectable on a standalone platform | 📌 | Same limitation; Export is disabled and says how to fix it |
| 58 | When the previous quest isn't in the new engine's world, the note shows its key, not its title | ✅ | Fixed 21 Nov: named by the title remembered from the world that listed it, with its key (tested) |
| 59 | `docs/FRONTEND.md`'s view texts are out of date (Export, Play quests, and also Train, Evaluate, World, Entries) | ⏳ | The other session updated the jobs section only and is waiting for your decision on the view texts |

## 6. World authoring: library, Save changes, preview, conditions, sections (20 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 60 | The library listed at most 200 matches | ✅ | **Show more** loads the next page (`library-more`, `app.py:6226`) |
| 61 | Changes made outside the Entries editor (Grow, imports, another browser) appear only after ↻ Refresh or on returning | ✅ | Fixed 21 Nov: the library watches `GET /v1/world/revision` every 10 s and lists changes made elsewhere (checked in a browser: 3.5 s) |
| 62 | Renaming an entry that is referenced (e.g. a place named by map routes) is refused; references aren't updated | ✅ | Fixed 21 Nov: a rename renames every reference in the same save, other files edited in place with comments kept; an ambiguous name is still refused (tested) |
| 63 | The preview shows one possible telling; map changes (a path opening) aren't in its "changes" list | ✅ | Map changes fixed 21 Nov: paths opening or closing, places unlocked, being held (tested). That a sample scene is one possible telling is inherent, and said in the preview |
| 64 | An entry not present in the sample scene shows its actions as unavailable | 📌 | Said with a warning (`app.py:4889`) |
| 65 | The condition builder covers only entry and action conditions | ✅ | Fixed 21 Nov: also an action's *No longer offered once*, a mission's *Offered only if* and a dialogue line's *Said when* (tested, and checked in a browser) |
| 66 | Adding a non-variable predicate to shorthand rewrites it as an explicit ALL list | 📌 | The meaning is unchanged (tested); only the text differs |
| 67 | World's sections are reached only through the new bar | 📌 | By design |
| 68 | The discovered map in Play was a text list | ✅ | Drawn (`discovered_map_svg`, `app.py:1121`) |

## 7. Jobs: panel, reattach, cancellation (20 November)

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 69 | Jobs live in the API's memory: an API restart loses queued and running jobs | ⏳ | Still true; persistence was not added. The studio says "Job lost" and how to recover |
| 70 | Queue positions are exact only within one API process | 📌 | The API runs as one process by design |
| 71 | A real judge job was not run in the browser | ⏳ | A verification gap: Evaluate's panel, reattach and cancel are covered by tests with a stand-in judge only |
| 72 | Cancelling takes effect only at a safe point; an evaluation's play phase can't be interrupted | 📌 | Cooperative cancellation by design |
| 73 | The remembered job is per browser, not per user account | 📌 | By design (browser storage); another browser doesn't reattach |
| 74 | The job panel's polling could stop for good if a run was submitted while the page loaded | ✅ | Found and fixed in the last round; the timer never stops (`jobs_poll`) |
| 75 | The job panel stopped refreshing once a selected job had ended | ✅ | Found and fixed in the last round (`allow_custom_value`), tested |
| 76 | Headless Firefox runs the studio page at full CPU, so its timings are slow | 📌 | The verification environment, not the code (same with the old code) |

## 8. Recurring process items

| # | Limitation | Status | Evidence / what is left |
|---|---|---|---|
| 77 | Work left uncommitted at the end of each request (up to 20 Nov) | ✅ | All committed, up to `1e71a9f` |
| 78 | The other session's in-progress edits broke two tests (20 Nov) | ✅ | It updated them; the full suite passes |
| 79 | `docs/FRONTEND.md` endpoint rows were added by this session in the other session's file | ✅ | The coverage test passes; the other session has the details |
| 80 | Work left uncommitted | ⏳ | The reattach and cancellation work is committed (`5491381`). The 21 November fixes are not committed yet |
