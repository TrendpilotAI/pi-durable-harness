"""Executor behaviour: limits, timeouts, environment scrubbing.

These run real subprocesses (python3 required on PATH). Rlimit assertions are
kept portable: RLIMIT_AS is not enforceable on macOS, so the memory test only
runs on Linux — CI (Linux) covers it.
"""

import sys

import pytest

from app import executor
from app.workspace import resolve_session_dir


@pytest.fixture()
def session(ws):
    return resolve_session_dir("org1", "conv1", create=True)


def test_happy_path_stdout_and_files(session):
    result = executor.run_code(
        session,
        "print('hello'); open('out.txt','w').write('data')",
        timeout_s=30,
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.files_created == ["out.txt"]
    assert (session / "out.txt").read_text() == "data"


def test_script_file_cleaned_up(session):
    executor.run_code(session, "print(1)", timeout_s=30)
    assert not (session / "_exec_script.py").exists()


def test_nonzero_exit_and_stderr(session):
    result = executor.run_code(session, "import sys; sys.exit(3)", timeout_s=30)
    assert result.exit_code == 3
    result = executor.run_code(session, "raise ValueError('boom')", timeout_s=30)
    assert result.exit_code != 0
    assert "boom" in result.stderr


def test_wallclock_timeout_kills_process_group(session, monkeypatch):
    # Subject under test is the process-group kill, not RLIMIT_NPROC. When the
    # suite runs as root the child drops to its session uid and NPROC is
    # enforced (16), which the grandchild spawn below can trip — masking the
    # timeout assertion with a BlockingIOError. Give this one test headroom so
    # it measures what it claims.
    from app.config import settings

    monkeypatch.setattr(settings, "exec_max_processes", 256)
    result = executor.run_code(
        session,
        "import subprocess,time\n"
        "subprocess.Popen(['sleep','60'])\n"
        "time.sleep(60)",
        timeout_s=2,
    )
    assert result.timed_out is True
    assert result.exit_code is None


def test_env_is_scrubbed(session, monkeypatch):
    monkeypatch.setenv("SANDBOX_API_KEY_SHA256", "deadbeef")
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    result = executor.run_code(
        session,
        "import os, json; print(json.dumps(dict(os.environ)))",
        timeout_s=30,
    )
    assert result.exit_code == 0
    assert "should-not-leak" not in result.stdout
    assert "deadbeef" not in result.stdout
    assert "SANDBOX_API_KEY" not in result.stdout


def test_blas_threads_pinned_in_child_env(session):
    """Regression: numpy/pandas must import under RLIMIT_AS.

    OpenBLAS sizes its per-core buffers from the HOST core count (48 on
    Railway). Unpinned, `import numpy` aborts with "OpenBLAS error: Memory
    allocation still failed" against the 512 MB RLIMIT_AS — which broke the
    first staging deploy. Assert the pins reach the child, not just that some
    import happens to work on this machine's core count.
    """
    result = executor.run_code(
        session,
        "import os\n"
        "for k in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):\n"
        "    print(k, os.environ.get(k))",
        timeout_s=30,
    )
    assert result.exit_code == 0
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        assert f"{key} 1" in result.stdout, f"{key} not pinned to 1 in child env"


@pytest.mark.skipif(
    not executor.seccomp_active(), reason="pyseccomp unavailable (non-Linux dev host)"
)
def test_network_is_blocked_for_executed_code(session):
    """hyp-sxb: executed code must not reach the network.

    Railway grants no CAP_SYS_ADMIN (so `unshare -rn` degrades) and offers no
    egress control, so this seccomp filter is the only layer that holds. The
    workspace holds offloaded tenant graph data, making an open socket an
    exfiltration path.
    """
    result = executor.run_code(
        session,
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=5)\n"
        "    print('NET_OPEN')\n"
        "except OSError as exc:\n"
        "    print('NET_BLOCKED', type(exc).__name__)",
        timeout_s=30,
    )
    assert "NET_OPEN" not in result.stdout, "executed code reached the network"
    assert "NET_BLOCKED" in result.stdout


@pytest.mark.skipif(
    not executor.seccomp_active(), reason="pyseccomp unavailable (non-Linux dev host)"
)
def test_unix_sockets_still_allowed(session):
    """The filter targets AF_INET/AF_INET6 only; CPython internals use AF_UNIX,
    so denying it wholesale would break unrelated stdlib behaviour."""
    result = executor.run_code(
        session,
        "import socket\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.close()\n"
        "print('AF_UNIX ok')",
        timeout_s=30,
    )
    assert result.exit_code == 0
    assert "AF_UNIX ok" in result.stdout


def test_output_capped(session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "exec_output_cap_bytes", 1000)
    result = executor.run_code(session, "print('x' * 100000)", timeout_s=30)
    assert len(result.stdout) < 1200
    assert "[output truncated]" in result.stdout


def test_isolated_mode_no_pythonpath_injection(session, monkeypatch):
    # python3 -I must ignore PYTHONPATH entirely.
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    result = executor.run_code(
        session, "import sys; print('/tmp/evil' in sys.path)", timeout_s=30
    )
    assert "False" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS only enforced on Linux")
def test_memory_limit_kills_allocator(session):
    result = executor.run_code(session, "x = bytearray(10**9)", timeout_s=30)
    assert result.exit_code != 0


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_CPU semantics vary off-Linux")
def test_cpu_limit(session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "exec_cpu_seconds", 1)
    result = executor.run_code(
        session, "while True:\n    pass", timeout_s=30
    )
    assert result.exit_code != 0 or result.timed_out


# ------------------------------------------------------- per-session uid ----
#
# hyp-90f.1: each session's child runs as the uid that owns the session dir,
# so cross-session reads are denied by the kernel (DAC), not by path secrecy.

import os
import stat

from app.config import settings as _settings
from app.workspace import ensure_service_dirs, shared_dir


def _run(session, code, **kw):
    return executor.run_code(session, code, timeout_s=kw.pop("timeout_s", 30))


@pytest.mark.skipif(
    os.geteuid() == 0 and sys.platform == "linux", reason="dev-mode behaviour only"
)
def test_child_identity_none_when_not_root(session):
    assert executor.child_identity(session) is None


def test_script_symlink_planted_by_child_is_not_followed(session, ws):
    # The service (root on prod) writes _exec_script.py into a dir the child
    # owns. A symlink left behind under that name must not turn the next
    # write into a root write elsewhere.
    victim = ws / "victim.txt"
    victim.write_text("untouched")
    (session / "_exec_script.py").symlink_to(victim)
    result = _run(session, "print('ran')")
    assert result.exit_code == 0 and "ran" in result.stdout
    assert victim.read_text() == "untouched"
    assert not (session / "_exec_script.py").exists()


@pytest.mark.root_linux
class TestUidSeparation:
    def test_child_runs_as_session_uid_no_groups_umask_077(self, session):
        # Identity is asserted from OUTSIDE the child via the kernel uid on a
        # file it creates: where `unshare -rn` is available (Docker 27 allows
        # unprivileged user namespaces; Railway does not) the child sees
        # itself as uid 0 inside its namespace while the kernel uid — the one
        # DAC checks against — is still the session's.
        st = os.lstat(session)
        assert st.st_uid >= _settings.uid_base
        result = _run(
            session,
            "import os\n"
            "open('whoami.txt', 'w').write('x')\n"
            "m = os.umask(0)\n"
            "print('inside', os.getuid(), os.getgid(), open('/proc/self/uid_map').read().split())\n"
            "print('groups', os.getgroups())\n"
            "print('umask', oct(m))",
        )
        assert result.exit_code == 0, result.stderr
        created = os.lstat(session / "whoami.txt")
        assert (created.st_uid, created.st_gid) == (st.st_uid, st.st_gid), result.stdout
        assert stat.S_IMODE(created.st_mode) == 0o600, "umask 077 not applied"
        assert "groups []" in result.stdout, result.stdout
        assert "umask 0o77" in result.stdout, result.stdout
        inside = result.stdout.splitlines()[0]
        if executor.unshare_prefix():
            # userns: mapped to 0 inside, session uid outside
            assert inside.startswith("inside 0 0 ") and str(st.st_uid) in inside, inside
        else:
            assert inside.startswith(f"inside {st.st_uid} {st.st_gid} "), inside

    def test_child_identity_matches_dir(self, session):
        st = os.lstat(session)
        assert executor.child_identity(session) == (st.st_uid, st.st_gid)

    def test_child_identity_fails_closed_on_unowned_dir(self, ws):
        # A dir the migration never touched (root-owned) must never launch a
        # root child. This is the guard behind "if chown fails, refuse".
        rogue = ws / "org1" / "rogue"
        rogue.mkdir(parents=True)
        with pytest.raises(executor.IsolationError):
            executor.child_identity(rogue)
        with pytest.raises(executor.IsolationError):
            executor.run_code(rogue, "print(1)", timeout_s=10)

    def test_cross_session_read_denied(self, ws):
        a = resolve_session_dir("org1", "conv-a", create=True)
        b = resolve_session_dir("org1", "conv-b", create=True)
        (b / "secret.txt").write_text("tenant data")
        os.chown(b / "secret.txt", os.lstat(b).st_uid, os.lstat(b).st_gid)
        code = (
            "import os\n"
            f"p = {str(b / 'secret.txt')!r}\n"
            "try:\n"
            "    open(p).read(); print('READ_OK')\n"
            "except PermissionError:\n"
            "    print('READ_DENIED')\n"
            "try:\n"
            "    os.listdir(os.path.dirname(p)); print('LIST_OK')\n"
            "except PermissionError:\n"
            "    print('LIST_DENIED')\n"
            "try:\n"
            "    os.listdir(os.path.dirname(os.path.dirname(p))); print('ORG_LIST_OK')\n"
            "except PermissionError:\n"
            "    print('ORG_LIST_DENIED')\n"
        )
        result = _run(a, code)
        assert result.exit_code == 0, result.stderr
        assert "READ_DENIED" in result.stdout and "READ_OK" not in result.stdout
        assert "LIST_DENIED" in result.stdout
        assert "ORG_LIST_DENIED" in result.stdout

    def test_cross_org_read_denied(self, ws):
        a = resolve_session_dir("org1", "conv-a", create=True)
        b = resolve_session_dir("org2", "conv-a", create=True)
        (b / "secret.txt").write_text("other tenant")
        result = _run(
            a,
            f"try:\n    open({str(b / 'secret.txt')!r}).read(); print('READ_OK')\n"
            "except PermissionError:\n    print('READ_DENIED')",
        )
        assert "READ_DENIED" in result.stdout and "READ_OK" not in result.stdout

    def test_service_state_denied_shared_readable(self, ws, session):
        ensure_service_dirs()
        (shared_dir() / "x.txt").write_text("public")
        (shared_dir() / "x.txt").chmod(0o644)
        jobs = ws / "_jobs"
        jobs.mkdir(exist_ok=True)
        jobs.chmod(0o700)
        (jobs / "j.json").write_text("{}")
        meta_counter = ws / "_meta" / "uid_counter"
        result = _run(
            session,
            f"print('SHARED', open({str(shared_dir() / 'x.txt')!r}).read())\n"
            f"for p in ({str(jobs / 'j.json')!r}, {str(meta_counter)!r}):\n"
            "    try:\n"
            "        open(p).read(); print('LEAK', p)\n"
            "    except PermissionError:\n"
            "        print('DENIED')\n",
        )
        assert result.exit_code == 0, result.stderr
        assert "SHARED public" in result.stdout
        assert "LEAK" not in result.stdout
        assert result.stdout.count("DENIED") == 2

    def test_mpl_config_dir_writable_by_child(self, session):
        result = _run(
            session,
            "import os\n"
            "d = os.environ['MPLCONFIGDIR']\n"
            "open(os.path.join(d, 'probe'), 'w').write('ok')\n"
            "print('MPL_OK')",
        )
        assert result.exit_code == 0, result.stderr
        assert "MPL_OK" in result.stdout
        assert os.lstat(session / ".mpl").st_uid == os.lstat(session).st_uid

    def test_nproc_fork_bomb_blocked(self, session, monkeypatch):
        monkeypatch.setattr(_settings, "exec_max_processes", 8)
        result = _run(
            session,
            "import subprocess\n"
            "procs = []\n"
            "try:\n"
            "    for _ in range(64):\n"
            "        procs.append(subprocess.Popen(['sleep', '30']))\n"
            "    print('UNBOUNDED')\n"
            "except BlockingIOError:\n"
            "    print('NPROC_HIT', len(procs))\n"
            "for p in procs:\n"
            "    p.kill()\n",
            timeout_s=20,
        )
        assert "NPROC_HIT" in result.stdout, (result.stdout, result.stderr)
        assert "UNBOUNDED" not in result.stdout

    def test_escaped_grandchild_reaped_by_uid(self, session, monkeypatch):
        monkeypatch.setattr(_settings, "exec_max_processes", 64)
        result = _run(
            session,
            "import os, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"          # leaves the killpg group
            "    if os.fork() == 0:\n"
            "        time.sleep(120)\n"
            "    os._exit(0)\n"
            "time.sleep(0.5)\n"
            "print('SPAWNED')",
        )
        assert "SPAWNED" in result.stdout
        uid = os.lstat(session).st_uid
        survivors = [
            e.name
            for e in os.scandir("/proc")
            if e.name.isdigit() and e.stat(follow_symlinks=False).st_uid == uid
        ]
        assert survivors == [], f"processes of uid {uid} survived: {survivors}"


def test_reap_waits_for_last_inflight_job_of_uid():
    # Same-session concurrency: the uid-wide reap must not fire while a
    # sibling job of the same session is still running.
    uid = 100_777
    executor._inflight_enter(uid)
    executor._inflight_enter(uid)
    assert executor._inflight_leave(uid) is False
    assert executor._inflight_leave(uid) is True
    assert uid not in executor._inflight
