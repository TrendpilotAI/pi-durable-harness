"""Harness entrypoint (hyp-90f.2 spike): pi runs inside the sandbox child.

The pi tests need ``node`` on PATH (the production image bakes it under
/usr/local/bin); the ownership assertions additionally need root on Linux.
Run the full set with sandbox/scripts/test-in-image.sh. Everything talks to
an in-test stub upstream through the real unix-socket proxy: no network.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from app import executor, llm_proxy
from app.config import settings
from app.workspace import resolve_session_dir
from tests.test_llm_proxy import KEY, StubUpstream

NODE = shutil.which("node")
PI_CLI = Path(settings.harness_pi_cli)
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")
needs_pi = pytest.mark.skipif(not PI_CLI.is_file(), reason=f"{PI_CLI} not installed")


@pytest.fixture()
def session(ws):
    return resolve_session_dir("org1", "conv-h", create=True)


@pytest.fixture()
def sock_path():
    d = tempfile.mkdtemp(prefix="llmh")
    yield Path(d) / "llm.sock"


@pytest_asyncio.fixture()
async def proxy(sock_path, monkeypatch):
    async with StubUpstream() as stub:
        p = llm_proxy.LLMProxy(
            socket_path=sock_path,
            upstream_url=stub.url,
            api_key=KEY,
            min_uid=0,
            token_budget=settings.harness_token_budget,
            max_requests=settings.harness_max_requests,
            max_tokens_per_request=settings.harness_max_tokens_per_request,
            body_cap_bytes=settings.harness_body_cap_bytes,
            upstream_timeout_s=10.0,
        )
        await p.start()
        monkeypatch.setattr(settings, "llm_proxy_socket", str(sock_path))
        try:
            yield stub
        finally:
            await p.stop()
            llm_proxy._active_jobs.clear()


class TestLimits:
    def test_harness_limits_are_node_shaped(self):
        lim = executor.harness_limits()
        # No RLIMIT_AS: V8 wasm guard regions need >12 GB of address space
        # each (measured in the image). RLIMIT_DATA is the real cap.
        assert lim.memory_bytes is None
        assert lim.data_bytes is not None and lim.data_bytes <= 1024**3
        assert lim.max_processes >= 64
        assert lim.max_open_files >= 256
        assert lim.cpu_seconds >= 120

    def test_python_limits_unchanged(self):
        lim = executor.python_limits()
        assert lim.memory_bytes == settings.exec_memory_bytes
        assert lim.data_bytes is None
        assert lim.max_processes == settings.exec_max_processes

    @needs_node
    def test_node_starts_under_harness_limits(self, session):
        result = executor.run_process(
            session,
            [NODE, "-e", "console.log('NODE_OK', process.version)"],
            executor.harness_env(session),
            executor.harness_limits(),
            timeout_s=30,
        )
        assert result.exit_code == 0, result.stderr
        assert "NODE_OK" in result.stdout

    @needs_node
    @pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_DATA only enforced on Linux")
    def test_node_allocation_capped_by_rlimit_data(self, session):
        # 1 GB RLIMIT_DATA: a 1.5 GB Buffer must fail, not succeed.
        result = executor.run_process(
            session,
            [NODE, "-e", "Buffer.alloc(1536*1024*1024); console.log('ALLOC_OK')"],
            executor.harness_env(session),
            executor.harness_limits(),
            timeout_s=30,
        )
        assert "ALLOC_OK" not in result.stdout


class TestHarnessEnv:
    def test_env_is_clean_env_plus_pi_knobs(self, session, monkeypatch):
        monkeypatch.setenv("SANDBOX_OPENROUTER_API_KEY", "must-not-leak")
        monkeypatch.setattr(settings, "openrouter_api_key", "must-not-leak")
        env = executor.harness_env(session)
        base = executor._clean_env(session)
        for k, v in base.items():
            assert env[k] == v
        assert env["PI_CODING_AGENT_DIR"] == str(session / ".pi")
        assert env["PI_OFFLINE"] == "1"
        assert env["PI_TELEMETRY"] == "0"
        assert env["PI_SKIP_VERSION_CHECK"] == "1"
        assert env["OPENROUTER_API_KEY"] == "sandbox-proxy"
        assert env["LLM_PROXY_SOCKET"] == settings.llm_proxy_socket
        assert "--import=" in env["NODE_OPTIONS"]
        assert env["NO_COLOR"] == "1"
        assert "must-not-leak" not in " ".join(env.values())

    def test_argv_uses_double_dash_before_task(self):
        argv = executor.harness_argv("openai/gpt-5-mini", "--not-a-flag")
        assert argv[0] == "node"
        assert argv[1] == settings.harness_pi_cli
        assert "-p" in argv and "--no-session" in argv and "--no-extensions" in argv
        assert argv[argv.index("--model") + 1] == "openrouter/openai/gpt-5-mini"
        assert argv[-2:] == ["--", "--not-a-flag"]


@needs_node
@needs_pi
@pytest.mark.asyncio
class TestPiThroughProxy:
    async def test_pi_completes_via_unix_socket_proxy(self, session, proxy):
        result = await asyncio.to_thread(
            executor.run_harness,
            session,
            "Say hello",
            settings.harness_models[0],
            "job-pi-1",
            120.0,
        )
        assert result.exit_code == 0, result.stderr
        assert "hi" in result.stdout, result.stdout
        # Every request pi made went through the proxy and got the real key.
        assert len(proxy.requests) >= 1
        for req in proxy.requests:
            assert req["target"] == "/api/v1/chat/completions"
            assert req["headers"]["authorization"] == f"Bearer {KEY}"
            assert req["json"]["stream_options"] == {"include_usage": True}
        assert result.llm_usage["requests"] == len(proxy.requests)
        assert result.llm_usage["denied"] == []
        assert result.llm_usage["prompt_tokens"] > 0
        for needle in ("EAFNOSUPPORT", "ENETUNREACH", "uv_os_get_passwd", "EACCES", "EPERM"):
            assert needle not in result.stderr, result.stderr
        # The job registry is cleaned up after the run.
        assert llm_proxy._active_jobs == {}

    @pytest.mark.root_linux
    async def test_pi_state_owned_by_session_uid(self, session, proxy):
        await asyncio.to_thread(
            executor.run_harness,
            session,
            "Say hello",
            settings.harness_models[0],
            "job-pi-2",
            120.0,
        )
        uid = os.lstat(session).st_uid
        assert uid >= settings.uid_base
        pi_dir = session / ".pi"
        assert pi_dir.is_dir()
        assert os.lstat(pi_dir).st_uid == uid


class TestApi:
    def test_default_entrypoint_is_python(self, client):
        r = client.post(
            "/v1/exec",
            json={"org_id": "org1", "conversation_id": "c1", "code": "print('py')"},
        )
        assert r.status_code == 200

    def test_unknown_entrypoint_rejected(self, client):
        r = client.post(
            "/v1/exec",
            json={"org_id": "org1", "conversation_id": "c1", "code": "x", "entrypoint": "bash"},
        )
        assert r.status_code == 422

    def test_harness_model_must_be_allowlisted(self, client):
        r = client.post(
            "/v1/exec",
            json={
                "org_id": "org1",
                "conversation_id": "c1",
                "code": "do things",
                "entrypoint": "harness:pi",
                "model": "openai/gpt-4o",
            },
        )
        assert r.status_code == 422

    def test_harness_task_capped_at_64kb(self, client):
        r = client.post(
            "/v1/exec",
            json={
                "org_id": "org1",
                "conversation_id": "c1",
                "code": "x" * (64 * 1024 + 1),
                "entrypoint": "harness:pi",
            },
        )
        assert r.status_code == 422

    def test_job_view_exposes_llm_usage(self, client):
        from app import jobs

        job = jobs.create_job("org1", "c1")
        view = jobs.public_view(job)
        assert view["llmUsage"] is None
        job["llm_usage"] = {"requests": 2, "total_tokens": 10}
        assert jobs.public_view(job)["llmUsage"] == {"requests": 2, "total_tokens": 10}
