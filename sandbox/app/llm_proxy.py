"""Unix-socket LLM egress proxy for harness children (hyp-90f.2 spike).

The child runs under a seccomp filter that denies IP sockets and with a
scrubbed environment, so it has no credential and no route to the internet.
This proxy is its only way to a model:

    child --AF_UNIX--> /run/hypebase/llm.sock --TCP/TLS--> OpenRouter

Identity is the kernel's, not a header: ``SO_PEERCRED`` gives the peer's uid,
which (hyp-90f.1) is the session's own uid. The executor registers that uid
in ``_active_jobs`` right before ``Popen`` and removes it after
``communicate()``; a connection from any other uid, from a uid below
``uid_base``, or from a uid with no job in flight is refused with 403. A
header or a per-job socket path would be forgeable from pi's bash tool; the
peer uid is not.

Per request: allowlist ``POST /api/v1/chat/completions`` only (any Host),
strip the inbound ``Authorization``, inject the real key server-side, force
usage reporting (``stream_options.include_usage`` + OpenRouter's
``usage.include``), clamp ``max_tokens``, cap the body, and close the
connection after the response. Usage from the final SSE chunk (or the JSON
body) is accumulated on the job's budget; past the per-job token or request
budget the child gets 429.

Not a second uvicorn: ASGI never exposes the peer socket, and h11 (already a
uvicorn dependency) is enough to parse one request per connection.

POLICY CHANGE (documented in config.py): the sandbox service now holds one
secret, ``SANDBOX_OPENROUTER_API_KEY``. It never enters a child's environment;
``/proc/<service-pid>/environ`` is 0400 root; the proxy scopes its use per
active job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import h11
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_METHOD = "POST"
ALLOWED_PATH = "/api/v1/chat/completions"
_MAX_DENIED_LOGGED = 50


class ProxyError(RuntimeError):
    """Job registry misuse (e.g. two harness jobs on one uid)."""


@dataclass
class JobBudget:
    """Per-job accounting, keyed by the session uid while the job runs."""

    job_id: str
    uid: int
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    denied: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "denied": list(self.denied),
        }

    def note_denied(self, method: str, target: str, status: int) -> None:
        if len(self.denied) < _MAX_DENIED_LOGGED:
            self.denied.append(f"{method} {target} -> {status}")


# ------------------------------------------------------------ registry ----
#
# Mutated from executor worker threads, read from the event loop. A uid is a
# *session*, and this spike runs one harness job per session at a time: a
# second registration on a live uid is refused rather than merged, because
# the proxy could not tell the two children's requests apart (production
# gap, see docs/reviews/harness-in-sandbox-spike.md).

_active_jobs: dict[int, JobBudget] = {}
_registry_lock = threading.Lock()


def register_job(uid: int, job_id: str) -> JobBudget:
    with _registry_lock:
        if uid in _active_jobs:
            raise ProxyError(f"uid {uid} already has harness job {_active_jobs[uid].job_id}")
        budget = JobBudget(job_id=job_id, uid=uid)
        _active_jobs[uid] = budget
        return budget


def unregister_job(uid: int) -> JobBudget | None:
    with _registry_lock:
        return _active_jobs.pop(uid, None)


def active_job(uid: int) -> JobBudget | None:
    with _registry_lock:
        return _active_jobs.get(uid)


# ----------------------------------------------------------- peer creds ----


def peer_uid(sock: socket.socket) -> int | None:
    """Kernel-asserted uid of the connected AF_UNIX peer.

    Linux: ``SO_PEERCRED`` -> ``struct ucred {pid, uid, gid}``. macOS (dev
    tests only): ``LOCAL_PEERCRED`` -> ``struct xucred``. ``None`` when the
    platform offers neither; callers must treat that as "refuse".
    """
    try:
        if sys.platform == "linux":
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        if sys.platform == "darwin":
            # SOL_LOCAL = 0, LOCAL_PEERCRED = 1; xucred = {u_int version;
            # uid_t uid; short ngroups; gid_t groups[16]} with native padding.
            fmt = "@IIh16I"
            raw = sock.getsockopt(0, 1, struct.calcsize(fmt))
            fields = struct.unpack(fmt, raw)
            return int(fields[1])
    except OSError:
        return None
    return None


# --------------------------------------------------------------- server ----


def _json_response(status: int, message: str, code: str) -> tuple[int, list, bytes]:
    body = json.dumps({"error": {"message": message, "code": code}}).encode()
    headers = [
        ("content-type", "application/json"),
        ("content-length", str(len(body))),
        ("connection", "close"),
    ]
    return status, headers, body


def _usage_from_obj(obj: Any) -> dict | None:
    if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
        return obj["usage"]
    return None


class _SseUsageTee:
    """Scans a streamed SSE body for the final chunk carrying ``usage``."""

    def __init__(self) -> None:
        self._buf = b""
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                usage = _usage_from_obj(json.loads(payload))
            except ValueError:
                continue
            if usage:
                self.usage = usage


class LLMProxy:
    def __init__(
        self,
        *,
        socket_path: str | Path,
        upstream_url: str,
        api_key: str,
        min_uid: int,
        token_budget: int,
        max_requests: int,
        max_tokens_per_request: int,
        body_cap_bytes: int,
        upstream_timeout_s: float,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.upstream_url = upstream_url
        self.api_key = api_key
        self.min_uid = min_uid
        self.token_budget = token_budget
        self.max_requests = max_requests
        self.max_tokens_per_request = max_tokens_per_request
        self.body_cap_bytes = body_cap_bytes
        self.upstream_timeout_s = upstream_timeout_s
        self._server: asyncio.AbstractServer | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        d = self.socket_path.parent
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o711)  # traversable by every session uid, not listable
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.upstream_timeout_s))
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        self.socket_path.chmod(0o666)  # any uid may connect; SO_PEERCRED gates
        logger.info("LLM proxy listening on %s -> %s", self.socket_path, self.upstream_url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    # -- one request per connection ------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=64 * 1024)
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        try:
            await self._serve_one(conn, reader, writer, uid)
        except Exception:
            logger.exception("LLM proxy: connection from uid %s failed", uid)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _read_request(
        self, conn: h11.Connection, reader: asyncio.StreamReader
    ) -> tuple[h11.Request | None, bytes, bool]:
        """Returns (request, body, over_cap)."""
        request: h11.Request | None = None
        body = b""
        over_cap = False
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(65536)
                conn.receive_data(data)
                if not data:
                    return request, body, over_cap
                continue
            if isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                if not over_cap:
                    body += event.data
                    if len(body) > self.body_cap_bytes:
                        over_cap = True
                        body = b""
            elif event is h11.PAUSED or isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                return request, body, over_cap

    async def _send(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        status: int,
        headers: list,
        body: bytes,
    ) -> None:
        writer.write(conn.send(h11.Response(status_code=status, headers=headers)))
        if body:
            writer.write(conn.send(h11.Data(data=body)))
        writer.write(conn.send(h11.EndOfMessage()))
        await writer.drain()

    async def _serve_one(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid: int | None,
    ) -> None:
        request, body, over_cap = await self._read_request(conn, reader)
        if request is None:
            return
        method = request.method.decode()
        target = request.target.decode(errors="replace")

        # 1. Identity: kernel peer uid must map to a job in flight.
        budget = active_job(uid) if uid is not None else None
        if uid is None or uid < self.min_uid or budget is None:
            logger.warning(
                "LLM proxy: refused %s %s from uid %s (no active job)", method, target, uid
            )
            await self._send(
                conn, writer, *_json_response(403, "no active job for peer uid", "forbidden")
            )
            return

        # 2. Allowlist: one method, one exact path, no query string.
        if method != ALLOWED_METHOD or target != ALLOWED_PATH:
            budget.note_denied(method, target, 403)
            logger.warning("LLM proxy: job %s denied %s %s", budget.job_id, method, target)
            await self._send(conn, writer, *_json_response(403, "path not allowed", "forbidden"))
            return

        if over_cap:
            budget.note_denied(method, target, 413)
            await self._send(
                conn, writer, *_json_response(413, "request body too large", "too_large")
            )
            return
        if not self.api_key:
            await self._send(
                conn, writer, *_json_response(503, "LLM proxy has no upstream key", "unavailable")
            )
            return

        # 3. Budget, checked before spending anything upstream.
        if budget.requests >= self.max_requests:
            budget.note_denied(method, target, 429)
            await self._send(
                conn, writer, *_json_response(429, "per-job request budget exhausted", "budget")
            )
            return
        if budget.total_tokens >= self.token_budget:
            budget.note_denied(method, target, 429)
            await self._send(
                conn, writer, *_json_response(429, "per-job token budget exhausted", "budget")
            )
            return

        # 4. Rewrite the body: usage on, max_tokens clamped.
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            await self._send(
                conn, writer, *_json_response(400, "body is not a JSON object", "bad_request")
            )
            return
        streaming = bool(payload.get("stream"))
        if streaming:
            opts = payload.get("stream_options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            opts["include_usage"] = True
            payload["stream_options"] = opts
        payload["usage"] = {"include": True}
        cap = self.max_tokens_per_request
        for key in ("max_tokens", "max_completion_tokens"):
            if key in payload:
                try:
                    payload[key] = min(int(payload[key]), cap)
                except (TypeError, ValueError):
                    payload[key] = cap
        if "max_tokens" not in payload and "max_completion_tokens" not in payload:
            payload["max_tokens"] = cap
        out_body = json.dumps(payload).encode()

        # 5. Headers: drop hop-by-hop + inbound auth, inject the real key.
        drop = {
            "authorization",
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
            "keep-alive",
            "proxy-authorization",
            "te",
            "upgrade",
            "expect",
            "content-type",  # re-added below, exactly once
        }
        out_headers = [
            (k.decode(), v.decode(errors="replace"))
            for k, v in request.headers
            if k.decode().lower() not in drop
        ]
        out_headers.append(("Authorization", f"Bearer {self.api_key}"))
        out_headers.append(("Content-Type", "application/json"))

        budget.requests += 1
        assert self._client is not None
        tee = _SseUsageTee()
        json_body = b""
        try:
            async with self._client.stream(
                "POST", self.upstream_url, headers=out_headers, content=out_body
            ) as upstream:
                resp_headers = [
                    (k, v)
                    for k, v in upstream.headers.multi_items()
                    if k.lower() not in ("content-length", "transfer-encoding", "connection")
                ]
                resp_headers.append(("connection", "close"))
                ctype = upstream.headers.get("content-type", "")
                is_sse = "text/event-stream" in ctype
                writer.write(
                    conn.send(h11.Response(status_code=upstream.status_code, headers=resp_headers))
                )
                async for chunk in upstream.aiter_bytes():
                    if is_sse:
                        tee.feed(chunk)
                    else:
                        json_body += chunk
                    writer.write(conn.send(h11.Data(data=chunk)))
                    await writer.drain()
                writer.write(conn.send(h11.EndOfMessage()))
                await writer.drain()
        except httpx.HTTPError as exc:
            logger.warning(
                "LLM proxy: upstream error for job %s: %s", budget.job_id, type(exc).__name__
            )
            if conn.our_state is h11.SEND_RESPONSE:
                await self._send(
                    conn, writer, *_json_response(502, "upstream unavailable", "upstream")
                )
            return

        usage = tee.usage if is_sse else None
        if usage is None and json_body:
            try:
                usage = _usage_from_obj(json.loads(json_body))
            except ValueError:
                usage = None
        if usage:
            budget.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            budget.completion_tokens += int(usage.get("completion_tokens") or 0)
            try:
                budget.cost_usd += float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
        else:
            logger.warning("LLM proxy: job %s response carried no usage", budget.job_id)


# -------------------------------------------------------------- lifespan ----


def proxy_from_settings() -> LLMProxy | None:
    """The service-wide proxy, or ``None`` when no socket path is configured."""
    if not settings.llm_proxy_socket:
        return None
    # Dev mode (service not root): children share the service uid, which is
    # below uid_base; gate on that uid instead so local runs still work.
    min_uid = settings.uid_base if os.geteuid() == 0 else 0
    if not settings.openrouter_api_key:
        logger.warning("SANDBOX_OPENROUTER_API_KEY unset: harness jobs will get 503 from the proxy")
    return LLMProxy(
        socket_path=settings.llm_proxy_socket,
        upstream_url=settings.harness_upstream_url,
        api_key=settings.openrouter_api_key,
        min_uid=min_uid,
        token_budget=settings.harness_token_budget,
        max_requests=settings.harness_max_requests,
        max_tokens_per_request=settings.harness_max_tokens_per_request,
        body_cap_bytes=settings.harness_body_cap_bytes,
        upstream_timeout_s=settings.harness_upstream_timeout_s,
    )
