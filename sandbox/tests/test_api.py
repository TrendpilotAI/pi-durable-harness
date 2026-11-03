import time

from tests.conftest import TEST_KEY


def _wait_terminal(client, org, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{org}/{job_id}").json()
        if job.get("isTerminal"):
            return job
        time.sleep(0.1)
    raise AssertionError("job never reached terminal state")


class TestAuth:
    def test_health_public(self, client):
        client.headers.pop("X-Sandbox-Key")
        assert client.get("/health").status_code == 200

    def test_missing_key_401(self, client):
        client.headers.pop("X-Sandbox-Key")
        assert client.post("/v1/exec", json={}).status_code == 401

    def test_wrong_key_401(self, client):
        client.headers["X-Sandbox-Key"] = "wrong"
        assert client.get("/v1/files/org1/conv1").status_code == 401

    def test_unconfigured_hash_fails_closed(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "api_key_sha256", "")
        client.headers["X-Sandbox-Key"] = TEST_KEY
        assert client.get("/v1/files/org1/conv1").status_code == 503


class TestExecFlow:
    def test_submit_poll_complete(self, client):
        r = client.post(
            "/v1/exec",
            json={
                "org_id": "org1",
                "conversation_id": "conv1",
                "code": "print('result: 42')\nopen('answer.txt','w').write('42')",
            },
        )
        assert r.status_code == 200
        job_id = r.json()["jobId"]
        job = _wait_terminal(client, "org1", job_id)
        assert job["status"] == "completed"
        assert "result: 42" in job["stdout"]
        assert job["filesCreated"] == ["answer.txt"]

        # The created file is readable through the files API.
        f = client.get("/v1/files/org1/conv1/answer.txt").json()
        assert f["content"] == "42"

    def test_unknown_job_terminal_404(self, client):
        r = client.get("/v1/jobs/org1/job-000000000000")
        assert r.status_code == 404
        assert r.json()["isTerminal"] is True

    def test_invalid_org_rejected(self, client):
        r = client.post(
            "/v1/exec",
            json={"org_id": "../evil", "conversation_id": "c", "code": "1"},
        )
        assert r.status_code == 400


class TestFiles:
    def test_put_list_get_roundtrip(self, client):
        body = b"col1,col2\n1,2\n"
        r = client.put("/v1/files/org1/conv2/data/input.csv", content=body)
        assert r.status_code == 200
        listing = client.get("/v1/files/org1/conv2").json()["files"]
        assert [f["path"] for f in listing] == ["data/input.csv"]
        got = client.get("/v1/files/org1/conv2/data/input.csv").json()
        assert got["content"] == body.decode()
        assert got["truncated"] is False

    def test_get_offset_and_cap(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "file_read_cap_bytes", 5)
        client.put("/v1/files/org1/conv3/big.txt", content=b"0123456789")
        got = client.get("/v1/files/org1/conv3/big.txt", params={"offset": 2}).json()
        assert got["content"] == "23456"
        assert got["truncated"] is True

    def test_traversal_rejected(self, client):
        # httpx normalizes "../" client-side, so exercise the validator with
        # payloads that survive URL normalization: encoded dots and dot
        # segments as literal path parts reaching the route parameter.
        assert client.put("/v1/files/org1/conv4/%2e%2e/x", content=b"x").status_code == 400
        assert client.put("/v1/files/org1/conv4/a/%2e%2e/b", content=b"x").status_code == 400
        assert client.get("/v1/files/org1/conv4/%2e%2e%2fsecret").status_code in (400, 404)
        # And the validator itself rejects raw traversal (unit-level guarantee).
        import pytest as _pytest

        from app.workspace import WorkspaceError, validate_relative_path

        with _pytest.raises(WorkspaceError):
            validate_relative_path("../../x")

    def test_delete_session(self, client):
        client.put("/v1/files/org1/conv5/a.txt", content=b"x")
        assert client.delete("/v1/sessions/org1/conv5").json()["deleted"] is True
        assert client.get("/v1/files/org1/conv5").status_code == 404


class TestJobsIsolationFromFilesApi:
    def test_jobs_state_not_reachable(self, client):
        # `_jobs` fails the org-id pattern at the files API layer.
        assert client.get("/v1/files/_jobs/org1").status_code == 400


# ------------------------------------------------------- per-session uid ----

import os
import stat
import sys

import pytest

from app.config import settings as _settings
from app.workspace import resolve_session_dir as _resolve

_ROOT_LINUX = os.geteuid() == 0 and sys.platform == "linux"


class TestReadyProbe:
    def test_ready_reports_per_session_uid(self, client):
        body = client.get("/ready").json()
        assert body["status"] == "ready"
        assert "uid_separation" not in body
        assert body["per_session_uid"] is _ROOT_LINUX
        assert "seccomp_netblock" in body and "netns" in body

    def test_lifespan_creates_service_dirs(self, client, ws):
        assert stat.S_IMODE((ws / "_meta").stat().st_mode) == 0o700
        assert stat.S_IMODE((ws / "_shared").stat().st_mode) == 0o755


@pytest.mark.root_linux
class TestOwnershipThroughApi:
    def test_put_file_owned_by_session_uid(self, client, ws):
        r = client.put("/v1/files/org1/conv-own/tools/export.json", content=b"{}")
        assert r.status_code == 200
        session = _resolve("org1", "conv-own")
        uid = os.lstat(session).st_uid
        assert uid >= _settings.uid_base
        assert os.lstat(session / "tools").st_uid == uid
        assert os.lstat(session / "tools" / "export.json").st_uid == uid
        # And the child can read what the pipeline offloaded.
        r = client.post(
            "/v1/exec",
            json={
                "org_id": "org1",
                "conversation_id": "conv-own",
                "code": "print('GOT', open('tools/export.json').read())",
            },
        )
        job = _wait_terminal(client, "org1", r.json()["jobId"])
        assert job["status"] == "completed", job
        assert "GOT {}" in job["stdout"]

    def test_jobs_dir_stays_root_0700(self, client, ws):
        r = client.post(
            "/v1/exec",
            json={"org_id": "org1", "conversation_id": "conv-j", "code": "print(1)"},
        )
        _wait_terminal(client, "org1", r.json()["jobId"])
        st = os.lstat(ws / "_jobs")
        assert st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700
        st = os.lstat(ws / "_shared")
        assert st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o755

    def test_exec_migrates_legacy_session_and_reads_old_file(self, client, ws):
        # Acceptance #4: a pre-existing session owned by the old `runner` uid
        # keeps working through the API, with its old files readable.
        legacy = ws / "org1" / "conv-legacy" / "tools"
        legacy.mkdir(parents=True)
        (legacy / "exportEntities-42c17f2f.json").write_text('{"rows": 3}')
        for p in (legacy.parent, legacy, legacy / "exportEntities-42c17f2f.json"):
            os.chown(p, 999, 999)
        r = client.post(
            "/v1/exec",
            json={
                "org_id": "org1",
                "conversation_id": "conv-legacy",
                "code": "print(open('tools/exportEntities-42c17f2f.json').read())",
            },
        )
        job = _wait_terminal(client, "org1", r.json()["jobId"])
        assert job["status"] == "completed", job
        assert '{"rows": 3}' in job["stdout"]
        assert os.lstat(legacy.parent).st_uid >= _settings.uid_base
        got = client.get("/v1/files/org1/conv-legacy/tools/exportEntities-42c17f2f.json").json()
        assert got["content"] == '{"rows": 3}'
