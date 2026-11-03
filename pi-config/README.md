# pi model environment

Snapshot of the dev machine's pi model configuration (2026-11-03), so the
harness has a known-good set of models to run episodes against.

## Files

| File | Install location (pi agent dir) | What it does |
|---|---|---|
| `models.json` | `<agent-dir>/models.json` | Registers the local `mlx` provider: five MLX models served by mlx-serve at `http://127.0.0.1:11234/v1` (dummy key `mlx-serve` — mlx-serve ignores it). |
| `settings.json` | `<agent-dir>/settings.json` | Sets the default model to `cerebras/qwen-3.8-27b` and the compaction reserve used for the harness. |

### Local mlx models (models.json)

| id | context | max output | notes |
|---|---|---|---|
| `ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit` | 262144 | 65536 | primary local chat/workhorse |
| `ddalcu/Qwen3.8-27B-MLX-Serve-4bit` | 262144 | 65536 | newer Qwen local |
| `RecursiveSI/flint-int4` | 162816 | 65536 | experimental |
| `ddalcu/MiMo-V2.6-Distill-Qwen-9B-MLX-Serve-6bit` | 262144 | 65536 | small distill |
| `mlx-community/Qwen3-Reranker-4B-mxfp8` | 40960 | 20480 | reranker |

These require the mlx-serve server running locally; the `MLX_SERVE_*`
override knobs control port and load behavior.

### Cerebras (not in models.json)

Cerebras is a built-in pi provider, so `models.json` carries nothing for it.
The two models used (`qwen-3.8-27b`, `gpt-oss-120b`) come from pi's bundled
catalog. Credentials live in the macOS Keychain under service
`pi-cerebras`, referenced from `auth.json` as
`"key": "!security find-generic-password -s pi-cerebras -w"`. **No key
material is in this directory** — do not add one.

## Install

```bash
cp models.json settings.json <pi-agent-dir>/
# then, on the dev machine, the cerebras auth.json entry (creates nothing new
# if the keychain item already exists):
security add-generic-password -U -a winner -s pi-cerebras -l "Cerebras API key (pi)"
# auth.json entry (key must be set interactively or via a secret manager):
#   "cerebras": { "type": "api_key", "key": "!security find-generic-password -s pi-cerebras -w" }
```

## Provenance

Copied from the dev machine's pi agent directory on 2026-11-03. Default
model was switched from `mlx/ddalcu/Qwen3.6-35B-A3B-MLX-Serve-4bit` to
`cerebras/qwen-3.8-27b` as part of the Cerebras provider validation. Re-sync
by copying both files back; do not hand-edit copies.