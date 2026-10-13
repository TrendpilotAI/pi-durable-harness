# pi-durable-harness

Durable coding-agent sessions on a local [mlx-serve](http://127.0.0.1:11234/v1) model, built on
[`@earendil-works/pi-durable`](https://github.com/earendil-works/pi/tree/main/packages/durable) (pinned **1.2.0**,
verified here), plus a local port of [MASS](https://github.com/TrendpilotAI/mass) (Multi-Agent Self-Supervision):
multi-agent workflows of model copies, workflow search with fixed weights, an in-loop same-model evaluator, and
trajectory logging in the format MASS post-training reads.

> pi-durable is experimental; its API changes between releases. The versions in `package-lock.json` are exact.

## Requirements

- macOS / Apple Silicon, Node >= 22.19 (tested with Node 24.21 via Volta; `node:sqlite` and TypeScript type stripping are used, no build step)
- mlx-serve with an OpenAI-compatible API at `http://127.0.0.1:11234/v1` serving `ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit`
- Optional: `~/.mlx-serve/pi/models.json` (provider `mlx`); if present, base URL, model, and compat flags are read from it
- `python3` (task checks), `gitleaks` (secret scan)

```bash
cd ~/code/pi-durable-harness
npm ci
npm run typecheck
npm test
./bin/pdh config        # shows the resolved provider (api key redacted)
```

## Durable sessions

Each session is one pi-durable SQLite store at `$PDH_HOME/sessions/<id>/session.sqlite` (default
`~/.pi-durable-harness`) plus `meta.json`. Inputs are submitted with a stable `requestId`, so after a crash the
same submission is found again instead of being sent twice; `harness.resume()` continues the generation and tool
tasks the dead process left behind. Tools run as durable tasks; the `agent` (sub-agent) tool is replay-safe and
its child conversation is found again after a restart.

```bash
./bin/pdh start --cwd /tmp/work --id demo1 "Create hello.py that prints hi and run it" </dev/null
./bin/pdh start --team --cwd /tmp/work2 "Use the agent tool to ..." </dev/null   # enable the multi-agent `agent` tool
./bin/pdh list
./bin/pdh status demo1          # meta + store inspection (live tasks, unsettled submissions, entries, usage); never runs tasks
./bin/pdh transcript demo1      # compact transcript, newest first
./bin/pdh resume demo1          # continue an interrupted run
./bin/pdh resume demo1 --prompt "Now add a test"   # follow-up in the same durable conversation
```

Options: `--thinking off|low|medium|high` (default `off`), `--timeout-ms N` (aborts the conversation durably),
`--prompt-file F`, `--temperature T` and `--seed N` (on `start` and `resume`). `start` prints `session <id>` before
running, so a killed run can be resumed by id.

Sampling: `--temperature` / `--seed` are stored in the session's `meta.json` (`sampling`) and sent with every model
request of the session, sub-agent requests included; a later `resume` reuses them, and a flag given to `resume`
replaces just that field. Without them nothing is sent and mlx-serve's defaults apply. In MASS episodes the
per-episode seed is always sent, and `episode.temperature` in the config is sent when set (see
[docs/MASS.md](docs/MASS.md)).

Environment: `PDH_HOME`, `PDH_BASE_URL`, `PDH_MODEL`, `PDH_MAX_TOKENS`, `PDH_API_KEY`, `PDH_PI_MODELS_JSON`,
`PDH_MAX_CONCURRENT` (MASS episodes: max requests in flight from the logging proxy to mlx-serve; default `0` =
unlimited; set `1` if mlx-serve fails decodes when team workers overlap).
Scripted calls should pass `</dev/null`.

If a run ends `unanswered` with a model error (mlx-serve can fail a decode under concurrent load), the session
queues up to two durable "continue" inputs (`<requestId>:continueN`) before giving up.

One process owns a store at a time (pi-durable has no cross-process locking): `run`/`resume` create
`<session>/owner.pid` exclusively and refuse while it names a live process; a lock left by a dead process is
taken over. `status` and `transcript` do not open a store whose recorded owner is alive.

### Kill-and-resume demo

```bash
./scripts/demo-kill-resume.sh kill-demo        # start a 3-worker task, SIGKILL the owner mid-run, status, resume
```

Evidence lands in `runs/demo-logs/<id>/`.

## MASS cycle (local)

```bash
./bin/pdh mass search  --config configs/local.json   # RHI: reference, v0..vN, judge vs reference, optimizer
./bin/pdh mass collect --config configs/local.json   # teacher episodes with the retained workflow
./bin/pdh mass rank    --config configs/local.json   # same-model pairwise tournament + Bradley-Terry
./bin/pdh mass select  --config configs/local.json   # train / validation manifests
./bin/pdh mass render  --config configs/local.json   # chat JSONL for SFT (teacher prompt -> bare task)
./bin/pdh mass cycle   --config configs/local.json   # all of the above
./bin/pdh mass train   --config configs/local.json   # deferred: prints a note, runs nothing
```

Every stage is resumable (completed episodes and recorded judgments are skipped; an interrupted episode resumes
its durable session). Layout under `runs/L0/` follows MASS: `reference/`, `workspaces/v{k}/`, `logs/.../api_requests.jsonl`,
`logs/.../run_status.json`, `search/query<ID>_ourTeam/{history.jsonl,champion_history.jsonl,retained_workflow.json,updated_queries/,proposals/}`,
`teacher_prompts/`, `pool.json`, `pool/{workspaces,runlogs}/`, `selection/`, `data/`. A changed config needs a new `run_dir`.

Tasks use MASS's file layout: `tasks/bare/query<ID>.txt`, `tasks/initial/query<ID>_ourTeam.txt`,
`tasks/teacher_directive.txt`, plus `tasks/checks/query<ID>.sh` (automated evidence for the evaluator).

See [docs/MASS.md](docs/MASS.md) for what is implemented vs deferred and how this stack differs from MASS's
qwen-code + Qwen3.6-27B reference, and [docs/DEMO.md](docs/DEMO.md) for the recorded demo results.

## Layout

| Path | Contents |
|---|---|
| `src/config.ts`, `src/provider.ts` | mlx provider for pi-ai (`openai-completions`) |
| `src/session.ts` | `SessionManager`: start / resume / list / status / transcript over pi-durable SQLite stores |
| `src/subagent.ts` | `agent` tool: durable, replay-safe sub-agents (model copies) |
| `src/proxy.ts` | port of MASS `runtime/api_proxy.py` (trajectory logging) |
| `src/mass/` | episode runner, judge, optimizer, ranking, rendering, pipeline |
| `test/` | vitest: SIGKILL-and-resume with a faux model, proxy, ranking, rendering, templates |
