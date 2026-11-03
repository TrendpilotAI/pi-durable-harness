"""Sandbox service HTTP surface.

Executor API contract (stable — a future swap to a managed sandbox provider
replaces the implementation behind these routes, not the routes):

    POST   /v1/exec                                    submit code -> {jobId}
    GET    /v1/jobs/{org}/{job_id}                     poll -> status envelope
    PUT    /v1/files/{org}/{conversation}/{path}       write file (raw body)
    GET    /v1/files/{org}/{conversation}/{path}       read file (capped)
    GET    /v1/files/{org}/{conversation}              list session files
    DELETE /v1/sessions/{org}/{conversation}           delete session
    GET    /health, /ready                             public probes
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app import executor, jobs, llm_proxy, workspace
from app.auth import require_sandbox_key
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Startup probe results surfaced by /ready. Filled by the lifespan.
_isolation = {"per_session_uid": False, "llm_proxy": False}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Service-owned trees first (root 0700/0755), then prove this container
    # can actually own files as a per-session uid. On Railway a user namespace
    # with a small uid map would make every chown fail: surface that here as
    # per_session_uid:false, before any session is touched (hyp-90f.1).
    workspace.ensure_service_dirs()
    _isolation["per_session_uid"] = executor.probe_per_session_uid(workspace.meta_dir())
    # Unix-socket LLM egress for harness children (hyp-90f.2). Failure to
    # bind is logged, not fatal: python jobs do not need it.
    proxy = llm_proxy.proxy_from_settings()
    if proxy is not None:
        try:
            await proxy.start()
            _isolation["llm_proxy"] = True
        except OSError:
            logger.exception("LLM proxy failed to start; harness jobs will be refused")
            proxy = None
    try:
        yield
    finally:
        if proxy is not None:
            await proxy.stop()
            _isolation["llm_proxy"] = False


app = FastAPI(title="hypebase-sandbox", docs_url=None, redoc_url=None, lifespan=_lifespan)

# Bounded execution concurrency; further submits queue in the pool.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="exec")


@app.exception_handler(workspace.WorkspaceError)
async def _workspace_error(_request: Request, exc: workspace.WorkspaceError):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc)})


# ---------------------------------------------------------------- probes ----


@app.get("/health")
def health() -> dict:
    return {"status": "healthy", "service": "sandbox"}


@app.get("/ready")
def ready() -> dict:
    root = workspace.workspace_root()
    writable = root.is_dir()
    if not writable:
        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"status": "unavailable", "workspace": "missing"},
        )
    return {
        "status": "ready",
        "workspace": "ok",
        # True only when the startup probe could chown to the session uid
        # range. False under root means execs are REFUSED (fail closed).
        "per_session_uid": _isolation["per_session_uid"],
        "netns": bool(executor.unshare_prefix()),
        # The layer that actually blocks the network on Railway (hyp-sxb).
        # False here means executed code can reach the internet.
        "seccomp_netblock": executor.seccomp_active(),
        # Harness egress (hyp-90f.2): the unix-socket proxy is bound and the
        # upstream key is configured. False means harness jobs fail.
        "llm_proxy": _isolation["llm_proxy"] and bool(settings.openrouter_api_key),
    }


# ------------------------------------------------------------------ exec ----


Entrypoint = Literal["python", "harness:pi"]


class ExecRequest(BaseModel):
    org_id: str
    conversation_id: str
    # For ``python`` this is the script; for ``harness:pi`` it is the task
    # prompt (capped separately below: it travels in argv).
    code: str = Field(min_length=1, max_length=200_000)
    timeout_s: float | None = None
    entrypoint: Entrypoint = "python"
    # harness:pi only. OpenRouter slug, validated against SANDBOX_HARNESS_MODELS;
    # None picks the first allowed model.
    model: str | None = None

    @field_validator("model")
    @classmethod
    def _allowed_model(cls, value: str | None) -> str | None:
        if value is not None and value not in settings.harness_models:
            raise ValueError("model not in SANDBOX_HARNESS_MODELS")
        return value

    @model_validator(mode="after")
    def _task_cap(self) -> ExecRequest:
        # The harness task travels in argv: cap it well below ARG_MAX.
        if self.entrypoint == "harness:pi" and (
            len(self.code.encode("utf-8")) > settings.harness_task_max_bytes
        ):
            raise ValueError("harness task exceeds SANDBOX_HARNESS_TASK_MAX_BYTES")
        return self


def _run_job(job: dict, code: str, timeout_s: float, entrypoint: Entrypoint, model: str) -> None:
    """Worker-thread body: transition queued -> running -> terminal."""
    try:
        session = workspace.resolve_session_dir(
            job["org_id"], job["conversation_id"], create=True
        )
        job["status"] = "running"
        jobs.save_job(job)
        if entrypoint == "harness:pi":
            result = executor.run_harness(session, code, model, job["job_id"], timeout_s)
            job["llm_usage"] = result.llm_usage
        else:
            result = executor.run_code(session, code, timeout_s)
        job["status"] = "failed" if result.timed_out else "completed"
        job["exit_code"] = result.exit_code
        job["stdout"] = result.stdout
        job["stderr"] = result.stderr
        job["timed_out"] = result.timed_out
        job["files_created"] = result.files_created
    except executor.HarnessError as exc:
        logger.warning("harness job %s refused: %s", job["job_id"], exc)
        job["status"] = "failed"
        job["stderr"] = (job.get("stderr") or "") + "\n[sandbox] harness unavailable"
    except Exception:
        logger.exception("exec job %s crashed", job["job_id"])
        job["status"] = "failed"
        job["stderr"] = (job.get("stderr") or "") + "\n[sandbox] internal execution error"
    jobs.save_job(job)


@app.post("/v1/exec", dependencies=[Depends(require_sandbox_key)])
async def submit_exec(body: ExecRequest) -> dict:
    # Validate ids and create the session before accepting the job. Off the
    # event loop: create=True may lazily re-own a whole legacy tree.
    await run_in_threadpool(
        workspace.resolve_session_dir, body.org_id, body.conversation_id, create=True
    )
    job = jobs.create_job(body.org_id, body.conversation_id)
    if body.entrypoint == "harness:pi":
        timeout_s = body.timeout_s or settings.harness_timeout_s
    else:
        timeout_s = body.timeout_s or settings.exec_timeout_default_s
    model = body.model or settings.harness_models[0]
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_pool, _run_job, job, body.code, timeout_s, body.entrypoint, model)
    return {"jobId": job["job_id"], "status": "queued"}


@app.get("/v1/jobs/{org_id}/{job_id}", dependencies=[Depends(require_sandbox_key)])
def get_job(org_id: str, job_id: str) -> dict:
    job = jobs.get_job(org_id, job_id)
    if job is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"jobId": job_id, "status": "not_found", "isTerminal": True},
        )
    return jobs.public_view(job)


# ----------------------------------------------------------------- files ----


@app.put(
    "/v1/files/{org_id}/{conversation_id}/{rel_path:path}",
    dependencies=[Depends(require_sandbox_key)],
)
async def put_file(org_id: str, conversation_id: str, rel_path: str, request: Request) -> dict:
    # for_write=True may lazily re-own a legacy tree: keep it off the loop.
    target = await run_in_threadpool(
        workspace.resolve_file, org_id, conversation_id, rel_path, for_write=True
    )
    body = await request.body()

    def _write() -> None:
        session = workspace.resolve_session_dir(org_id, conversation_id)
        workspace.enforce_write_quota(session, len(body))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        # The service wrote on the session's behalf: hand the file (and any
        # dirs it created) to the session uid so the child can read and
        # modify it.
        workspace.own_by_session(session, target)

    await run_in_threadpool(_write)
    return {"path": rel_path, "size": len(body)}


@app.get(
    "/v1/files/{org_id}/{conversation_id}/{rel_path:path}",
    dependencies=[Depends(require_sandbox_key)],
)
def get_file(
    org_id: str,
    conversation_id: str,
    rel_path: str,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
) -> dict:
    target = workspace.resolve_file(org_id, conversation_id, rel_path)
    if not target.is_file():
        raise workspace.WorkspaceError("file not found", status_code=404)
    size = target.stat().st_size
    cap = min(limit or settings.file_read_cap_bytes, settings.file_read_cap_bytes)
    with target.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(cap)
    return {
        "path": rel_path,
        "size": size,
        "offset": offset,
        "returned": len(chunk),
        "truncated": offset + len(chunk) < size,
        "content": chunk.decode("utf-8", errors="replace"),
    }


@app.get(
    "/v1/files/{org_id}/{conversation_id}",
    dependencies=[Depends(require_sandbox_key)],
)
def list_files(org_id: str, conversation_id: str) -> dict:
    session = workspace.resolve_session_dir(org_id, conversation_id)
    return {"files": workspace.list_session_files(session)}


@app.delete(
    "/v1/sessions/{org_id}/{conversation_id}",
    dependencies=[Depends(require_sandbox_key)],
)
def delete_session(org_id: str, conversation_id: str) -> dict:
    session = workspace.resolve_session_dir(org_id, conversation_id)
    shutil.rmtree(session)
    return {"deleted": True}
