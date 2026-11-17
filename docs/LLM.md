# The language model and the judge

Gemma 4 on vLLM: how it is run, and how it judges stories as a game critic.

## Language model: Gemma 4 on vLLM

One language model, **Gemma 4 E4B** (`google/gemma-4-E4B-it`), serves every LLM feature
through a single shared runtime ([`utils/llm_model_loader.py`](../utils/llm_model_loader.py)),
so the model is loaded once:

- `llm_evaluator`: the judge of the final story (`evaluator/llm_eval.py`).
- `augmentation`: world expansion (`core_engine/augmenter.py`).
- `editor_suggester`: field suggestions in the terminal editor.

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

Without vLLM (or a server), the LLM features report that the model is unavailable and
everything else runs as usual. `llm_evaluator` sets the judge's `max_new_tokens` and
`temperature`, `editor_suggester` the editor's.

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
  the fitness's agreement with the judge (Spearman) and the judge's test-retest
  difference.

The judge also calibrated the fitness. Over 50 judged stories, the terms that ran
against the judge's engagement (volatility -0.38, scene contrast -0.33, combat
successes -0.31) now count half as much, and those that ran with it (scenario
duplication +0.44, novelty fading, loops, filler) cost more; repeated failed
attempts, its most frequent complaint, now ease the next attempt and step back for
a turn or two (`director.retry_bonus`, `retry_cooldown`).

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
