import pytest

from app.workspace import (
    WorkspaceError,
    enforce_write_quota,
    resolve_file,
    resolve_session_dir,
    validate_relative_path,
)


class TestIds:
    def test_valid_session_created(self, ws):
        session = resolve_session_dir("org1", "conv-abc123", create=True)
        assert session.is_dir()
        assert session == ws / "org1" / "conv-abc123"

    @pytest.mark.parametrize(
        "bad", ["", "..", "UPPER", "a/b", "a b", ".hidden", "_jobs", "-lead", "x" * 65]
    )
    def test_bad_org_rejected(self, ws, bad):
        with pytest.raises(WorkspaceError):
            resolve_session_dir(bad, "conv1", create=True)

    def test_jobs_dir_unaddressable_via_ids(self, ws):
        with pytest.raises(WorkspaceError):
            resolve_session_dir("_jobs", "org1", create=True)

    def test_missing_session_404(self, ws):
        with pytest.raises(WorkspaceError) as exc:
            resolve_session_dir("org1", "nope")
        assert exc.value.status_code == 404


class TestPaths:
    @pytest.mark.parametrize(
        "bad",
        ["../x", "a/../../x", "/etc/passwd", "a//b", ".", "..", "a/./b", "\\x", ".env"],
    )
    def test_traversal_rejected(self, bad):
        with pytest.raises(WorkspaceError):
            validate_relative_path(bad)

    def test_depth_capped(self):
        with pytest.raises(WorkspaceError):
            validate_relative_path("/".join(["d"] * 9))

    def test_good_paths(self):
        for ok in ("data.csv", "tools/1-search.json", "out/plot.png"):
            assert validate_relative_path(ok) == ok

    def test_symlink_escape_blocked(self, ws):
        session = resolve_session_dir("org1", "conv1", create=True)
        outside = ws.parent / "outside.txt"
        outside.write_text("secret")
        (session / "link").symlink_to(outside)
        with pytest.raises(WorkspaceError):
            resolve_file("org1", "conv1", "link")


class TestQuota:
    def test_byte_quota(self, ws, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "session_max_bytes", 100)
        session = resolve_session_dir("org1", "conv1", create=True)
        (session / "a.txt").write_bytes(b"x" * 90)
        with pytest.raises(WorkspaceError) as exc:
            enforce_write_quota(session, 20)
        assert exc.value.status_code == 413

    def test_file_count_quota(self, ws, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "session_max_files", 2)
        session = resolve_session_dir("org1", "conv1", create=True)
        (session / "a").write_text("1")
        (session / "b").write_text("2")
        with pytest.raises(WorkspaceError):
            enforce_write_quota(session, 1)


# ------------------------------------------------------------ uid layer ----
#
# Contract (hyp-90f.1): every session directory is owned by a kernel uid of
# its own, allocated from a persistent monotonic counter under
# ``{root}/_meta``. Allocation itself needs no privilege, so it is tested
# everywhere; ownership checks need root on Linux.

import os
import stat
import sys
import threading

from app.config import settings
from app.workspace import allocate_uid, ensure_service_dirs, meta_dir, shared_dir

_BASE = settings.uid_base


class TestUidAllocator:
    def test_first_allocation_starts_at_base(self, ws):
        assert allocate_uid() == _BASE
        assert allocate_uid() == _BASE + 1

    def test_meta_dir_is_private(self, ws):
        allocate_uid()
        st = meta_dir().stat()
        assert stat.S_IMODE(st.st_mode) == 0o700
        assert (meta_dir() / "uid_counter").is_file()

    def test_unique_and_monotonic_under_threads(self, ws):
        got: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            mine = [allocate_uid() for _ in range(50)]
            with lock:
                got.extend(mine)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(got) == 400
        assert len(set(got)) == 400, "duplicate uid issued under concurrency"
        assert set(got) == set(range(_BASE, _BASE + 400))
        # The counter on disk is the next value to issue: the allocator
        # resumes from the file, never from memory.
        assert int((meta_dir() / "uid_counter").read_text()) == _BASE + 400
        assert allocate_uid() == _BASE + 400

    def test_resumes_from_existing_counter(self, ws):
        meta = ws / "_meta"
        meta.mkdir()
        (meta / "uid_counter").write_text(f"{_BASE + 42}\n")
        assert allocate_uid() == _BASE + 42
        assert allocate_uid() == _BASE + 43

    def test_counter_never_goes_below_base(self, ws):
        # A corrupted/legacy counter below the base must not re-issue a
        # system uid range.
        meta = ws / "_meta"
        meta.mkdir()
        (meta / "uid_counter").write_text("999\n")
        assert allocate_uid() == _BASE

    def test_reinit_ignores_legacy_owned_dirs(self, ws):
        # Dirs owned by uids below the base (root, the old `runner`, the dev
        # user) do not move the counter: those are exactly the ones migration
        # will re-own.
        (ws / "org1" / "conv-a").mkdir(parents=True)
        (ws / "org1" / "conv-b").mkdir(parents=True)
        assert allocate_uid() == _BASE

    @pytest.mark.root_linux
    def test_reinit_from_existing_session_dirs(self, ws):
        # Lost counter (fresh volume, restore from backup): re-seed from the
        # highest session uid on disk so a live uid can never be re-issued.
        a = ws / "org1" / "conv-a"
        a.mkdir(parents=True)
        os.chown(a, _BASE + 7, _BASE + 7)
        b = ws / "org2" / "conv-b"
        b.mkdir(parents=True)
        os.chown(b, _BASE + 3, _BASE + 3)
        # Service-owned trees are skipped even if something odd owns them.
        j = ws / "_jobs" / "org1"
        j.mkdir(parents=True)
        os.chown(j, _BASE + 900, _BASE + 900)
        assert allocate_uid() == _BASE + 8


class TestServiceDirs:
    def test_ensure_service_dirs_modes(self, ws):
        ensure_service_dirs()
        assert stat.S_IMODE(meta_dir().stat().st_mode) == 0o700
        assert stat.S_IMODE(shared_dir().stat().st_mode) == 0o755
        # Idempotent and re-asserts modes.
        shared_dir().chmod(0o700)
        ensure_service_dirs()
        assert stat.S_IMODE(shared_dir().stat().st_mode) == 0o755

    def test_service_dirs_unaddressable_as_ids(self, ws):
        for name in ("_meta", "_shared"):
            with pytest.raises(WorkspaceError):
                resolve_session_dir(name, "x", create=True)


@pytest.mark.root_linux
class TestSessionOwnership:
    def test_two_sessions_distinct_uids_stable(self, ws):
        a = resolve_session_dir("org1", "conv-a", create=True)
        b = resolve_session_dir("org1", "conv-b", create=True)
        sa, sb = os.lstat(a), os.lstat(b)
        assert sa.st_uid >= _BASE and sb.st_uid >= _BASE
        assert sa.st_uid != sb.st_uid
        assert sa.st_gid == sa.st_uid and sb.st_gid == sb.st_uid
        assert stat.S_IMODE(sa.st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(a.parent).st_mode) == 0o711
        assert os.lstat(a.parent).st_uid == 0
        # Later resolves (every exec, every put_file) keep the identity.
        for _ in range(3):
            assert os.lstat(resolve_session_dir("org1", "conv-a", create=True)).st_uid == sa.st_uid
            assert os.lstat(resolve_session_dir("org1", "conv-a")).st_uid == sa.st_uid

    def test_cross_org_sessions_distinct(self, ws):
        a = resolve_session_dir("org1", "conv-a", create=True)
        b = resolve_session_dir("org2", "conv-a", create=True)
        assert os.lstat(a).st_uid != os.lstat(b).st_uid

    def test_legacy_dir_migrated_lazily(self, ws):
        # A session created by the old shared-`runner` build: dir and contents
        # owned by uid 999, plus a symlink a child may have planted.
        legacy = ws / "org1" / "conv-old"
        (legacy / "tools").mkdir(parents=True)
        (legacy / "tools" / "export.json").write_text("{}")
        outside = ws / "_jobs" / "secret.json"
        outside.parent.mkdir()
        outside.write_text("root state")
        (legacy / "escape").symlink_to(outside)
        for p in (legacy, legacy / "tools", legacy / "tools" / "export.json"):
            os.chown(p, 999, 999)
        os.lchown(legacy / "escape", 999, 999)
        os.chmod(legacy, 0o755)

        session = resolve_session_dir("org1", "conv-old", create=True)
        uid = os.lstat(session).st_uid
        assert uid >= _BASE
        assert stat.S_IMODE(os.lstat(session).st_mode) == 0o700
        assert os.lstat(session / "tools").st_uid == uid
        assert os.lstat(session / "tools" / "export.json").st_uid == uid
        # The symlink itself is re-owned; its target is never followed.
        assert os.lstat(session / "escape").st_uid == uid
        assert os.lstat(outside).st_uid == 0
        # Migration happens once: a second resolve keeps the uid.
        assert os.lstat(resolve_session_dir("org1", "conv-old", create=True)).st_uid == uid

    def test_root_owned_dir_migrated_too(self, ws):
        # Any owner below the base is "not ours" — root-owned counts.
        legacy = ws / "org1" / "conv-root"
        legacy.mkdir(parents=True)
        (legacy / "f").write_text("x")
        session = resolve_session_dir("org1", "conv-root", create=True)
        assert os.lstat(session).st_uid >= _BASE
        assert os.lstat(session / "f").st_uid == os.lstat(session).st_uid

    def test_chown_failure_fails_closed(self, ws, monkeypatch):
        # Simulates a user namespace whose uid map does not cover the base
        # (Railway risk): the session must not be handed out unisolated.
        import app.workspace as wsmod

        def boom(*_a, **_k):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr(wsmod.os, "lchown", boom)
        with pytest.raises(WorkspaceError) as exc:
            resolve_session_dir("org1", "conv-x", create=True)
        assert exc.value.status_code == 503


@pytest.mark.skipif(
    os.geteuid() == 0 and sys.platform == "linux", reason="dev-mode behaviour only"
)
def test_non_root_create_leaves_ownership_alone(ws):
    session = resolve_session_dir("org1", "conv-dev", create=True)
    assert os.lstat(session).st_uid == os.geteuid()
    assert not (ws / "_meta" / "uid_counter").exists()
