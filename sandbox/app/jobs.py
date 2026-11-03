"""On-disk job store.

Jobs live under ``{workspace_root}/_jobs/{org_id}/{job_id}.json`` so state
survives redeploys without adding Redis. Two protections keep executed code
away from it: the workspace id pattern rejects ``_jobs`` at the files API, and
on a hardened deploy the directory is service-owned with mode 700 while
executed code runs as its session's own uid (see executor.py) — a different
uid, so the kernel denies the read.

Same terminal/stuck contract as the pipeline's ingest jobs: ``isTerminal`` on
``completed``/``failed``; a job ``running`` longer than the watchdog window is
reported failed.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.workspace import WorkspaceError, _validate_id, workspace_root

_TERMINAL = ("completed", "failed")


def _jobs_dir(org_id: str) -> Path:
    _validate_id(org_id, "org_id")
    base = workspace_root() / "_jobs"
    d = base / org_id
    d.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)  # service-owned; kernel-denied to every session uid
    return d


def _job_path(org_id: str, job_id: str) -> Path:
    if not job_id.startswith("job-"):
        raise WorkspaceError("invalid job_id")
    _validate_id(job_id.removeprefix("job-"), "job_id")
    return _jobs_dir(org_id) / f"{job_id}.json"


def create_job(org_id: str, conversation_id: str) -> dict[str, Any]:
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "org_id": org_id,
        "conversation_id": conversation_id,
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "files_created": [],
        # Set by harness jobs only: proxy accounting (hyp-90f.2).
        "llm_usage": None,
    }
    save_job(job)
    return job


def save_job(job: dict[str, Any]) -> None:
    job["updated_at"] = time.time()
    path = _job_path(job["org_id"], job["job_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


def get_job(org_id: str, job_id: str) -> dict[str, Any] | None:
    path = _job_path(org_id, job_id)
    if not path.is_file():
        return None
    job = json.loads(path.read_text(encoding="utf-8"))
    # Stuck watchdog: a job that has been non-terminal past the window is
    # synthesized as failed so callers stop polling a zombie.
    if job["status"] not in _TERMINAL:
        age = time.time() - job["updated_at"]
        if age > settings.job_stuck_timeout_s:
            job["status"] = "failed"
            job["stderr"] = (
                job.get("stderr") or ""
            ) + f"\n[watchdog] job stuck for {int(age)}s; marked failed"
            save_job(job)
    return job


def public_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobId": job["job_id"],
        "status": job["status"],
        "exitCode": job["exit_code"],
        "stdout": job["stdout"],
        "stderr": job["stderr"],
        "timedOut": job["timed_out"],
        "filesCreated": job["files_created"],
        "llmUsage": job.get("llm_usage"),
        "isTerminal": job["status"] in _TERMINAL,
    }
