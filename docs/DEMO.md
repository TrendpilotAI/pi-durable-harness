# Demo results on Big Trophy (2026-10-13, times EDT)

Model: `ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit` on the existing mlx-serve (`127.0.0.1:11234`, unchanged), thinking off.
mlx-serve was also serving another client during both demos. Raw evidence is in [`docs/demo/`](demo/).

## 1. Session killed with SIGKILL and resumed by id

`./scripts/demo-kill-resume.sh kill-demo`: a `--team` session asks for three sequential sub-agent steps
(fib.py, test_fib.py, README.md). The script waits for `fib.py` to appear, then `kill -9`s the node process that
owns the store (pid 84912).

- After the kill ([`status-after-kill.json`](demo/kill-resume/status-after-kill.json)): `list` shows
  `interrupted`; the store has 3 live tasks: the root `pi.generation` **waiting** on the `pi.tool` task 12 (the
  `agent` call for agent_a), which owns the child conversation's `pi.generation` (task 30, pending); unsettled
  submissions are the root input (8, `placed`) and the child's input (20, `agent:12`, `placed`).
- `pdh resume kill-demo` (06:22:58) reopened the store; the replay-safe `agent` tool found the same child and the
  same submission (`agent:12`) and finished it; the run went on to agent_b and agent_c and ended `done` at 06:23:34
  ([`resume.out`](demo/kill-resume/resume.out)). The transcript has exactly one `agent_a` report, no duplicate
  ([`transcript.jsonl`](demo/kill-resume/transcript.jsonl)); `meta.runs` has 2 entries; 0 unsettled submissions after.
- Workspace after resume: `fib.py`, `test_fib.py`, `README.md`; `python3 -m unittest -q` → `Ran 5 tests ... OK`
  ([`tests-after-resume.txt`](demo/kill-resume/tests-after-resume.txt)).

An earlier attempt (not kept) killed a session before its first model response; resume also completed it, but the
generation then failed once with mlx-serve's `GenerationFailed` (see below), which is why sessions now auto-continue.

The same property is covered offline by `test/session.test.ts` (faux model, SIGKILL mid-stream, resume, same
submission id).

## 2. Small MASS cycle (`pdh mass cycle --config configs/local.json`)

Budget: task 1 (interval merging), 1 RHI update (v0, v1), 3 teacher candidates, ranks 1-2 train / 3 val, 2 opponents.
Wall time 06:23:42 → 06:42:14 including two interruptions.

| Step | Result |
|---|---|
| Reference (bare task) | success, 7 logged requests, check passes |
| v0 (initial 3-agent team) | **process died mid-episode** (the detached shell exited); rerunning `mass cycle` resumed the same durable session (submission 8, `runs: 2`); one mlx-serve decode failure was absorbed by auto-continue; success, 12 requests, check passes |
| Judge v0 vs reference | `B` (v0 wins) |
| Optimizer → v1 | added `agent_reviewer` ([`proposal-v1.json`](demo/mass/proposal-v1.json), [`query1_ourTeam-v1.txt`](demo/mass/query1_ourTeam-v1.txt)) |
| v1 | success, 13 requests, check passes; judge v1 vs reference `B` |
| Champion arbitration v0 vs v1 | `A` → v0 stays champion; `retained_workflow.json` = `{"version": 0, "beats_reference": true}` |
| Collection (3 seeds, teacher prompt = v0 + directive) | s710100: success, but its check exits 2 (a deliverable missing). s710101: failed after 3 mlx-serve `GenerationFailed` errors (attempt 1 kept as `run_status.attempt1.json`), then `mass cycle --retry-failed` continued the same session to success (43 requests total). s710102: success |
| Rank (3 same-model pairwise judgments, Bradley-Terry) | 1. s710102 (0.755), 2. s710101 (-0.400), 3. s710100 (-1.598): the candidate with the failing check ranked last |
| Select / render | train = ranks 1-2, val = rank 3; `data/train.jsonl` 9 conversations (2 orchestrator, 7 worker), `data/val.jsonl` 3 (1 + 2); orchestrator teacher prompts replaced by the bare task |

All 6 episodes wrote `api_requests.jsonl` (118 logged requests in total) and MASS-schema `run_status.json`.
A trimmed sample record is [`api_requests.sample-record.json`](demo/mass/api_requests.sample-record.json).
Logs: [`cycle.part1.log`](demo/mass/cycle.part1.log) (first process), [`cycle.log`](demo/mass/cycle.log) (resumed runs).

### Caveats seen during the demo

- mlx-serve sometimes fails a decode under concurrent load (`[concatenate] All the input array dimensions must
  match ...` then `decode aborted: MLX failure mid-generation`, in `~/mlx-serve.log`). pi-durable settles the input
  `unanswered / model_error`. The harness answers with up to two durable "continue" inputs per session, and
  `--retry-failed` for MASS episodes. This is a server-side issue; it was not changed.
- The judge's `rationale` uses the labels it was shown; when `swapped` is true they are reversed relative to the
  record's `winner` (now spelled out in `presented_winner` / `rationale_labels`).
- One task and a handful of episodes say nothing about whether the workflow search improves quality. This demo
  shows that the loop runs and that its artifacts are written; it is not a result.
