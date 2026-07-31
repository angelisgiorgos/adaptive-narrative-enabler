# Adaptive Narrative Enabler

Adaptive Narrative Enabler is a Python prototype for generating interactive narrative runs from modular YAML content. It builds a world from locations, NPCs, items, actions, and outcomes, then evolves genome-like control parameters that influence movement, discovery, threat, recovery, setup/payoff, and success behavior.

The main entry point is [`main.py`](main.py). Content and tuning live in [`config/`](config/). Generated stories, best worlds, and per-run archives are written to [`runs/`](runs/).

## Features

- Modular story-world authoring through YAML files.
- Evolutionary search over narrative-control genomes.
- Multi-core genome scoring with `ProcessPoolExecutor`.
- Configurable fitness scoring for dramatic arc, scene contrast, setup/payoff, volatility, path variety, character interaction, combat, discovery, and ending quality.
- Optional interactive terminal editor for browsing and editing story setup.
- Shared Gemma 4 E2B runtime through vLLM for world augmentation, editor suggestions, and final narrative evaluation.
- Tag-driven world evolution with extensible genome tag preferences rather than fixed location-transition sequences.
- Parameterized scenario templates that support variants while rejecting exact duplicates.
- Explicit objective losses for transition quality, tag coverage, named-edge dependence, duplicate scenarios, and world extension.
- Separate generation and play phases, with reusable saved genomes.
- Bidirectional location discovery, explicit forced movement, backtracking, locked exits, and mandatory encounters.
- Explicit, player-visible skill checks plus active NPC goals and setup/payoff state.
- Per-run timestamped archives containing the best world, best generated story, and final transcript.

## Repository Layout

```text
adaptive-narrative-enabler/
|-- main.py                         # Entry point, world building, evolution, simulation, archives
|-- config/
|   |-- hyperparameters.yaml        # Evolution, fitness, LLM, editor, augmentation settings
|   |-- locations.yaml              # Location definitions and location actions
|   |-- items.yaml                  # Object/item definitions and item actions
|   `-- npcs.yaml                   # Character definitions and NPC actions
|-- core_engine/
|   |-- actions.py                  # Action/outcome resolution
|   |-- augmenter.py                # Optional LLM world augmentation
|   |-- game_state.py               # Runtime story state and simulation loop
|   |-- genome.py                   # Evolvable narrative parameters
|   `-- world.py                    # World graph, spawn rules, entities
|-- evaluator/
|   `-- llm_eval.py                 # Structural and optional LLM evaluation
|-- utils/
|   |-- config_loader.py            # Deep-merges config YAML files
|   |-- config_editor.py            # Terminal config/story editor
|   |-- llm_model_loader.py         # Shared embedded/server vLLM runtime
|   |-- llm_suggester.py            # Optional editor suggestions
|   |-- tag_similarity.py           # Cached semantic/fallback tag similarity
|   |-- visualize.py                # Optional graph visualization
|   `-- generate_presentation.py    # Optional project-summary presentation
|-- augmentations/                  # Generated augmentation snapshots
|-- runs/                           # Generated story and world archives
`-- environment.yml                 # Conda environment definition
```

## Setup

Create the Conda environment:

```bash
conda env create -f environment.yml
conda activate adaptive-narrative-enabler
```

Or with Mamba:

```bash
mamba env create -f environment.yml
mamba activate adaptive-narrative-enabler
```

The environment includes the core runtime packages plus optional local LLM and visualization dependencies.
It requires vLLM 0.25 or newer for Gemma 4 support.

## Running

Start the application:

```bash
python main.py
```

The phases can also be run directly:

```bash
# Step 1: evolve and save a reusable genome/world, then exit
python main.py generate

# Step 2: play the latest saved genome/world without evolving again
python main.py play

# Intentionally do both in sequence
python main.py run

# Play a particular saved run
python main.py play --world runs/20260731_120000/best_world.yaml
```

Startup options:

```text
1. Generate and save a new genome/world (do not play)
2. Play the latest saved genome/world
3. Generate a new genome/world, then play it
4. Show the existing story setup
5. Edit story setup only
6. Load a saved world YAML path and play it
```

Option `6` accepts paths such as:

```text
runs/best_world.yaml
runs/20260428_124418/best_world.yaml
/absolute/path/to/world.yaml
```

During generation, press `q` to stop evolution early and continue to story execution with the best genome found so far.

## Execution Flow

1. `utils/config_loader.py` deep-merges all YAML files under `config/`.
2. `main.py` optionally opens the editor or loads a user-provided world YAML path.
3. Optional augmentation can expand the world before evolution.
4. `build_world(genome)` turns YAML into runtime `Location`, `Action`, `Outcome`, `Character`, and `Object` instances.
5. `evolve()` scores genomes over simulated episodes using multiple worker processes.
6. The best genome and discovered graph are saved and generation exits.
7. A later `play` command restores the world and genome without evolution.
8. Optional LLM evaluation scores the final play transcript.
9. Artifacts are saved in timestamped folders under `runs/`.

## Evolution And Speed

The `evolution` section in [`config/hyperparameters.yaml`](config/hyperparameters.yaml) controls search size and parallelism:

```yaml
evolution:
  pop_size: 400
  generations: 200
  episodes_per_genome: 6
  max_steps: 60
  evaluation_threads: 8
  parallel_backend: processes
```

`parallel_backend: processes` uses multiple CPU cores for score generation.
Semantic tag comparisons are cached to reduce repeated scoring work.

## Gemma 4 With vLLM

The default model is `google/gemma-4-E2B-it`. It is the smallest Gemma 4
instruction-tuned variant and one shared vLLM runtime is reused by:

- `llm_evaluator`: final narrative evaluation.
- `augmentation`: world expansion.
- `editor_suggester`: editor field suggestions.

Embedded mode requires no separate server command:

```yaml
vllm:
  mode: embedded
  model_name: "google/gemma-4-E2B-it"
  gpu_memory_utilization: 0.55
  max_model_len: 4096
  max_num_seqs: 1
  enforce_eager: true
  limit_mm_per_prompt:
    image: 0
    audio: 0
```

The single shared runtime prevents the evaluator, augmenter, and editor from
loading separate model copies. `gpu_memory_utilization` controls the memory
reserved for weights, activations, and KV cache. Gemma access must already be
accepted for the active Hugging Face account.

The reduced context length limits KV-cache memory, and the multimodal limits
skip image/audio profiling because this application sends text only. Set
`vllm.quantization` only to a method supported by the installed vLLM build and
the selected checkpoint.

For stronger isolation, run vLLM as one external service:

```bash
vllm serve google/gemma-4-E2B-it \
  --gpu-memory-utilization 0.55 \
  --max-model-len 4096 \
  --max-num-seqs 1 \
  --enforce-eager \
  --limit-mm-per-prompt image=0,audio=0
```

Then switch the application to server mode:

```yaml
vllm:
  mode: server
  base_url: "http://127.0.0.1:8000/v1"
```

For non-LLM runs, disable these sections:

```yaml
llm_evaluator:
  enabled: false

augmentation:
  enabled: false

editor_suggester:
  enabled: false
```

## Tag-Driven World Evolution

Ordinary discovery uses a semantic tag intent:

```yaml
- desc: A trail leads toward a secluded natural district.
  tags: [discovery, nature]
  spawn: true
  target_tags: [nature, secluded]
```

The runtime scores all eligible locations using intent alignment, contextual
coherence, novelty, per-tag genome preferences, prior visits, and extension
pressure. `spawn_rules` are optional weak priors. Named reveal fields from older
content are converted into tag hints by default; use `force_named_target: true`
only when identity itself is narratively mandatory.

Locations may reuse an archetype through parameters:

```yaml
North Market:
  template_id: district_market
  parameters:
    district: north
    prosperity: high
  tags: [urban, trade, affluent]
```

Another `district_market` with different parameters is a valid extension. An
exact repeat of the same template and parameters is rejected as a duplicate
scenario.

Evolution maximizes the existing narrative rewards minus five configurable
losses under `objective_losses`:

- `tag_transition`: keeps adjacent tag contexts within a useful
  coherence/novelty band.
- `tag_coverage`: prevents collapse onto a small tag subset.
- `named_transition_dependence`: discourages authored reveal edges.
- `scenario_duplication`: penalizes repeated template/parameter/action beats.
- `world_extension`: rewards discovery coverage and useful graph branching.

## Outputs

Each generation run creates a timestamped folder:

```text
runs/YYYYMMDD_HHMMSS/
```

The folder contains:

```text
best_world.yaml
best_story.txt
story_YYYYMMDD_HHMMSS.txt
```

The latest best artifacts are also copied to:

```text
runs/best_world.yaml
runs/best_story.txt
```

Augmented worlds are written under [`augmentations/`](augmentations/). If `augmentation.save_to_source` is true, augmentation can also update files under [`config/`](config/), so review diffs after augmentation.

## Configuration Files

- [`config/locations.yaml`](config/locations.yaml): places, tags, descriptions, actions, outcomes, movement, reveals.
- [`config/items.yaml`](config/items.yaml): objects, tags, descriptions, collectibility, item actions.
- [`config/npcs.yaml`](config/npcs.yaml): characters, tags, descriptions, known locations/objects, NPC actions.
- [`config/hyperparameters.yaml`](config/hyperparameters.yaml): evolution, simulation, fitness, LLM, editor, and augmentation settings.

## Location Graph And Movement

Locations are unique graph nodes. A discovered route creates a bidirectional
edge between the current node and the new node. `spawn` or `reveal_location`
only exposes that edge; it does not move the player:

```yaml
- desc: The map reveals a route to the Palace.
  spawn: true
  reveal_location: Palace
```

Use `move_to` only when the outcome must immediately transfer the player:

```yaml
- desc: The guards drag you to the Dungeon.
  move_to: Dungeon
```

Every connected node is always offered as `Move to <location>` during
interactive play. Set `exit_locked: true` on a special location and use
`unlock_exit: true` on an outcome that makes its connected routes usable.

## Explicit Checks And Power

Tags such as `stealth` describe content; they do not make an action risky by
themselves. A roll occurs only when `check`, `success_prob`, or
`use_genome_bias` is explicitly configured:

```yaml
- name: Collect the wine
  check:
    skill: stealth
    base_success: 0.7
  collects_object: Vintage Wine
  outcomes:
  - desc: The traveler pockets a bottle of fine wine.
    tags: [stealth]
```

The player sees the check name and calculated success chance before selecting
the action. A guard-related failed check can still cause arrest.

`Power` is the current combat rating. It is derived from health and inventory
attack/defense tags and is used only to weight combat/threat outcomes, success
probabilities, and damage mitigation. The interactive status line shows its
health, attack, and defense components.

## NPC Goals And Mandatory Encounters

NPC goals affect scene visibility and action ranking. Outcomes whose tags align
with an NPC's goals update visible goal progress in the transcript.

To force an immediate dilemma, mark its actions as mandatory. Navigation is
hidden until one mandatory action resolves:

```yaml
- name: Confront the bandits
  mandatory: true
  encounter: true
  return_to_previous: true
  outcomes:
  - desc: You drive the bandits away.
    tags: [combat, success]
```

`return_to_previous: true` returns to the prior graph node after the encounter
unless the selected outcome explicitly uses `move_to`.

## Setup And Payoff Example

A setup has a stable ID. A payoff with the same ID remains unavailable until
the setup has occurred:

```yaml
# Earlier action
- desc: The map reveals markings for a palace service route.
  tags: [setup_clue]
  setup_id: palace_route

# Later action
- name: Use the discovered palace route
  outcomes:
  - desc: The markings guide you through the inner halls.
    requires_setup: palace_route
    payoff_id: palace_route
    tags: [stealth, success]
```

The transcript records `[SETUP]` and `[PAYOFF]` events, and the fitness
calculation uses their actual action indices and narrative distance.

## Useful Commands

Run the main generator:

```bash
python main.py
```

Run the augmenter directly:

```bash
python -m core_engine.augmenter
```

Generate the project presentation:

```bash
python -m utils.generate_presentation
```

The presentation conversion path expects LibreOffice's `soffice` command if PPTX output is needed.

## Development Notes

- Keep version control active when using augmentation, especially with `augmentation.save_to_source: true`.
- For reproducible runs, set `reproducibility.seed` in `config/hyperparameters.yaml`.
- For faster debugging, temporarily lower `pop_size`, `generations`, and `episodes_per_genome`, or press `q` during generation to continue with the current best result.
