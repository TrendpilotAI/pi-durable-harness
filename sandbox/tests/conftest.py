import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_KEY = "test-sandbox-key"
TEST_KEY_SHA256 = hashlib.sha256(TEST_KEY.encode()).hexdigest()

# The per-session-uid layer only exists when the service is container-root on
# Linux (the production image). Tests marked ``root_linux`` exercise that layer
# and skip everywhere else; run them with sandbox/scripts/test-in-image.sh.
ROOT_LINUX = os.geteuid() == 0 and sys.platform == "linux"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "root_linux: needs euid 0 on Linux (kernel uid separation); skipped otherwise",
    )


def pytest_runtest_setup(item):
    if item.get_closest_marker("root_linux") and not ROOT_LINUX:
        pytest.skip("requires root on Linux (run via sandbox/scripts/test-in-image.sh)")


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Point the workspace root at a temp dir and configure auth.

    The root is made world-traversable on purpose. When the suite runs as root
    (CI containers, the production image), the executor drops children to a
    per-session uid; pytest's tmp_path is root-owned 0700, so the child could
    not even read its own ``_exec_script.py`` and every executor test failed
    with a bare "Permission denied" that looked like a product bug. The real
    deployment does not hit this because the Dockerfile creates /workspace
    mode 0755. Mirror that here so the tests exercise the same permission
    shape they will run under.
    """
    from app.config import settings

    for parent in (tmp_path, *tmp_path.parents):
        try:
            if parent.stat().st_mode & 0o755 != 0o755:
                parent.chmod(parent.stat().st_mode | 0o055)
        except (PermissionError, OSError):
            break
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path))
    monkeypatch.setattr(settings, "api_key_sha256", TEST_KEY_SHA256)
    return tmp_path


@pytest.fixture()
def client(ws):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers["X-Sandbox-Key"] = TEST_KEY
        yield c
