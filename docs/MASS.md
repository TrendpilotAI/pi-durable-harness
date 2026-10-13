# MASS on a local harness: what is ported, what is deferred

Source read: `github.com/TrendpilotAI/mass` (fork of SakanaAI/mass) at `fb403581bed2c974b3f99e7236224169da643079`
(README, `docs/paper_to_code.md`, `mass/pipeline.py`, `mass/ranking.py`, `mass/judging.py`,
`harness_improvement/iterate_multi_agent_prompt.py`, `runtime/api_proxy.py`, `runtime/run_episode.sh`,
`training/prepare_sft_data.py`, `tasks/`).

MASS alternates **RHI** (workflow search with the weights fixed; the current model is executor, optimizer and
in-loop evaluator) with **post-training** on trajectories of the retained workflows:
`L0 -- RHI -> post-training --> L1 -- ... --> L2`.

## Stage map

| MASS stage | MASS code | Here | Status |
|---|---|---|---|
| Episode runner (qwen-code headless, request logging) | `runtime/run_episode.sh`, `runtime/api_proxy.py` | `src/mass/episode.ts`, `src/proxy.ts` | **Ported.** Each episode is a durable pi-durable session; every model request goes through a byte-faithful logging proxy into `<logs>/api_requests.jsonl` with the same record fields (`ts_start`, `seq`, `request`, `response` with SSE reassembled into `content` / `reasoning_content` / `tool_calls` / `finish_reason` / `usage`, `duration_s`, ...). `run_status.json` keeps MASS's keys (`exit_code`, `trace_has_success_result`, `hiw.condition`, `hiw.api_requests`, ...). |
| Multi-agent workflow of model copies | qwen-code `agent` tool, `tasks/initial/*_ourTeam.txt` | `src/subagent.ts` (`agent` tool), `tasks/initial/` | **Ported.** Workers are child conversations owned by the tool call, same model, same tools, no nested workers. The tool is replay-safe, so a crash mid-worker resumes the same child. |
| RHI workflow search | `mass search`, `iterate_multi_agent_prompt.py` | `searchTask()` in `src/mass/pipeline.ts` | **Ported (compact).** Reference (bare-task) run, then v0..vN; each version is judged against the reference (`v{k}_vs_no_harness`, A = reference, B = version); champion via the ported `_derive_champion_baseline` plus champion arbitration (`champion_v{a}_vs_v{b}`); the optimizer (same model) writes `updated_queries/query<ID>_ourTeam-v{k+1}.txt` and `proposals/v{k+1}.json` with MASS's JSON output schema; the last version is evaluate-only and writes `retained_workflow.json` `{version, beats_reference, ...}`. |
| In-loop evaluator | `mass/judging.self_judge` | `src/mass/judge.ts` | **Ported, simplified.** Same-model pairwise judge returning `{winner: A/B/tie, rationale}`. The evidence bundle is a small file tree, file heads, and the output of an automated task check (`tasks/checks/query<ID>.sh`). Presentation order is swapped by seed and mapped back. |
| Teacher collection | `mass collect` | `collect()` | **Ported.** Retained workflow + `tasks/teacher_directive.txt` (adapted to the `agent` tool), N seeded candidates, `pool.json`. |
| Self-evaluation ranking | `mass rank`, `ranking.bradley_terry`, `tournament_pairs` | `rank()`, `src/mass/ranking.ts` | **Ported.** Ring tournament + Bradley-Terry (MM with prior 0.5). The PRNG is not Python's, so pair schedules differ from MASS for the same seed. |
| Train/val selection | `mass select` | `select()` | **Ported.** Ranks 1..`train_ranks` train, rank `validation_rank` validation. |
| Conversation rendering | `training/prepare_sft_data.py` | `src/mass/render.ts`, `render()` | **Partly ported.** Thread grouping by prefix, replay message form, and replacement of the orchestrator's teacher prompt by the bare task are ported; output is chat JSONL (`messages`, `tools`) in `runs/*/data/{train,val}.jsonl`. **Not ported:** tokenization, 49,152-token windows with 8,192 masked overlap, assistant-only `-100` label masks, task-uniform / orchestrator-1/3 sampling. |
| LoRA SFT, checkpoint selection, merge, FP8 export | `train_sft_lora.py`, `best_checkpoint`, `merge_lora.py`, `quantize_fp8_blockwise.py` | — | **Deferred** (see below). `pdh mass train` only prints that it is deferred. |
| External reporting (GPT/Claude judges) | `mass report`, `evaluation_claudecodex/` | — | **Not ported** (needs external APIs; out of scope for a local harness). |
| Public benchmarks | `benchmarks/` | — | **Not ported.** |

## Post-training: deferred

MASS trains a fresh LoRA (rank 64, alpha 128, dropout 0.05, 1,624 steps, 3 replicas x 2 GPUs) on the BF16 parent,
selects the checkpoint with minimum validation loss, merges it, and converts to block-FP8 for vLLM. On Big Trophy:

- `mlx_lm` is not installed, and the served model is a 4-bit MLX quantization. QLoRA on the 4-bit 35B-A3B MoE with
  `mlx_lm.lora` is possible in principle, but it needs the BF16 or 4-bit weights loaded a second time beside
  mlx-serve (which already keeps ~18 GB per resident model), hours of Apple-GPU time, and serving the result would
  mean changing mlx-serve's model, which this work must not touch.
- With the demo budget (one task, a few episodes) there is far too little data for a meaningful update.

So the cycle stops after `render`. The chat JSONL in `runs/<run>/data/` is the hand-off point: a later step can
run `mlx_lm.lora --train --data <dir-with-train.jsonl-and-valid.jsonl>` against a separate copy of the model and
serve the fused adapter from a separate mlx-serve instance. This is **unverified** here.

## Reference stack vs this stack

| | MASS reference | Here |
|---|---|---|
| Model | Qwen3.6-27B (dense), FP8 on vLLM, GPUs | `ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit` (MoE, ~3B active), 4-bit MLX on mlx-serve, Apple Silicon |
| Agent runtime | qwen-code 0.20.0 CLI, headless yolo; its own long system prompt, tool set, `agent` tool with `subagent_type`/`run_in_background`, history compression, `@`-path rewriting | `@earendil-works/pi-durable` 1.2.0 library harness; tools `read`/`write`/`edit`/`bash` + our `agent` tool; no system prompt unless instructions are set; pi-durable compaction; no background workers |
| Durability | Episodes are not resumable; a partial episode is refused | Every episode is a SQLite-backed durable session; rerunning the stage resumes an interrupted episode |
| Sampling | temp 0.6, top_p 0.95, top_k 20, thinking on, 49,152 max output tokens (judge/optimizer) and 32,768 per episode response, seeded | pi-ai defaults for episodes (no seed forwarded; the recorded `seed` is nominal); `thinking_level` from config (demo: `off`, sent as `reasoning_effort: none`); 16,384 max tokens; judge/optimizer temp 0.6, top_p 0.95, JSON schema output, seed passed through (mlx-serve support for `seed` unverified) |
| Budget | 11 RHI updates, 18 candidates, 16 ranks, 6 opponents | `configs/local.json`: 1 update, 3 candidates, ranks 2/3, 2 opponents |
| Tasks | 12 synthetic research tasks (finance, pharmacy, robotics), hours each | 2 small local coding tasks with automated checks (`tasks/`) |
| Logged request shape | qwen-code's messages and tool schemas | pi-ai `openai-completions` messages and pi-durable tool schemas; rendering uses the logged requests, so training data matches what this runtime sends |
| Teacher directive | `agent` tool, `subagent_type "general-purpose"`, `run_in_background: false` | rewritten for this `agent` tool (`agent_id`, `role_instruction`, `task`) |
| Champion rule | baseline-anchored with arbitration; pipeline judges vs baseline from v1 on | same rule; v0 is also judged vs the reference so it can hold a proven win |
