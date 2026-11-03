# Spike: pi inside the sandbox via a unix-socket LLM proxy (hyp-90f.2)

Date: 2026-09-04. Branch `spike/sandbox-harness-pi` (on `origin/main`
b9a7ce94). Timebox: 2 days, used 1. Parent epic hyp-90f; prerequisite
hyp-90f.1 (one kernel uid per session) merged in a3225989.

## Verdict

**GO.** Verified end to end in the production image against real OpenRouter:
pi 0.85.0 runs inside the sandbox child as the session uid, under seccomp
(no IP sockets), with `_clean_env` unchanged and every model call leaving
through the service's unix-socket proxy. No NO-GO trigger fired:

| Trigger | Result |
|---|---|
| (1) pi's bundle captures a fetch that bypasses `globalThis.fetch` | **Did not fire.** pi *does* reassign `globalThis.fetch` (3× at startup, via `undici.install()` on its bundled undici) but the shim makes `fetch` an accessor whose setter swallows the reassignment. Proxy saw every request; zero `EAFNOSUPPORT`. |
| (2) Node cannot start under Limits even with RLIMIT_DATA | **Did not fire, but the ticket's numbers were wrong.** RLIMIT_AS 4 GB kills Node's `fetch` (`WebAssembly.instantiate(): Out of memory`, llhttp in undici); 8 GB too; 12 GB passes for one wasm instance. Decision: no RLIMIT_AS for the harness, RLIMIT_DATA 1 GB is the real cap (a 1.5 GB `Buffer.alloc` fails under it). |
| (3) pi needs a passwd entry / network at startup / global paths | **Did not fire.** No `uv_os_get_passwd` (pi never calls `os.userInfo()`; `os.homedir()` honours `HOME`). `PI_OFFLINE=1` skips the version check, model-catalog refresh and package update. State goes to `PI_CODING_AGENT_DIR=<session>/.pi` (pi creates `auth.json`, `models-store.json` there). |
| (4) pi's tools need network for the trivial task | **Did not fire** (stub run: read/write tools worked with no sockets beyond the UDS). |
| (5) wall > 300 s | **Did not fire.** Real run: 20.3 s and 21.3 s wall on two attempts, against a 300 s ceiling. |

Both GO thresholds are met with room to spare. The budget ceiling is 120 s
wall and 50k tokens; the real run finished in 20.3 s using 9570 tokens, about
a sixth of the wall budget and a fifth of the token budget. Every proxy
request was on the allowlist, and the three untouchable functions are
byte-identical to `main`.

## What the proxy is (design as built)

`sandbox/app/llm_proxy.py`, started from the FastAPI lifespan:

- `asyncio.start_unix_server` on `SANDBOX_LLM_PROXY_SOCKET`
  (`/run/hypebase/llm.sock`; dir 0711, socket 0666; not on the volume).
- Identity: `SO_PEERCRED` → peer uid → `_active_jobs[uid]`, a `JobBudget`
  the executor registers before `Popen` and removes after `communicate()`.
  Unknown uid, uid < `uid_base` (root service) or no job → 403. The uid is
  the only thing pi's bash tool cannot forge.
- Allowlist: exactly `POST /api/v1/chat/completions`, no query string, any
  Host. Everything else → 403 and logged on the job (`llmUsage.denied`).
- Rewrite: inbound `Authorization` stripped, `Bearer $SANDBOX_OPENROUTER_API_KEY`
  injected, `stream_options.include_usage=true` + `usage.include=true`
  forced, `max_tokens`/`max_completion_tokens` clamped to 8192 (pi sends
  64000), body ≤ 1 MB (413), `Connection: close`.
- Budget per job: 40 requests / 200k tokens → 429 JSON; usage parsed from
  the final SSE chunk (or JSON body) incl. OpenRouter's `usage.cost`;
  persisted as `llm_usage` in the job JSON and exposed as `llmUsage`.
- Upstream: `httpx.AsyncClient.stream`, 120 s timeout, 502 on failure.
- h11 parses the request (already a uvicorn dependency); httpx moved to
  runtime deps.

Launch path (`sandbox/app/executor.py`): `run_process(session_dir, argv,
env, Limits, timeout)` is the single isolation path; `run_code` (python) and
`run_harness` (pi) are wrappers with their own argv/env/limits. `/v1/exec`
gained `entrypoint: "python" | "harness:pi"` and `model` (validated against
`SANDBOX_HARNESS_MODELS`). pi argv:

```
node /opt/harness/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
  -p --no-session --no-extensions --model openrouter/<model> -- <task>
```

env = `_clean_env` + `PI_CODING_AGENT_DIR PI_OFFLINE=1 PI_TELEMETRY=0
PI_SKIP_VERSION_CHECK=1 OPENROUTER_API_KEY=sandbox-proxy LLM_PROXY_SOCKET
NODE_OPTIONS="--import=/opt/harness/shim.mjs --max-old-space-size=768"
NO_COLOR=1`. Limits: CPU 120 s, RLIMIT_DATA 1 GB, no RLIMIT_AS, NPROC 64,
NOFILE 256, FSIZE 64 MB, timeout 300 s. Seccomp unchanged.

Shim (`sandbox/harness/shim.mjs`): its own pinned undici 8.10.1 under
`/opt/harness/node_modules`, `new Agent({connect:{socketPath}})`,
`https:`→`http:`, dispatcher passed per request, `globalThis.fetch` defined
as an accessor. Runtime baked into the image: `COPY --from=node:22-bookworm-slim`
(node 22.23.2) + `npm ci --ignore-scripts` of `sandbox/harness/package.json`
(pi 0.85.0 pinned; 0.84.4 in the ticket was superseded on 2026-09-04).
`strace` added to the image for the spike only.

## Untouchable functions: diff vs `origin/main` is empty

Extracted by AST from both versions of `sandbox/app/executor.py`:

| Function | Lines | `diff` lines |
|---|---|---|
| `_install_network_block` | 25 | 0 |
| `_clean_env` | 22 | 0 |
| `child_identity` | 19 | 0 |

The identity drop itself (`user=uid, group=gid, extra_groups=[], umask=0o077`)
moved verbatim into `run_process`; `test_child_runs_as_session_uid_no_groups_umask_077`
still asserts it from outside the child.

## Findings that changed the ticket's assumptions

1. **pi reassigns `globalThis.fetch` at startup.** `configureHttpDispatcher()`
   builds an `EnvHttpProxyAgent` on the *bundled* undici, calls
   `setGlobalDispatcher` and then `undici.install()`, which assigns
   `globalThis.fetch/Headers/Request/Response`. A plain `globalThis.fetch = x`
   shim is overwritten. The accessor-property shim survives (no pi patch).
2. **Anthropic slugs on OpenRouter use `/api/v1/messages`.** pi's registry
   marks 15 `anthropic/*` OpenRouter models as `anthropic-messages`; they
   would be denied by the allowlist (and pi sends `x-api-key`, not `Bearer`).
   347 slugs are `openai-completions`; default is `openai/gpt-5-mini`. The
   allowlist stays single-path on purpose; widening it is a product decision
   for hyp-90f.3.
3. **RLIMIT_AS is incompatible with Node's fetch.** See trigger (2). This is
   a V8 wasm guard-region property, not tunable from Node flags in 22.x
   without `--no-wasm-...` experiments that were out of scope.
4. **pi 0.85.0 (not 0.84.4)** was `latest` on npm the day of the spike.
   `undici` 8.10.1 for the shim (pi bundles 8.9.0 internally).
5. **Node lands in `python:3.11-slim`, which is now Debian trixie.** libstdc++6
   is present; node from `node:22-bookworm-slim` links fine (glibc 2.41 ≥ 2.36).

## Measurements

Fixture: three `tools/*.json` via `PUT /v1/files`, then
`POST /v1/exec {"entrypoint":"harness:pi","code":"Read tools/*.json in the current directory and write summary.md describing each."}`.
Driver: `measure.py` (scratchpad; reproduced below in "Reproduce").

### In the production image, stub upstream (no key)

The stub replies with one `write summary.md` tool call and then a text
answer, so pi exercises its tool loop through the real proxy/uid/seccomp
stack. Tokens/cost here are the stub's constants, not a model's.

| Run | Mode | Wall (created→terminal) | Requests | Prompt / completion tokens | Cost | Exit | `summary.md` | Denied paths | stderr greps |
|---|---|---|---|---|---|---|---|---|---|
| job-d3b005347aa4 | stub, tool call `write` | 2.1 s | 2 | 200 / 40 (stub constants) | 0.0002 (stub) | 0 | present, written by pi's `write` tool as uid 100000, mode 0600 | none | none |
| job-93c040bf73bb | same, under `strace -f` | 3.0 s | 2 | 200 / 40 | 0.0002 | 0 | present | none | none |
| job-41de3b1d35b1 | stub, tool call `bash` running the adversarial probe below | 2.0 s | 2 | 200 / 40 | 0.0002 | 0 | n/a | `GET /api/v1/models -> 403`, `POST /api/v1/messages -> 403` | none |

`summary_mentions` is empty in the stub runs because the stub writes a
canned file; only a real model reads the three JSONs. `/ready` in the image:
`per_session_uid:true netns:true seccomp_netblock:true llm_proxy:true`.

Adversarial probe (job-41de3b1d35b1): the stub's first reply was a `bash`
tool call whose command, run by pi as the session uid inside the child,
tried an IP socket, two off-allowlist paths over the UDS, three service
files, and an env scan. Result reported back through pi's tool loop:

```json
{"inet": "OSError:97", "models": 403, "messages": 403,
 "/workspace/_jobs": "PermissionError", "/workspace/_meta/uid_counter": "PermissionError",
 "/proc/1/environ": "PermissionError", "env_has_key": false, "uid": 0}
```

errno 97 = `EAFNOSUPPORT` (seccomp). `uid: 0` is the user-namespace view
under Docker's `unshare -rn` (see the `sandbox-test-in-image-userns` memory);
the kernel uid is 100000, as the file ownership shows. Railway has no userns
and reports the real uid.

### Real OpenRouter (`openai/gpt-5-mini`, the default allowlisted slug)

Key supplied by the owner's Railway environment at run time
(`railway run -s hypebase-pipeline -- bash real-run.sh`, no `-e` flag, from a
shell linked to the `production` environment — see the key-provenance note
below), never written to disk and never in the agent's context. Two attempts; the first used a driver
that only looked for `summary.md` at the session root, so it recorded the
file as missing when pi had written it under `tools/`. The driver now finds
it from the session listing. Nothing about the harness differed between the
two runs.

| Run | Wall | Requests | Prompt / completion tokens | Cost (USD) | Exit | `summary.md` | Denied paths | stderr greps |
|---|---|---|---|---|---|---|---|---|
| job-1c66e417dc13 | 21.3 s | 6 | 8907 / 661 | 0.002113 | 0 | written as `tools/summary.md` | none | none |
| job-f6fe222e4f17 | 20.3 s | 6 | 8879 / 691 | 0.001940 | 0 | `summary.md`, 1340 bytes, names all three inputs | none | none |

| GO criterion | Ceiling | Measured |
|---|---|---|
| Wall, `created_at` → terminal | 120 s | 20.3 s |
| Tokens per job | 50 000 | 9 570 |
| Proxy requests off the allowlist | 0 | 0 |
| Changes to seccomp / uid drop / `_clean_env` | 0 lines | 0 lines |

The model read the three fixtures and wrote a structured summary of each,
with the row contents quoted back correctly. pi's own stdout for the second
run: "I read the three JSON files under tools/ and created summary.md
describing each file and its contents." Session listing afterwards holds the
three inputs, `summary.md`, and pi's own `.pi/auth.json` and
`.pi/models-store.json`, all owned by the session uid.

Cost per job is about a fifth of a cent at this size, so the 200k-token
per-job budget corresponds to roughly four cents of exposure per runaway job.

### Syscall / path capture (`strace -f -e trace=%network,%file`)

One run (job-93c040bf73bb) with `/usr/local/bin/node` wrapped in
`strace -f -e trace=%network,%file -o $HOME/.trace` inside the container.
3679 lines. Summary:

| Category | Result |
|---|---|
| `socket()` | 2 calls, both `AF_UNIX, SOCK_STREAM\|SOCK_CLOEXEC\|SOCK_NONBLOCK` |
| `connect()` | 2 calls, both `sun_path="/run/hypebase/llm.sock"` → 0 |
| AF_INET / AF_INET6 attempts | **0** — pi never tries an IP socket with `PI_OFFLINE=1` (seccomp did not even have to fire) |
| `EACCES` / `EPERM` | **0** |
| `ENOENT` | 1545, all Node module-resolution probes (`.../undici/lib/**/package.json`, `.../dist/bundle/chunks/package.json`) plus 10× `<session>/.pi/settings.json` (pi reads before creating; harmless) |
| `EINVAL` | 9 (libuv `ioctl`/`fstat` probes on pipes; harmless) |
| Writes outside the session dir | **0** (`openat` with `O_WRONLY\|O_RDWR\|O_CREAT` only under `/workspace/org1/<conv>`) |
| `execve` | 1: `node.real <cli.js> -p --no-session --no-extensions --model openrouter/openai/gpt-5-mini -- <task>`; the `bash` tool run adds `/bin/sh`+`python3` in the probe job |
| `uv_os_get_passwd` / `getpwuid` | not observed |

Full trace kept out of the repo (session dir of the throw-away container).

### stderr greps (`EAFNOSUPPORT|EACCES|EPERM|ENOENT|uv_os_get_passwd`)

`EAFNOSUPPORT|EACCES|EPERM|ENOENT|uv_os_get_passwd` over the job's stderr:
empty in all three runs (stderr was empty). The first attempt, before the
RLIMIT_AS fix, had exit 1 and `RangeError: WebAssembly.instantiate(): Out of
memory` in stderr with zero proxy requests — that is the signature to
watch for if limits regress.

## Tests

- `sandbox/tests/test_llm_proxy.py` (portable, 23 tests): peer gating,
  allowlist (6 off-list shapes), key injection, usage forcing, clamp, 413,
  400, 503, SSE relay + `Connection: close`, request/token 429, accounting
  from SSE and JSON, 502.
- `sandbox/tests/test_harness.py`: limits shape, Node starts under harness
  limits, RLIMIT_DATA enforced (Linux), env/argv contract, **pi end to end
  through the proxy** (needs node + pi), `.pi` owned by the session uid
  (root_linux), API validation (entrypoint literal, model allowlist, 64 KB
  task cap, `llmUsage` in the view).

Results (2026-09-04):

| Where | Command | Result |
|---|---|---|
| Production image, root, Docker 27.3.1 x86_64 | `sandbox/scripts/test-in-image.sh` | **116 passed, 2 skipped** (the two dev-mode-only tests), 0 failed, 147 s |
| macOS dev box, pi from a scratch `npm install` | `cd sandbox && SANDBOX_HARNESS_PI_CLI=… SANDBOX_HARNESS_SHIM=… pytest -q tests` | 94 passed, 24 skipped (root_linux + Linux-only rlimits) |

`test_pi_completes_via_unix_socket_proxy` passes in both: pi 0.85.0 end to
end through the real proxy, all requests on the allowlist with the injected
key, usage accounted, no forbidden errno in stderr. One earlier full
in-image run had `test_escaped_grandchild_reaped_by_uid` (pre-existing,
hyp-90f.1) fail once while a second container and a review agent were
competing for CPU; it passed in isolation and in the final run. Not touched
by this spike; flagged as load-sensitive.

An independent critic pass on the staged diff found one real defect that
the stub had masked (it collapsed headers into a dict): the proxy appended
a second `Content-Type` instead of replacing the child's. Fixed (`drop` set)
and covered by a raw-header uniqueness assertion.

## Risks and what production needs (input for hyp-90f.3)

1. **Real-model run: done, GO criteria met** (20.3 s, 9570 tokens, exit 0).
   Kept here for the key-provenance note. Needs
   `SANDBOX_OPENROUTER_API_KEY` on the local `docker run` (or on the staging
   `hypebase-sandbox` service). The measurement driver and fixture are in
   this doc; expected budget for the trivial task with `openai/gpt-5-mini`:
   a handful of requests, well under 50k tokens.

   **Key provenance (owner decision, 2026-09-04):** the owner elected to
   reuse the pipeline's existing `PIPELINE_OPENROUTER_API_KEY` value rather
   than mint a separate one.

   An earlier revision of this section claimed the Railway project has a
   single environment named `production` that serves as the team's staging
   deploy. That was wrong, and the correction strengthens the case for a
   dedicated key rather than weakening it. `railway status --json` on the
   project `Hypebase AI` (`0013046d-6ebc-41b6-8921-4b95612b73cd`) lists three
   environments: `production`, `staging` and `staging-legacy`. This week's
   validation work ran against `staging`; `production` is a separate,
   currently stale environment. The measurement runs recorded above were
   launched with no `-e` flag from a shell linked to `production`, so the
   credential injected into them was **production's**
   `PIPELINE_OPENROUTER_API_KEY`, not staging's.

   Consequences, stated plainly. That key now has usage on it from
   model-authored code executing in a sandbox child, so revoking it after
   abuse would stop production chat completions, and OpenRouter's cost
   attribution cannot tell the two consumers apart. The exposure from the
   measurement itself is bounded: the value was injected into the process by
   `railway run` for the duration of one command, never written to disk,
   never passed in argv, never set on any service, and roughly $0.004 of
   credit was spent in total. Nothing persists. The recommendation in
   `docs/DEPLOY.md` stands and applies to every environment: give
   `hypebase-sandbox` its own key of the same OpenRouter account.
2. **One harness job per session at a time.** The proxy keys the budget on
   the uid, which is per session; a second `harness:pi` exec on a live uid
   is refused (`HarnessError` → job failed, `[sandbox] harness unavailable`).
   Production needs either a per-session queue in the executor or a
   per-job identity (a per-job uid would need the uid counter to move from
   per-session to per-job, which conflicts with the DAC model). Prefer the
   queue.
3. **Concurrent python jobs of the same session** are unaffected (they do
   not register), but a python job and a harness job on the same uid at the
   same time means the python child could also reach the proxy with the
   harness job's budget. The proxy cannot distinguish them. Same fix as (2):
   serialize per session while any harness job runs.
4. **Memory pressure.** 4 workers × Node at 1 GB RLIMIT_DATA is 4 GB on the
   Railway plan; run one harness job at a time (pool of 1 for the harness,
   or a semaphore) until measured.
5. **RLIMIT_AS is off for the harness** (wasm guard regions). RLIMIT_DATA
   caps private writable memory; file-backed mappings and shared memory are
   not counted. Acceptable, but document it in the isolation model.
6. **Image size**: `/opt/harness/node_modules` is ~450 MB (pi bundles
   every provider SDK plus `@silvia-odwyer/photon-node`). `npm ci
   --ignore-scripts` worked (no native builds needed). Trim with a
   pruned install or the `/workspace/_shared/harness/` roll-out in hyp-90f.3.
7. **Model allowlist is one slug.** Anthropic-on-OpenRouter needs
   `/api/v1/messages`, which the allowlist denies by design. Widening to two
   paths means teaching the proxy the Anthropic usage shape too.
8. **`strace` in the image is spike-only**; remove in the build-out.
9. **Output**: pi's `-p` mode prints only the final assistant text on stdout
   (64 KB cap applies). Tool-call traces are not surfaced; streaming and
   cancellation are out of scope per the ticket.
10. **pi state under `<session>/.pi`** (`auth.json`, `models-store.json`,
    later `settings.json`) is per session and model-writable; harmless, but
    a model could write `settings.json` to change pi's defaults for the
    *next* job of that session (e.g. enable extensions). The argv already
    passes `--no-extensions`; consider `--no-skills --no-prompt-templates
    --no-context-files` and a fresh `.pi` per job in production.
11. **pi version pin**: 0.85.0 was published the day of the spike; the fetch
    accessor trick depends on pi resolving `fetch` from `globalThis` at
    client construction, which held for 0.84.x and 0.85.0. Pin and re-run
    `test_pi_completes_via_unix_socket_proxy` on every bump (it fails loudly
    if the bypass appears).

## What was not measured

- Staging: nothing deployed, no Railway variable set or changed. The real
  run above executed locally in the production image, with the key injected
  from the owner's Railway environment for the duration of the command.
- Concurrency: every run was a single harness job. The per-session
  serialization gap below is reasoned, not measured.
- Sustained cost: two jobs of one trivial task. No load or long-task profile.

## Reproduce

Local (Docker, x86_64):

```bash
docker build -t hypebase-sandbox:test sandbox/
# stub upstream, no key: proves mechanics
SANDBOX_OPENROUTER_API_KEY=stub docker run -d --name hb -p 8101:8100 \
  -e SANDBOX_API_KEY_SHA256="$(printf 'spike-key' | shasum -a 256 | cut -d' ' -f1)" \
  -e SANDBOX_HARNESS_UPSTREAM_URL=http://127.0.0.1:9999/api/v1/chat/completions \
  -e SANDBOX_OPENROUTER_API_KEY hypebase-sandbox:test
# real OpenRouter: export SANDBOX_OPENROUTER_API_KEY in the shell first, drop
# the SANDBOX_HARNESS_UPSTREAM_URL line, keep `-e SANDBOX_OPENROUTER_API_KEY`
# (name only, so the value never appears in argv or shell history).
```

Then, with any HTTP client (the spike used a 60-line `measure.py`):

1. `PUT /v1/files/org1/conv1/tools/<name>.json` × 3 (any JSON).
2. `POST /v1/exec` with
   `{"org_id":"org1","conversation_id":"conv1","entrypoint":"harness:pi","code":"Read tools/*.json in the current directory and write summary.md describing each."}`
   (header `X-Sandbox-Key: spike-key`).
3. Poll `GET /v1/jobs/org1/<jobId>` until `isTerminal`; read `llmUsage`,
   `exitCode`, `stderr`; `GET /v1/files/org1/conv1/summary.md`.

Staging: set `SANDBOX_OPENROUTER_API_KEY` on `hypebase-sandbox` (user
action), deploy this branch's sandbox, check `GET /ready` shows
`llm_proxy:true`, run steps 1–3 against the sandbox's private URL with the
pipeline's sandbox key. Nothing in the pipeline calls `harness:pi` yet
(out of scope), so this is a direct-to-sandbox check only.

Tests: `sandbox/scripts/test-in-image.sh` (root, node, pi all present in the
image) and `cd sandbox && pytest -q tests` locally (pi tests skip unless
`SANDBOX_HARNESS_PI_CLI`/`SANDBOX_HARNESS_SHIM` point at a local install).
