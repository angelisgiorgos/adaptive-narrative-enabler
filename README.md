# Adaptive Narrative Enabler

Adaptive Narrative Enabler is a Python prototype for generating interactive narrative runs from modular YAML content. It builds a world from locations, NPCs, items, actions, and outcomes, then evolves genome-like control parameters that influence movement, discovery, threat, recovery, setup/payoff, and success behavior.

The main entry point is [`main.py`](main.py). Content and tuning live in [`config/`](config/). Generated stories, best worlds, and per-run archives are written to [`runs/`](runs/).

## Features

- Modular story-world authoring through YAML files.
- Evolutionary search over narrative-control genomes.
- Multi-core genome scoring with `ProcessPoolExecutor`.
- Configurable fitness scoring for dramatic arc, scene contrast, setup/payoff, volatility, path variety, character interaction, combat, discovery, and ending quality.
- Optional interactive terminal editor for browsing and editing story setup.
- Optional LLM support for world augmentation, editor suggestions, and final narrative evaluation.
- Automatic public-model fallback when the configured Hugging Face model is gated or inaccessible.
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
|   |-- llm_model_loader.py         # Shared LLM loader with fallback support
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

## Running

Start the application:

```bash
python main.py
```

Startup options:

```text
1. Run the story generator
2. Show the existing story setup
3. Edit story setup, then run
4. Edit story setup only
5. Load a world YAML path, then run
```

Option `5` accepts paths such as:

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
6. The best genome is used for a final detailed story simulation.
7. Optional LLM evaluation scores the final transcript.
8. Artifacts are saved in a timestamped folder under `runs/`.

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

`parallel_backend: processes` uses multiple CPU cores for score generation. Tag similarity and similarity-based spawn rules are cached to reduce repeated scoring work.

## LLM Fallback

LLM features are used by:

- `llm_evaluator`: final narrative evaluation.
- `augmentation`: world expansion.
- `editor_suggester`: editor field suggestions.

The primary model is configured here:

```yaml
llm_evaluator:
  enabled: true
  model_name: "meta-llama/Llama-3.2-1B-Instruct"
```

If that model is gated, restricted, or unavailable to the current Hugging Face user, the shared loader in [`utils/llm_model_loader.py`](utils/llm_model_loader.py) automatically tries the public fallback:

```yaml
llm_fallback:
  enabled: true
  model_name: "Qwen/Qwen2.5-0.5B-Instruct"
  local_files_only: false
  trust_remote_code: false
```

This fallback is shared by evaluation, augmentation, and editor suggestions. If you want offline-only behavior, set `llm_fallback.local_files_only: true` and make sure the selected models are already cached locally.

For faster non-LLM runs, disable these sections:

```yaml
llm_evaluator:
  enabled: false

augmentation:
  enabled: false

editor_suggester:
  enabled: false
```

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
