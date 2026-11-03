"""Inbound auth: X-Sandbox-Key verified against a stored SHA-256 hash.

The service env holds only ``SANDBOX_API_KEY_SHA256`` — the hex digest, never
the key. Executed code that manages to read this process's environment (e.g.
``/proc/*/environ`` on a non-hardened deploy) learns the hash, which is not
invertible and not accepted by this endpoint. The actual key lives solely in
the pipeline service's env as ``PIPELINE_SANDBOX_API_KEY``.

Fail closed: with no hash configured, every request is rejected.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, HTTPException

from app.config import settings


def require_sandbox_key(x_sandbox_key: str = Header(default="")) -> None:
    expected = settings.api_key_sha256.strip().lower()
    if not expected:
        raise HTTPException(status_code=503, detail="sandbox auth not configured")
    presented = hashlib.sha256(x_sandbox_key.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid sandbox key")
