"""Unix-socket LLM egress proxy (hyp-90f.2).

Portable: registers the test's own uid as an active job, connects over the
UDS with httpx, and talks to an in-test stub upstream. Asserts the allowlist,
peer-uid gating, key injection, request rewriting, budgets, and usage
accounting. No network, no root needed.
Explicit asyncio markers: the in-image run has no pyproject.toml, so
asyncio_mode=auto does not apply there.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import h11
import httpx
import pytest
import pytest_asyncio

from app import llm_proxy
from app.llm_proxy import LLMProxy, register_job, unregister_job

MY_UID = os.geteuid()
KEY = "or-secret-key-never-in-child"
ALLOWED = "http://openrouter.ai/api/v1/chat/completions"


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


class StubUpstream:
    """Minimal HTTP/1.1 upstream on 127.0.0.1 recording every request."""

    def __init__(self, *, stream_usage: dict | None = None, json_usage: dict | None = None):
        self.requests: list[dict] = []
        self.stream_usage = stream_usage or {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "cost": 0.00042,
        }
        self.json_usage = json_usage or {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        }
        self.server: asyncio.AbstractServer | None = None
        self.url = ""

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/api/v1/chat/completions"
        return self

    async def __aexit__(self, *_exc):
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        conn = h11.Connection(h11.SERVER)
        req = None
        body = b""
        while True:
            ev = conn.next_event()
            if ev is h11.NEED_DATA:
                data = await reader.read(65536)
                conn.receive_data(data)
                if not data:
                    break
                continue
            if isinstance(ev, h11.Request):
                req = ev
            elif isinstance(ev, h11.Data):
                body += ev.data
            elif isinstance(ev, (h11.EndOfMessage, h11.ConnectionClosed)):
                break
        assert req is not None
        headers = {k.decode().lower(): v.decode() for k, v in req.headers}
        raw_headers = [(k.decode().lower(), v.decode()) for k, v in req.headers]
        payload = json.loads(body)
        self.requests.append(
            {
                "method": req.method.decode(),
                "target": req.target.decode(),
                "headers": headers,
                "raw_headers": raw_headers,
                "json": payload,
            }
        )
        if payload.get("stream"):
            # Shape pi's openai-completions parser accepts: a content delta,
            # a finish_reason chunk, then the usage-only chunk OpenRouter emits
            # with stream_options.include_usage.
            head = {
                "id": "stub",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": payload.get("model", "stub"),
            }
            out = (
                _sse(
                    {
                        **head,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "hi"},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + _sse({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                + _sse({**head, "choices": [], "usage": self.stream_usage})
                + b"data: [DONE]\n\n"
            )
            ctype = "text/event-stream"
        else:
            out = json.dumps(
                {
                    "id": "stub",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload.get("model", "stub"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": self.json_usage,
                }
            ).encode()
            ctype = "application/json"
        writer.write(
            conn.send(
                h11.Response(
                    status_code=200,
                    headers=[("content-type", ctype), ("content-length", str(len(out)))],
                )
            )
        )
        writer.write(conn.send(h11.Data(data=out)))
        writer.write(conn.send(h11.EndOfMessage()))
        await writer.drain()
        writer.close()


@pytest_asyncio.fixture()
async def upstream():
    async with StubUpstream() as stub:
        yield stub


@pytest.fixture()
def sock_path():
    # Short path on purpose: sun_path is 104 bytes on macOS.
    d = tempfile.mkdtemp(prefix="llmp")
    yield Path(d) / "llm.sock"


def _proxy(upstream: StubUpstream, sock_path: Path, **overrides) -> LLMProxy:
    kw = {
        "socket_path": sock_path,
        "upstream_url": upstream.url,
        "api_key": KEY,
        "min_uid": 0,
        "token_budget": 200_000,
        "max_requests": 40,
        "max_tokens_per_request": 4096,
        "body_cap_bytes": 1024 * 1024,
        "upstream_timeout_s": 10.0,
    }
    kw.update(overrides)
    return LLMProxy(**kw)


@pytest_asyncio.fixture()
async def proxy(upstream, sock_path):
    p = _proxy(upstream, sock_path)
    await p.start()
    yield p
    await p.stop()
    llm_proxy._active_jobs.clear()


@pytest.fixture()
def client(sock_path):
    transport = httpx.AsyncHTTPTransport(uds=str(sock_path))
    return httpx.AsyncClient(transport=transport, timeout=10.0)


@pytest.fixture()
def job():
    budget = register_job(MY_UID, "job-test")
    yield budget
    unregister_job(MY_UID)


def _chat(stream: bool = True, **extra) -> dict:
    body = {
        "model": "openai/gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    body.update(extra)
    return body


class TestRegistry:
    def test_double_registration_refused(self):
        register_job(MY_UID, "job-a")
        try:
            with pytest.raises(llm_proxy.ProxyError):
                register_job(MY_UID, "job-b")
        finally:
            assert unregister_job(MY_UID).job_id == "job-a"
        assert unregister_job(MY_UID) is None


@pytest.mark.asyncio
class TestPeerGating:
    async def test_unregistered_uid_is_403(self, proxy, client):
        r = await client.post(ALLOWED, json=_chat())
        assert r.status_code == 403
        assert "no active job" in r.json()["error"]["message"]

    async def test_uid_below_min_is_403_even_when_registered(self, upstream, sock_path, client):
        p = _proxy(upstream, sock_path, min_uid=MY_UID + 1)
        await p.start()
        try:
            register_job(MY_UID, "job-low")
            r = await client.post(ALLOWED, json=_chat())
            assert r.status_code == 403
            assert upstream.requests == []
        finally:
            unregister_job(MY_UID)
            await p.stop()

    async def test_socket_is_world_connectable_dir_0711(self, proxy, sock_path):
        assert sock_path.exists()
        assert (sock_path.stat().st_mode & 0o777) == 0o666
        assert (sock_path.parent.stat().st_mode & 0o777) == 0o711


@pytest.mark.asyncio
class TestAllowlist:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("GET", "http://openrouter.ai/api/v1/models"),
            ("POST", "http://openrouter.ai/api/v1/messages?beta=true"),
            ("POST", "http://openrouter.ai/api/v1/chat/completions/extra"),
            ("POST", "http://openrouter.ai/api/v1/chat/completions?x=1"),
            ("GET", "http://openrouter.ai/api/v1/chat/completions"),
            ("POST", "http://openrouter.ai/api/v1/auth/keys"),
        ],
    )
    async def test_off_allowlist_is_403_and_logged(self, proxy, client, upstream, job, method, url):
        r = await client.request(method, url, json=_chat())
        assert r.status_code == 403
        assert upstream.requests == []
        assert len(job.denied) == 1
        assert job.denied[0].startswith(method)

    async def test_any_host_accepted_for_allowed_path(self, proxy, client, upstream, job):
        r = await client.post("http://example.invalid/api/v1/chat/completions", json=_chat())
        assert r.status_code == 200
        assert len(upstream.requests) == 1


@pytest.mark.asyncio
class TestRewrite:
    async def test_key_injected_and_inbound_auth_stripped(self, proxy, client, upstream, job):
        r = await client.post(
            ALLOWED,
            json=_chat(),
            headers={"Authorization": "Bearer sandbox-proxy", "X-Custom": "keep"},
        )
        assert r.status_code == 200
        sent = upstream.requests[0]
        assert sent["headers"]["authorization"] == f"Bearer {KEY}"
        assert sent["headers"].get("x-custom") == "keep"
        assert sent["target"] == "/api/v1/chat/completions"
        # Exactly one of each single-valued header: the inbound client sends
        # its own content-type/authorization and the proxy must replace, not
        # append (a duplicate Content-Type is invalid per RFC 7230 §3.2.2).
        names = [k for k, _ in sent["raw_headers"]]
        assert names.count("content-type") == 1, sent["raw_headers"]
        assert names.count("authorization") == 1, sent["raw_headers"]
        assert names.count("host") == 1, sent["raw_headers"]

    async def test_usage_forced_and_max_tokens_clamped(self, proxy, client, upstream, job):
        r = await client.post(ALLOWED, json=_chat(max_tokens=64000))
        assert r.status_code == 200
        body = upstream.requests[0]["json"]
        assert body["stream_options"] == {"include_usage": True}
        assert body["usage"] == {"include": True}
        assert body["max_tokens"] == 4096

    async def test_max_tokens_added_when_absent(self, proxy, client, upstream, job):
        await client.post(ALLOWED, json=_chat(stream=False))
        body = upstream.requests[0]["json"]
        assert body["max_tokens"] == 4096
        assert "stream_options" not in body
        assert body["usage"] == {"include": True}

    async def test_stream_body_relayed_verbatim_with_connection_close(
        self, proxy, client, upstream, job
    ):
        r = await client.post(ALLOWED, json=_chat())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("connection") == "close"
        assert b"data: [DONE]" in r.content
        assert b'"content": "hi"' in r.content

    async def test_body_over_cap_is_413(self, upstream, sock_path, client):
        p = _proxy(upstream, sock_path, body_cap_bytes=200)
        await p.start()
        try:
            register_job(MY_UID, "job-big")
            r = await client.post(ALLOWED, json=_chat(padding="x" * 500))
            assert r.status_code == 413
            assert upstream.requests == []
        finally:
            unregister_job(MY_UID)
            await p.stop()

    async def test_invalid_json_is_400(self, proxy, client, upstream, job):
        r = await client.post(
            ALLOWED, content=b"{not json", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400
        assert upstream.requests == []

    async def test_missing_upstream_key_is_503(self, upstream, sock_path, client):
        p = _proxy(upstream, sock_path, api_key="")
        await p.start()
        try:
            register_job(MY_UID, "job-nokey")
            r = await client.post(ALLOWED, json=_chat())
            assert r.status_code == 503
            assert upstream.requests == []
        finally:
            unregister_job(MY_UID)
            await p.stop()


@pytest.mark.asyncio
class TestBudget:
    async def test_usage_accounted_from_sse_and_json(self, proxy, client, upstream, job):
        await client.post(ALLOWED, json=_chat())
        assert job.requests == 1
        assert (job.prompt_tokens, job.completion_tokens) == (120, 30)
        assert job.cost_usd == pytest.approx(0.00042)
        await client.post(ALLOWED, json=_chat(stream=False))
        assert job.requests == 2
        assert (job.prompt_tokens, job.completion_tokens) == (127, 33)
        view = job.as_dict()
        assert view["requests"] == 2 and view["total_tokens"] == 160
        assert view["denied"] == []

    async def test_request_cap_returns_429(self, upstream, sock_path, client):
        p = _proxy(upstream, sock_path, max_requests=1)
        await p.start()
        try:
            budget = register_job(MY_UID, "job-cap")
            assert (await client.post(ALLOWED, json=_chat())).status_code == 200
            r = await client.post(ALLOWED, json=_chat())
            assert r.status_code == 429
            assert "request budget" in r.json()["error"]["message"]
            assert budget.requests == 1
            assert len(upstream.requests) == 1
        finally:
            unregister_job(MY_UID)
            await p.stop()

    async def test_token_budget_returns_429(self, upstream, sock_path, client):
        p = _proxy(upstream, sock_path, token_budget=150)
        await p.start()
        try:
            register_job(MY_UID, "job-tok")
            assert (await client.post(ALLOWED, json=_chat())).status_code == 200  # uses 150
            r = await client.post(ALLOWED, json=_chat())
            assert r.status_code == 429
            assert "token budget" in r.json()["error"]["message"]
            assert len(upstream.requests) == 1
        finally:
            unregister_job(MY_UID)
            await p.stop()

    async def test_denied_requests_do_not_consume_budget(self, proxy, client, upstream, job):
        await client.get("http://openrouter.ai/api/v1/models")
        assert job.requests == 0


@pytest.mark.asyncio
class TestUpstreamFailure:
    async def test_upstream_unreachable_is_502(self, sock_path, client):
        p = LLMProxy(
            socket_path=sock_path,
            upstream_url="http://127.0.0.1:1/api/v1/chat/completions",
            api_key=KEY,
            min_uid=0,
            token_budget=1000,
            max_requests=5,
            max_tokens_per_request=100,
            body_cap_bytes=4096,
            upstream_timeout_s=2.0,
        )
        await p.start()
        try:
            register_job(MY_UID, "job-502")
            r = await client.post(ALLOWED, json=_chat())
            assert r.status_code == 502
        finally:
            unregister_job(MY_UID)
            await p.stop()
