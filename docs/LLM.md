# The language model and the judge

Gemma 4 on vLLM: how it is run, and how it judges stories as a game critic.

## Language model: Gemma 4 on vLLM

One language model, **Gemma 4 E4B** (`google/gemma-4-E4B-it`), serves every LLM feature
through a single shared runtime ([`utils/llm_model_loader.py`](../utils/llm_model_loader.py)),
so the model is loaded once:

- `llm_evaluator`: the judge of the final story (`evaluator/llm_eval.py`).
- `editor_suggester`: field suggestions in the terminal editor.

World augmentation has its own model, **Qwen3.5-4B** (below), so the judge's model is not used for it.

It runs on [vLLM](https://docs.vllm.ai), in one of two modes:

```yaml
vllm:
  mode: embedded               # embedded | server
  model_name: google/gemma-4-E4B-it
  gpu_memory_utilization: 0.55
  max_model_len: 8192
  limit_mm_per_prompt: {image: 0, audio: 0}   # text only
  base_url: http://127.0.0.1:8000/v1           # server mode
```

- **`embedded`** loads the model in this process (needs `pip install "vllm>=0.25.0"`
  and a GPU; `environment.yml` installs it). Evolution's worker processes are then
  *spawned* instead of forked, since a GPU context must not be forked; they receive
  the active configuration and tag vectors explicitly.
- **`server`** calls a separately started OpenAI-compatible server, e.g.
  `vllm serve google/gemma-4-E4B-it --max-model-len 8192`. Use it with Docker (the
  image does not install vLLM) or to share one model between several processes.

**Pointing at a server.** `ANE_JUDGE_URL` (a server's `/v1` address) selects server mode over
`vllm.*`, with `ANE_JUDGE_API_KEY` and `ANE_JUDGE_MODEL`. With Docker, the `judge` service in
`compose.yaml` is that server, off unless asked for (it takes a GPU):

```bash
docker compose --profile judge up -d judge   # Gemma 4 E4B on GPU ANE_JUDGE_GPU (1), from ANE_JUDGE_HF_CACHE
```

The API looks for it at `http://hamlet-ane-judge:8000/v1` by default. It serves the model from a
local Hugging Face cache (offline), so the model must be in that cache.

**Is it there?** `GET /v1/judge/status` says whether the judge can be used: `{available, mode,
model, server, reason, how}` (a server is asked which models it serves). The studio shows it in
*⚖ Evaluate*, next to *▶ Play → Judge this playthrough* and in *🌍 World → ✨ Grow the world*: a ready judge, or why not
(no server answering at the address, a server with another model, a refused key, vLLM not
installed) and how to start one.

Without vLLM (or a server), the LLM features report that the model is unavailable, and why, and
everything else runs as usual. `llm_evaluator` sets the judge's `max_new_tokens` and
`temperature`, `editor_suggester` the editor's.

## World augmentation: Qwen3.5-4B on vLLM

World augmentation (🌍 World → ✨ Grow the world, and `python main.py`) runs on its own model,
**Qwen3.5-4B** (`Qwen/Qwen3.5-4B`, the instruction-tuned 4B of the Qwen3.5 small series), in
**float16** and not quantized (the TITAN RTX, a Turing GPU, has no bfloat16). It is a runtime role of
its own in `utils/llm_model_loader.py` (`settings("augmentation")`, `get_llm_runtime(role="augmentation")`),
so it never loads or asks the judge's model, and the judge never loads it.

```yaml
augmentation:
  enabled: true                # the tick in Grow the world (PUT /v1/augmentation)
  llm:
    mode: server               # server | embedded
    base_url: http://127.0.0.1:8001/v1
    model_name: Qwen/Qwen3.5-4B          # the name the server serves it under
    model_path: /data/angelisg/llm_models/Qwen3.5-4B   # its weights (embedded: loaded from here)
    dtype: float16
    gpu_memory_utilization: 0.75
    max_model_len: 8192
    limit_mm_per_prompt: {image: 0, video: 0}   # Qwen3.5 is multimodal: text only here
    enable_thinking: false     # answers directly (JSON), without its reasoning
    json_output: true          # vLLM holds the reply to valid JSON (structured output)
    max_new_tokens: 3072
    temperature: 0.7
```

- **The weights** are kept in a folder of their own, not the Hugging Face cache:
  `hf download Qwen/Qwen3.5-4B --local-dir /data/angelisg/llm_models/Qwen3.5-4B` (about 9 GB).
- **With Docker**, the `augmenter` service in `compose.yaml` serves it, off unless asked for:
  `docker compose --profile augmenter up -d augmenter`. It mounts `ANE_AUGMENT_MODELS`
  (`/data/angelisg/llm_models`) read-only, serves `ANE_AUGMENT_MODEL_DIR` (`Qwen3.5-4B`) as
  `Qwen/Qwen3.5-4B` on GPU `ANE_AUGMENT_GPU` (0), with the judge's vLLM image (vLLM 0.20.2 serves
  Qwen3.5). The API finds it at `http://hamlet-ane-augmenter:8000/v1` (`ANE_AUGMENT_URL`).
- **Without Docker**: `vllm serve /data/angelisg/llm_models/Qwen3.5-4B --served-model-name Qwen/Qwen3.5-4B
  --dtype float16 --max-model-len 8192 --limit-mm-per-prompt '{"image":0,"video":0}' --port 8001`, or
  `mode: embedded` to load it in the API's process.
- **Environment**: `ANE_AUGMENT_URL` (a server's `/v1` address: server mode), `ANE_AUGMENT_MODEL`,
  `ANE_AUGMENT_API_KEY` and `ANE_AUGMENT_MODEL_PATH` take precedence over `augmentation.llm.*`.
- **Is it there?** `GET /v1/augmentation` says whether augmentation is on and whether its model can be
  used (`{enabled, model: {available, mode, model, server, reason, how}}`); the studio shows it beside
  the tick in Grow the world.
- **What it is asked**: the prompt spells out the engine's action shape (`name`; `outcomes` with
  `desc`, `tags`, optionally `target_tags` or `reveal_location`) and shows one of the world's own actions
  as the example. The common deviations of a small model (an `action`/`outcome` pair, snake_case names,
  `target_tags` on the action) are put into that shape (`WorldAugmenter.normalize_actions`); only fields
  the engine knows are kept, and an action left without a name or an outcome is dropped. The proposal is
  still checked whole when it is added. Measured here: a proposal of 2–3 places, items and people in
  35–45 s on a TITAN RTX, accepted as proposed.
- **Off** (the tick unticked, `augmentation.enabled: false`): the studio's *Propose* is disabled,
  `POST /v1/jobs/world-proposal` is refused (409), and `python main.py` proposes nothing.

## Both on one GPU: taking turns

The judge (Gemma 4) and the augmenter (Qwen3.5-4B) do not both fit awake on one 24 GB GPU, so in one
session they **take turns** (`llm_sharing`, on by default; `utils/llm_model_loader.Turns`):

- Before a model generates, the other is put to sleep once nothing of its is running (vLLM's sleep
  level 1: its weights go to CPU memory and its GPU memory is freed), and its own is woken.
- A model that has stopped running is put to sleep after `idle_seconds` (5), so it waits in CPU memory.
- A background watcher in the API (started and stopped with it; the studio, a client of the API,
  runs none) puts to sleep, every
  `watch_seconds` (30), a model that is awake while nothing of it runs: one that loaded awake when
  its container started, or that something else woke. So no model holds its GPU memory while no
  evaluation runs; the next request wakes it (a few seconds). `watch_seconds: 0` turns the watcher
  off; `ANE_LLM_WATCHER=off` turns it off for one process (the tests do).
- Requests of the same model at the same time (a judge benchmark) share one wake; a request of the
  other model waits until they have finished (`wait_seconds`, 900, at most).
- Measured on a TITAN RTX: waking takes 1–2 s and going to sleep about 1 s; the first switch after the
  servers start takes about a minute (each model's first sleep sets up its CPU memory). Through the API:
  a proposal, three judge requests at once, a proposal again: the second switch round took 8 s, and both
  models were asleep 5 s after the last request (GPU memory back to about 2 GB). Asleep, each keeps about 1 GB of the GPU; awake, Gemma takes about
  18 GB and Qwen about 13 GB. The sleeping model's weights take host memory (Gemma about 15 GB, Qwen
  about 9 GB).
- The servers need `--enable-sleep-mode` and `VLLM_SERVER_DEV_MODE=1` (for `/sleep`, `/wake_up` and
  `/is_sleeping`). Those endpoints do **not** check the API key: keep the servers off published ports
  (the compose services are on the internal network only). An embedded engine is built with sleep mode
  when sharing is on. A server without sleep mode is left awake.
- **compose.yaml**: both services default to GPU 0 (`ANE_JUDGE_GPU`, `ANE_AUGMENT_GPU`) with memory
  shares that fit beside the other asleep (`ANE_JUDGE_GPU_MEMORY` 0.72, `ANE_AUGMENT_GPU_MEMORY` 0.6).
  Each starts through `packaging/vllm_turn_start.py`, which puts the other to sleep before loading its
  model. Started together, the augmenter first waits until the judge is serving
  (`ANE_LLM_WAIT_FOR_PEERS`, 900 s at most; a judge that was not started is not waited for), so the judge
  loads first and neither runs out of memory.
- `GET /v1/judge/status` and `GET /v1/augmentation` say whether a model is asleep (`sleeping`); the
  studio shows it.
- **Several sessions**: give each model its own GPU (`ANE_JUDGE_GPU`, `ANE_AUGMENT_GPU`, larger memory
  shares) and set `ANE_LLM_SHARING=off` (or `llm_sharing.enabled: false`): nothing is put to sleep then.
  Turns are kept within one API process; another process using the same servers (a terminal run) is
  not part of them.

### Gemma 4 as the judge of a story, from the view of a video game

`evaluator/game_judge.py` uses the language model as a judge (LLM-as-a-judge):
it reads a digest of a playthrough (each turn: the place, the choice, what
happened, checks, events, moves, changes of health and coins, the story beats
the player saw - a quest step, a twist, a callback, an ally, a new path, an
arrest - and the choices passed up) and scores it as
a game critic would, from 1 to 10, on **agency**, **pacing**, **surprise**,
**payoff**, **exploration**, **stakes**, **coherence** and **replayability**,
with the overall **engagement**, the best and the weakest moment and a verdict.
`llm_judge.samples` judgements are averaged; their spread says how sure the
judge is.

**Optional content.** When the digest shows optional content (side quests: `[SIDE QUEST]`,
`[SIDE STORY]`; dynamic encounters: `[AMBUSH]`, `[PATROL]`, `[ATTACK]`, `[ASSAULT]`, `[RAID]`,
`[DANGER]`, `[DEFEND]`), the prompt also asks the judge to count each optional thread as
**meaningful** (it connects to the main quest, changes what happens later, reveals something or
pays off near the end) or **filler** (it could be cut without changing anything), and to score
what it added from 1 to 10: `optional_content` `{score, meaningful, filler, filler_share, note,
samples}` (the mean of the samples that gave one; `null` without optional content). A playthrough
without optional content gets exactly the prompt it got before, so its judgements stay comparable.
The engine labels the same threads itself (`core_engine/side_story.classify`, the fitness's
`side_payoff` / `side_filler`); a judge benchmark reports the judge's mean `optional_content`, how
many stories had some, and the Spearman agreement of the judge's filler share with the engine's
(`filler_engine_vs_judge`, from 3 stories on). That agreement is what to look at before trusting
the engine's labels: they are rules, not a reader.

```python
from evaluator.game_judge import GameJudge
result = GameJudge().judge_state(state)   # or judge_turns(recorded turns, meta)
result["engagement"], result["scores"], result["verdict"]
```

- API: `POST /v1/records/stories/{id}/judge` (a recorded story) and
  `POST /v1/sessions/{id}/judge` (the game played so far); `503` without a model.
- Terminal: `python -m evaluator.game_judge STORY_ID`; with `llm_judge.enabled`
  the terminal story is judged after it is told.
- A shared GPU server: `vllm serve google/gemma-4-E4B-it --dtype float16
  --max-model-len 8192` (float16 on GPUs without bfloat16, such as the TITAN RTX),
  then `vllm.mode: server` and `vllm.base_url`.
- Benchmark: `python -m evaluator.judge_benchmark --stories 15 --server
  http://127.0.0.1:8000/v1` plays stories with the current engine (or `--engine ID`,
  a recorded one), judges each twice and prints the mean engagement, the criteria,
  the fitness's agreement with the judge (Spearman), the judge's test-retest
  difference, the fitness version the stories were scored with (`fitness_version`) and,
  where stories had optional content, `optional_content` and `filler_engine_vs_judge`.

The judge also calibrated the fitness. Over 50 judged stories, the terms that ran
against the judge's engagement (volatility -0.38, scene contrast -0.33, combat
successes -0.31) now count half as much, and those that ran with it (scenario
duplication +0.44, novelty fading, loops, filler) cost more; repeated failed
attempts, its most frequent complaint, now ease the next attempt and step back for
a turn or two (`director.retry_bonus`, `retry_cooldown`).

**Comparing with baselines.** `python -m evaluator.compare_baselines --server URL --worlds city
examples/frozen_monastery.yaml examples/orbital_station.yaml --stories 16 --train 12 8 3` trains,
per world and with the same settings and seed, a storyteller the old way (it plays its own
stories, `evolution.player: storyteller`) and the new way (simulated players), then plays the
same story seeds with each and with random storytellers. It reports fitness, the judge's
engagement and criteria (each story read twice), fitness's agreement with the judge, quests won,
and head-to-head win rates (new against old, new against random, both orders), as JSON and
markdown tables. No learning memory and no records are used, so the baselines are clean.

**Measuring with the judge, and learning from it.** Small differences in the judge's 1-10
scores (about 0.1) sit within run-to-run noise, so compare settings head to head.
`python -m evaluator.judge_benchmark --pairwise --variant director.surprise=0 --stories 20`
plays the same seeds with and without the change. Gemma 4 reads each pair twice, in both
orders, and a story wins only when both readings agree, which cancels the judge's preference
for whichever story comes first. The result is B's win rate with a 95% Wilson interval and a
win rate per criterion. `python -m evaluator.calibrate_fitness --stories stories.jsonl
--judged judged.jsonl --write weights.yaml` fits which fitness terms run with the judge,
suggests a multiplier per term (`fitness.term_weights`, 0.25-3) and reports how well the fit
predicts stories it was not fitted on. Finally, `evolution.judge_weight` (0 = off) puts the
judge in the loop: each generation, the judge reads one story from each of the
`judge_top_k` best storytellers and adds `judge_weight x (engagement - 5)` to their scores
before breeding. Without a model the run continues on the fitness alone.

For faster non-LLM runs, disable these sections:

```yaml
llm_evaluator:
  enabled: false

augmentation:
  enabled: false

editor_suggester:
  enabled: false
```
