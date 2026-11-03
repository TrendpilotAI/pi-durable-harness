# pi-durable-harness

Durable-harness work for the pi runtime: the pi-in-sandbox implementation
(LLM proxy over a Unix socket) and the Cerebras model-provider setup,
extracted from `Hypebase-ai/hypebase-platform` on 2026-11-03 so both can
develop without waiting on that repo's ticket and review gates.

## Contents

| Path | What it is | Source |
|---|---|---|
| `sandbox/` | The sandbox service as a whole: per-session workspace, rlimited executor, and the embedded pi runtime behind a Unix-socket LLM proxy. | `hypebase-platform` `sandbox/` @ `a9f8a934` (origin/main, 2026-11-03). Harness spike: merged PR #178 (`spike/sandbox-harness-pi`). |
| `docs/harness-in-sandbox-spike.md` | The spike write-up: how pi runs inside the sandbox, the proxy protocol, what was proven and what was stubbed. | Same commit, `docs/reviews/`. |
| `cerebras/pipeline-llm-provider.patch` | The Cerebras model-provider change for the pipeline: `cerebras-cloud-sdk` dependency, `PIPELINE_LLM_PROVIDER=cerebras`, dedicated client (Cerebras key only, no SDK TCP warm-up), and catalog-model fallback. | Uncommitted work on branch `feat/cerebras-llm-provider` in a hypebase-platform worktree, 2026-11-03. |
| `cerebras/tests/test_cerebras_provider.py` | The 5 tests that ship with that change (SDK constructor is faked; factories/settings/resolution are production code). | Same. |

## Applying the Cerebras change back to hypebase-platform

```bash
cd <hypebase-platform-checkout>
git apply --check pi-durable-harness/cerebras/pipeline-llm-provider.patch
git apply pi-durable-harness/cerebras/pipeline-llm-provider.patch
cp pi-durable-harness/cerebras/tests/test_cerebras_provider.py pipeline/tests/
cd pipeline && uv sync --locked
```

The patch is against `a9f8a934`; if main has moved, rebase it — it touches
`pipeline/{app/config.py,app/clients/llm.py,app/api.py,pyproject.toml,uv.lock}`,
which change often. The test file is a new file and applies anywhere under
`pipeline/tests/`.

### Configuration

```bash
PIPELINE_LLM_PROVIDER=cerebras        # switch chat to Cerebras
PIPELINE_CEREBRAS_API_KEY=csk-...     # required when the provider is cerebras
PIPELINE_CEREBRAS_MODEL=qwen-3.8-27b  # fallback for catalog ids containing "/"
PIPELINE_CEREBRAS_BASE_URL=https://api.cerebras.ai   # optional
```

### Known differences from the OpenRouter path

- Dictation always stays on OpenRouter, regardless of `PIPELINE_LLM_PROVIDER`.
- Arize/OpenInference tracing instruments the OpenAI SDK only, so Cerebras
  chat calls are not traced.
- The SDK's constructor warms a synchronous TCP connection by default; the
  client factory sets `warm_tcp_connection=False` or it blocks the event loop
  on every chat turn.

## Provenance rule

Copies here diverge from `hypebase-platform` the moment either side changes.
Do not edit `sandbox/` here and expect it to match the platform; sync with
copies from the platform (or vice versa) deliberately, and record the source
commit when you do.