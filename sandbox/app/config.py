"""Sandbox service settings.

Deliberately minimal. Until hyp-90f.2 this service held no credentials for
any other system; the only secret-adjacent value was the SHA-256 *hash* of
the inbound API key — executed code that reads this process's environment
learns the hash, never the key itself (the key lives only in the pipeline
service's env).

POLICY CHANGE (hyp-90f.2 spike): the service now holds exactly one secret,
``SANDBOX_OPENROUTER_API_KEY``, so a harness child can reach a model through
the unix-socket proxy (app/llm_proxy.py). The child never sees it: it is not
in any child env (``executor._clean_env`` is unchanged), ``/proc/<service
pid>/environ`` is 0400 root and the child is a different uid, and the proxy
only spends it on behalf of the uid's registered job, within that job's
budget. Unset means harness jobs get 503 from the proxy; python jobs are
unaffected.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Where session workspaces live. On Railway this is the mounted Volume;
    # locally it is a bind mount from docker-compose or a temp dir in tests.
    workspace_root: str = "/workspace"

    # Hex SHA-256 of the API key the pipeline presents in X-Sandbox-Key.
    # Empty means auth is not configured: every request is rejected (fail
    # closed) except /health and /ready.
    api_key_sha256: str = ""

    port: int = 8100

    # Execution limits (per job).
    exec_timeout_default_s: float = 60.0
    exec_timeout_max_s: float = 300.0
    exec_cpu_seconds: int = 30
    exec_memory_bytes: int = 512 * 1024 * 1024
    exec_max_processes: int = 16
    exec_max_file_bytes: int = 64 * 1024 * 1024
    exec_max_open_files: int = 64
    exec_output_cap_bytes: int = 64 * 1024  # stdout and stderr, each

    # Workspace quotas (per session directory).
    session_max_bytes: int = 256 * 1024 * 1024
    session_max_files: int = 2000
    file_read_cap_bytes: int = 512 * 1024  # single read response cap

    # A job stuck in `running` longer than this is reported failed
    # (same contract as pipeline's ingest-job watchdog).
    job_stuck_timeout_s: float = 300.0

    # First kernel uid handed to a session (hyp-90f.1). Each session dir is
    # owned by its own uid == gid allocated from a persistent counter under
    # ``{workspace_root}/_meta``; anything owned below this base is "not
    # ours" and gets migrated on next touch. Well above any distro range.
    uid_base: int = 100_000

    # ---- harness-in-sandbox (hyp-90f.2 spike) ------------------------------
    # The one secret this service holds (see module docstring). Never copied
    # into a child env.
    openrouter_api_key: str = ""
    # Unix socket the LLM proxy listens on. NOT on the volume: sockets go
    # stale across redeploys and _meta is 0700. Empty disables the proxy and
    # every harness job fails at launch.
    llm_proxy_socket: str = "/run/hypebase/llm.sock"
    harness_upstream_url: str = "https://openrouter.ai/api/v1/chat/completions"
    # OpenRouter slugs a caller may request; the first is the default. Every
    # entry must resolve to an ``openai-completions`` model in pi's registry
    # (Anthropic slugs route to /api/v1/messages, which the proxy denies).
    harness_models: list[str] = ["openai/gpt-5-mini"]
    harness_pi_cli: str = (
        "/opt/harness/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    )
    harness_shim: str = "/opt/harness/shim.mjs"
    harness_task_max_bytes: int = 64 * 1024
    # Per-job proxy budget.
    harness_token_budget: int = 200_000
    harness_max_requests: int = 40
    harness_max_tokens_per_request: int = 8192
    harness_body_cap_bytes: int = 1024 * 1024
    harness_upstream_timeout_s: float = 120.0
    # Node-shaped limits. RLIMIT_DATA is the real memory cap: it counts
    # private writable mappings (Linux >= 4.7) and excludes PROT_NONE
    # reservations. RLIMIT_AS is OFF (None) for the harness: V8 reserves a
    # ~10 GB guard region per WebAssembly memory, and Node's fetch (llhttp in
    # undici) is wasm — measured in the image (hyp-90f.2): AS at 4 GB and 8 GB
    # dies with "WebAssembly.instantiate(): Out of memory", 12 GB passes for
    # one instance, and pi loads several. NPROC 64 because pi spawns bash.
    harness_timeout_s: float = 300.0
    harness_cpu_seconds: int = 120
    harness_memory_bytes: int | None = None
    harness_data_bytes: int = 1024 * 1024 * 1024
    harness_max_processes: int = 64
    harness_max_open_files: int = 256

    @field_validator("harness_models", mode="before")
    @classmethod
    def _split_models(cls, value: object) -> object:
        # Railway env vars are strings: accept "a/b,c/d" as well as JSON.
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    model_config = {"env_prefix": "SANDBOX_", "env_file": ".env"}


settings = Settings()
