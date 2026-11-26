# Playtest: example worlds

Automated measures (counts and checks of what the engine recorded) and the judge's view (a language model's subjective reading) are reported separately.

## Scenarios

| Scenario | World / engine | Player | Seed | Start | Quest | Turns | Stopped |
| --- | --- | --- | --- | --- | --- | --- | --- |
| monastery-relic-main_quest-s3 | frozen_monastery | main_quest | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-main_quest-s11 | frozen_monastery | main_quest | 11 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-explorer-s3 | frozen_monastery | explorer | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-explorer-s11 | frozen_monastery | explorer | 11 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-side_quest-s3 | frozen_monastery | side_quest | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-side_quest-s11 | frozen_monastery | side_quest | 11 | Mountain Gate | return_the_relic | 15 | ended |
| monastery-relic-combat-s3 | frozen_monastery | combat | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-combat-s11 | frozen_monastery | combat | 11 | Mountain Gate | return_the_relic | 32 | ended |
| monastery-relic-conflict_avoiding-s3 | frozen_monastery | conflict_avoiding | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-conflict_avoiding-s11 | frozen_monastery | conflict_avoiding | 11 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-random-s3 | frozen_monastery | random | 3 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-relic-random-s11 | frozen_monastery | random | 11 | Mountain Gate | return_the_relic | 40 | max_turns |
| monastery-glacier-side_quest-s5 | frozen_monastery | side_quest | 5 | Glacier Path | earn_trust | 19 | ended |
| monastery-glacier-combat-s5 | frozen_monastery | combat | 5 | Glacier Path | earn_trust | 40 | max_turns |
| station-reactor-main_quest-s3 | orbital_station | main_quest | 3 | Docking Bay | reboot_reactor | 13 | ended |
| station-reactor-main_quest-s11 | orbital_station | main_quest | 11 | Docking Bay | reboot_reactor | 5 | ended |
| station-reactor-explorer-s3 | orbital_station | explorer | 3 | Docking Bay | reboot_reactor | 13 | ended |
| station-reactor-explorer-s11 | orbital_station | explorer | 11 | Docking Bay | reboot_reactor | 34 | ended |
| station-reactor-combat-s3 | orbital_station | combat | 3 | Docking Bay | reboot_reactor | 12 | ended |
| station-reactor-combat-s11 | orbital_station | combat | 11 | Docking Bay | reboot_reactor | 35 | ended |
| station-reactor-conflict_avoiding-s3 | orbital_station | conflict_avoiding | 3 | Docking Bay | reboot_reactor | 40 | max_turns |
| station-reactor-conflict_avoiding-s11 | orbital_station | conflict_avoiding | 11 | Docking Bay | reboot_reactor | 40 | max_turns |
| station-reactor-random-s3 | orbital_station | random | 3 | Docking Bay | reboot_reactor | 40 | max_turns |
| station-reactor-random-s11 | orbital_station | random | 11 | Docking Bay | reboot_reactor | 40 | max_turns |

## Automated measures

| Scenario | Quest done | Stages | Side quests (acc/done/fail/decl) | Attacks (/10 turns) | Meaningful choices | Filler (longest) | Backtracks | Repeated actions | NPCs met/talked | Contradictions | Fitness |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| monastery-relic-main_quest-s3 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 52% | 48% (6) | 26 | 18 | 3/1 | 0 | -10.51 |
| monastery-relic-main_quest-s11 | no | 1/2 | 0/0/0/1 | 1 (0.25) | 75% | 25% (4) | 23 | 10 | 4/2 | 0 | 301.44 |
| monastery-relic-explorer-s3 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 80% | 20% (4) | 15 | 4 | 5/3 | 0 | 442.10 |
| monastery-relic-explorer-s11 | no | 1/2 | 1/1/0/0 | 1 (0.25) | 78% | 22% (4) | 18 | 7 | 4/3 | 0 | 391.77 |
| monastery-relic-side_quest-s3 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 68% | 28% (3) | 20 | 11 | 4/2 | 0 | 382.76 |
| monastery-relic-side_quest-s11 | yes | 2/2 | 0/0/0/0 | 0 (0.0) | 100% | 0% (0) | 5 | 0 | 3/2 | 0 | 709.07 |
| monastery-relic-combat-s3 | no | 0/2 | 0/0/0/1 | 1 (0.25) | 82% | 15% (3) | 16 | 6 | 5/3 | 0 | 491.04 |
| monastery-relic-combat-s11 | yes | 2/2 | 0/0/0/0 | 0 (0.0) | 91% | 9% (2) | 13 | 3 | 3/1 | 0 | 779.25 |
| monastery-relic-conflict_avoiding-s3 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 62% | 38% (5) | 19 | 14 | 3/2 | 0 | 170.13 |
| monastery-relic-conflict_avoiding-s11 | no | 1/2 | 0/0/0/0 | 0 (0.0) | 75% | 25% (3) | 19 | 7 | 4/2 | 0 | 313.89 |
| monastery-relic-random-s3 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 70% | 30% (3) | 18 | 8 | 3/1 | 0 | 242.87 |
| monastery-relic-random-s11 | no | 0/2 | 0/0/0/0 | 0 (0.0) | 70% | 30% (5) | 20 | 10 | 4/1 | 0 | 368.14 |
| monastery-glacier-side_quest-s5 | yes | 1/1 | 1/1/0/0 | 1 (0.53) | 79% | 16% (1) | 10 | 5 | 3/2 | 0 | 657.32 |
| monastery-glacier-combat-s5 | no | 0/1 | 0/0/0/0 | 0 (0.0) | 65% | 30% (4) | 22 | 12 | 4/2 | 0 | 186.95 |
| station-reactor-main_quest-s3 | yes | 1/1 | 0/0/0/0 | 0 (0.0) | 69% | 31% (4) | 5 | 4 | 1/1 | 0 | 397.81 |
| station-reactor-main_quest-s11 | yes | 1/1 | 0/0/0/0 | 0 (0.0) | 100% | 0% (0) | 1 | 0 | 2/1 | 0 | 312.69 |
| station-reactor-explorer-s3 | yes | 1/1 | 0/0/0/0 | 0 (0.0) | 100% | 0% (0) | 3 | 0 | 1/1 | 0 | 576.69 |
| station-reactor-explorer-s11 | yes | 1/1 | 0/0/0/0 | 1 (0.29) | 85% | 12% (2) | 16 | 3 | 5/3 | 0 | 697.94 |
| station-reactor-combat-s3 | yes | 1/1 | 0/0/0/0 | 0 (0.0) | 100% | 0% (0) | 2 | 0 | 1/1 | 0 | 581.62 |
| station-reactor-combat-s11 | yes | 1/1 | 0/0/0/0 | 1 (0.29) | 77% | 23% (4) | 13 | 8 | 4/2 | 0 | 650.20 |
| station-reactor-conflict_avoiding-s3 | no | 0/1 | 0/0/0/0 | 0 (0.0) | 62% | 38% (3) | 22 | 12 | 3/2 | 0 | 159.73 |
| station-reactor-conflict_avoiding-s11 | no | 0/1 | 0/0/0/0 | 1 (0.25) | 65% | 32% (4) | 23 | 8 | 5/3 | 0 | 259.31 |
| station-reactor-random-s3 | no | 0/1 | 0/0/0/0 | 0 (0.0) | 85% | 15% (3) | 18 | 4 | 3/2 | 0 | 361.81 |
| station-reactor-random-s11 | no | 0/1 | 0/0/0/0 | 1 (0.25) | 60% | 38% (4) | 22 | 7 | 5/2 | 0 | 222.32 |

## World checks (before the first turn; the same for every playthrough of a world)

- **quest_not_viable_from_start** (orbital_station, warning): The quest `reach_observatory` cannot be worked toward from the start `Docking Bay` (no known or discoverable route to where it advances, or its goal holds already): the story never chooses it there. Edit: mission `reach_observatory` (/v1/documents/missions); location `Docking Bay` (/v1/entities/Locations/Docking%20Bay).

## Issues found while playing

| Issue | Count | Severity | Scenarios |
| --- | --- | --- | --- |
| `action_loop` | 8 | warning | monastery-glacier-combat-s5, monastery-relic-conflict_avoiding-s3, monastery-relic-main_quest-s11, monastery-relic-main_quest-s3 … |

### Failure examples

- **action_loop** (monastery-relic-main_quest-s3, turn 5): "Return to Mountain Gate" at `Abbot's Study` was taken 5 times. Edit: location `Abbot's Study` (/v1/entities/Locations/Abbot%27s%20Study).
- **action_loop** (monastery-relic-main_quest-s3, turn 9): "Return to Ice Cell" at `Mountain Gate` was taken 4 times. Edit: location `Mountain Gate` (/v1/entities/Locations/Mountain%20Gate).
- **action_loop** (monastery-relic-main_quest-s3, turn 11): "Ring the gate bell" at `Mountain Gate` was taken 4 times. Edit: location `Mountain Gate` (/v1/entities/Locations/Mountain%20Gate).
- **action_loop** (monastery-relic-main_quest-s11, turn 7): "Return to Mountain Gate" at `Cloister` was taken 4 times. Edit: location `Cloister` (/v1/entities/Locations/Cloister).
- **action_loop** (station-reactor-conflict_avoiding-s3, turn 5): "Return to Docking Bay" at `Cryo Vault` was taken 5 times. Edit: location `Cryo Vault` (/v1/entities/Locations/Cryo%20Vault).
- **action_loop** (station-reactor-conflict_avoiding-s3, turn 6): "Return to Cryo Vault" at `Docking Bay` was taken 4 times. Edit: location `Docking Bay` (/v1/entities/Locations/Docking%20Bay).

## Weak segments (for authors)

- **filler**, monastery-relic-main_quest-s3, turns 15-20: 6 turns brought nothing new: no new place, person, check, item, quest progress or outcome. Most of it at `Cloister` (Return to Mountain Gate ×3, Ring the gate bell ×2, Travel by known ways to the Abbot's Study ×1).
  - Suggestion: Give `Cloister` more to discover (new outcomes for its actions, an event, a person with a lead toward the quest), or connect it to a place the quest needs.
  - Edit: location `Cloister` → ?section=world (World → Locations → Cloister), `/v1/entities/Locations/Cloister`
- **loop**, monastery-relic-main_quest-s3, turns 5-29: "Return to Mountain Gate" at `Abbot's Study` was taken 5 times (turns 5, 8, 13, 18, 29).
  - Suggestion: Limit it (`repeat`), give it outcomes that change with each try, or make what it leads to clearer, so a player has a reason to move on.
  - Edit: location `Abbot's Study` → ?section=world (World → Locations → Abbot's Study), `/v1/entities/Locations/Abbot%27s%20Study`
- **loop**, monastery-relic-main_quest-s3, turns 9-27: "Return to Ice Cell" at `Mountain Gate` was taken 4 times (turns 9, 22, 25, 27).
  - Suggestion: Limit it (`repeat`), give it outcomes that change with each try, or make what it leads to clearer, so a player has a reason to move on.
  - Edit: location `Mountain Gate` → ?section=world (World → Locations → Mountain Gate), `/v1/entities/Locations/Mountain%20Gate`
- **loop**, monastery-relic-main_quest-s3, turns 11-21: "Ring the gate bell" at `Mountain Gate` was taken 4 times (turns 11, 16, 19, 21).
  - Suggestion: Limit it (`repeat`), give it outcomes that change with each try, or make what it leads to clearer, so a player has a reason to move on.
  - Edit: location `Mountain Gate` → ?section=world (World → Locations → Mountain Gate), `/v1/entities/Locations/Mountain%20Gate`
- **filler**, monastery-relic-main_quest-s11, turns 19-22: 4 turns brought nothing new: no new place, person, check, item, quest progress or outcome. Most of it at `Cloister` (Return to Mountain Gate ×1, Travel by known ways to the Glacier Path ×1, Return to Crypt ×1).
  - Suggestion: Give `Cloister` more to discover (new outcomes for its actions, an event, a person with a lead toward the quest), or connect it to a place the quest needs.
  - Edit: location `Cloister` → ?section=world (World → Locations → Cloister), `/v1/entities/Locations/Cloister`
- **dead_end_failure**, monastery-relic-main_quest-s11, turns 10-10: Turn 10: "Cross the crevasse" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: location `Glacier Path` → ?section=world (World → Locations → Glacier Path), `/v1/entities/Locations/Glacier%20Path`
- **dead_end_failure**, monastery-relic-main_quest-s11, turns 27-27: Turn 27: "(Warden) Slip past the warden" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: npc `Warden` → ?section=world (World → NPCs → Warden), `/v1/entities/NPCs/Warden`
- **loop**, monastery-relic-main_quest-s11, turns 7-26: "Return to Mountain Gate" at `Cloister` was taken 4 times (turns 7, 16, 19, 26).
  - Suggestion: Limit it (`repeat`), give it outcomes that change with each try, or make what it leads to clearer, so a player has a reason to move on.
  - Edit: location `Cloister` → ?section=world (World → Locations → Cloister), `/v1/entities/Locations/Cloister`
- **filler**, monastery-relic-explorer-s3, turns 30-33: 4 turns brought nothing new: no new place, person, check, item, quest progress or outcome. Most of it at `Abbot's Study` (Return to Mountain Gate ×1, Return to Cloister ×1, Return to Abbot's Study ×1).
  - Suggestion: Give `Abbot's Study` more to discover (new outcomes for its actions, an event, a person with a lead toward the quest), or connect it to a place the quest needs.
  - Edit: location `Abbot's Study` → ?section=world (World → Locations → Abbot's Study), `/v1/entities/Locations/Abbot%27s%20Study`
- **dead_end_failure**, monastery-relic-explorer-s3, turns 25-25: Turn 25: "(Warden) Slip past the warden" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: npc `Warden` → ?section=world (World → NPCs → Warden), `/v1/entities/NPCs/Warden`
- **dead_end_failure**, monastery-relic-explorer-s3, turns 37-37: Turn 37: "Ring the great bell" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: location `Bell Tower` → ?section=world (World → Locations → Bell Tower), `/v1/entities/Locations/Bell%20Tower`
- **filler**, monastery-relic-explorer-s11, turns 24-27: 4 turns brought nothing new: no new place, person, check, item, quest progress or outcome. Most of it at `Bell Tower` (Return to Bell Tower ×2, Return to Library ×1, Return to Mountain Gate ×1).
  - Suggestion: Give `Bell Tower` more to discover (new outcomes for its actions, an event, a person with a lead toward the quest), or connect it to a place the quest needs.
  - Edit: location `Bell Tower` → ?section=world (World → Locations → Bell Tower), `/v1/entities/Locations/Bell%20Tower`
- **dead_end_failure**, monastery-relic-explorer-s11, turns 2-2: Turn 2: "(Warden) Slip past the warden" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: npc `Warden` → ?section=world (World → NPCs → Warden), `/v1/entities/NPCs/Warden`
- **dead_end_failure**, monastery-relic-explorer-s11, turns 40-40: Turn 40: "Copy a forbidden page" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: location `Library` → ?section=world (World → Locations → Library), `/v1/entities/Locations/Library`
- **dead_end_failure**, monastery-relic-side_quest-s11, turns 2-2: Turn 2: "(Warden) Slip past the warden" failed and failing only costs health or coins (it leads nowhere).
  - Suggestion: Give its failure a consequence: an outcome that moves, reveals or sets something up, or a complication event sharing a tag with it (docs/WORLD.md: failures).
  - Edit: npc `Warden` → ?section=world (World → NPCs → Warden), `/v1/entities/NPCs/Warden`

## The judge's view (subjective)

Not run: no judge was asked (`--judge` needs the judge's server: docs/LLM.md).

## Recommendations

- Some quests cannot be worked toward from the default start (see World checks): give them a route a player can discover from there, or a start of their own.
- Players loop on the same action: give those actions a `repeat` limit or outcomes that change with each try, and make the way toward the quest clearer at those places.
