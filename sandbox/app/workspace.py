"""Session workspace path resolution, ownership, and quotas.

Every path that reaches the filesystem goes through ``resolve_session_dir`` or
``resolve_file`` — both resolve symlinks and verify the result is still inside
the workspace root (the same reject-then-verify shape as the pipeline's
``_validate_ingest_url`` SSRF guard). Identifiers are validated against a
strict pattern before they ever touch a path.

Ownership (hyp-90f.1): on a hardened deploy (service is container-root) each
session directory is owned by a kernel uid of its own, allocated from the
persistent counter ``{root}/_meta/uid_counter``. The executor launches the
session's child as that uid, so the kernel's discretionary access control
denies every cross-session and cross-tenant read even when the attacker knows
the exact path. No passwd entries, no lookup table: the directory's ``st_uid``
*is* the session identity.

Layout under the root (0755):

    {org_id}/                 root-owned 0711  traversable, not listable
    {org_id}/{conversation}/  uid_N-owned 0700 the session's private tree
    _jobs/                    root-owned 0700  job state (jobs.py)
    _meta/                    root-owned 0700  uid counter + startup probe
    _shared/                  root-owned 0755  read-only data for all sessions
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# Mirrors pipeline's _ORG_ID_PATTERN spirit: lowercase slug segments. The
# leading-character rule is what keeps ``_jobs``/``_meta``/``_shared``
# unaddressable through the API.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Relative file paths inside a session: no leading slash, no drive letters,
# each segment a conservative token, no traversal.
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_SEGMENTS = 8

_META_DIR = "_meta"
_SHARED_DIR = "_shared"
_COUNTER_FILE = "uid_counter"
_COUNTER_LOCK = "uid_counter.lock"

# Serialises allocation and migration across the event loop and the exec
# worker threads (re-entrant: migration allocates while holding it).
# fcntl.flock below covers the (currently theoretical) multi-process case.
_uid_lock = threading.RLock()


class WorkspaceError(ValueError):
    """Raised for any invalid identifier, path, or quota violation."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _validate_id(value: str, kind: str) -> str:
    if not _ID_PATTERN.match(value or ""):
        raise WorkspaceError(f"invalid {kind}")
    return value


def workspace_root() -> Path:
    return Path(settings.workspace_root).resolve()


def meta_dir() -> Path:
    return workspace_root() / _META_DIR


def shared_dir() -> Path:
    return workspace_root() / _SHARED_DIR


def ensure_service_dirs() -> None:
    """Create ``_meta`` (0700) and ``_shared`` (0755) under the root.

    Called from the FastAPI lifespan; idempotent and re-asserts modes so a
    drifted volume heals on boot. Ownership is whoever the service runs as
    (root on the hardened deploy), which is the point: no session uid owns
    them, so the kernel denies writes from every child.
    """
    root = workspace_root()
    if not root.is_dir():
        logger.warning("workspace root %s missing; service dirs not created", root)
        return
    for name, mode in ((_META_DIR, 0o700), (_SHARED_DIR, 0o755)):
        d = root / name
        d.mkdir(exist_ok=True)
        d.chmod(mode)


# ------------------------------------------------------------ uid layer ----


def _is_root() -> bool:
    return os.geteuid() == 0


def _scan_max_session_uid(root: Path) -> int:
    """Highest ``st_uid`` over ``{root}/*/*`` skipping ``_*`` service trees.

    Only consulted when the counter file is missing (fresh volume, restore
    from backup). Seeding from disk means a lost counter can never re-issue a
    uid that still owns a live session.
    """
    highest = 0
    try:
        orgs = [p for p in os.scandir(root) if p.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return 0
    for org in orgs:
        if org.name.startswith("_"):
            continue
        try:
            for session in os.scandir(org.path):
                highest = max(highest, session.stat(follow_symlinks=False).st_uid)
        except OSError:
            continue
    return highest


def allocate_uid() -> int:
    """Hand out the next session uid. Monotonic, persistent, never recycled.

    The counter file holds the *next* value to issue. Locking: the module
    lock serialises threads in this process; ``flock`` on a fresh fd per call
    (flock is per open-file-description) serialises processes. The write is
    tmp + fsync + ``os.replace`` so a crash mid-write leaves the old value,
    which is safe: re-issuing the same value twice is exactly what the lock
    prevents, and a stale-but-lower counter only wastes ids on the next scan.
    """
    base = settings.uid_base
    meta = meta_dir()
    counter = meta / _COUNTER_FILE
    with _uid_lock:
        meta.mkdir(exist_ok=True)
        meta.chmod(0o700)
        lock_fd = os.open(meta / _COUNTER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                current = int(counter.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError):
                seeded = _scan_max_session_uid(meta.parent) + 1
                current = max(base, seeded)
                logger.info("uid counter missing; seeded at %d", current)
            # A counter below the base would re-issue system uids.
            uid = max(current, base)
            tmp = meta / (_COUNTER_FILE + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"{uid + 1}\n".encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, counter)
            dir_fd = os.open(meta, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            os.close(lock_fd)  # closing releases the flock
    return uid


def _chown_tree(top: Path, uid: int, gid: int) -> None:
    """Re-own ``top`` and everything below it without following symlinks.

    A child may have planted symlinks pointing anywhere (``_jobs``, ``/etc``).
    ``os.walk(followlinks=False)`` never descends through them and
    ``os.lchown`` re-owns the link itself, never its target.
    """
    os.lchown(top, uid, gid)
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        for name in (*dirnames, *filenames):
            os.lchown(os.path.join(dirpath, name), uid, gid)


def session_identity(session: Path) -> tuple[int, int] | None:
    """``(uid, gid)`` that owns ``session``, or ``None`` when the service is
    not root (dev mode: everything runs as the developer's uid)."""
    if not _is_root():
        return None
    st = os.lstat(session)
    return st.st_uid, st.st_gid


def own_by_session(session: Path, target: Path) -> None:
    """Give ``target`` (and any directories between it and ``session``) to the
    session's uid. Used after the service writes into a session on the
    session's behalf (``put_file``, the matplotlib config dir) so the child can
    read *and* modify what was written for it."""
    identity = session_identity(session)
    if identity is None:
        return
    uid, gid = identity
    path = target
    while True:
        os.lchown(path, uid, gid)
        if path.parent == session or path.parent == path:
            break
        path = path.parent


def _harden_session_ownership(org_dir: Path, session: Path) -> None:
    """On a hardened deploy, make ``session`` the private tree of its own uid.

    Runs on EVERY ``resolve_session_dir(create=True)`` — not only first
    creation — so it doubles as the lazy migration: a session dir whose owner
    is below ``uid_base`` (root, the retired shared ``runner`` uid, a drifted
    image uid, anything not ours) gets a fresh uid and a recursive re-own,
    once, the next time it is touched. No boot sweep: the volume only grows,
    and most old sessions are never touched again. Modes are re-asserted
    unconditionally so a drifted tree self-heals.

    Fail closed: if the chown is refused (e.g. a user namespace whose uid map
    does not cover ``uid_base``), the caller gets a 503 and the executor never
    launches a child for this directory as root.
    """
    if not _is_root():
        return
    base = settings.uid_base
    try:
        org_dir.chmod(0o711)
        # Stat + allocate + chown under one lock: two callers racing on the
        # same fresh dir must not both allocate and interleave their chowns.
        with _uid_lock:
            st = os.lstat(session)
            if st.st_uid < base or st.st_gid != st.st_uid:
                uid = allocate_uid()
                logger.info(
                    "session %s: uid %d -> per-session uid %d", session, st.st_uid, uid
                )
                _chown_tree(session, uid, uid)
        session.chmod(0o700)
    except OSError as exc:
        logger.error(
            "cannot own session dir %s for a per-session uid (%s); refusing the "
            "session rather than running unisolated",
            session,
            exc,
        )
        raise WorkspaceError("sandbox isolation unavailable", status_code=503) from exc


def resolve_session_dir(org_id: str, conversation_id: str, *, create: bool = False) -> Path:
    """Return the session directory, confined to the workspace root."""
    _validate_id(org_id, "org_id")
    _validate_id(conversation_id, "conversation_id")
    root = workspace_root()
    session = (root / org_id / conversation_id).resolve()
    # Belt and braces: ids are already slug-validated, but re-verify prefix
    # after resolution so a symlinked segment cannot escape.
    if not session.is_relative_to(root):
        raise WorkspaceError("session path escapes workspace root", status_code=400)
    if create:
        try:
            session.mkdir(parents=True)
        except FileExistsError:
            pass  # another caller won the race; ownership is checked below
        _harden_session_ownership(session.parent, session)
    elif not session.is_dir():
        raise WorkspaceError("session not found", status_code=404)
    return session


def validate_relative_path(rel_path: str) -> str:
    """Validate a session-relative file path (no traversal, bounded depth)."""
    if not rel_path or rel_path.startswith(("/", "\\")):
        raise WorkspaceError("path must be relative")
    segments = rel_path.split("/")
    if len(segments) > _MAX_SEGMENTS:
        raise WorkspaceError("path too deep")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise WorkspaceError("path traversal rejected")
        if not _SEGMENT_PATTERN.match(segment):
            raise WorkspaceError("invalid path segment")
    return rel_path


def resolve_file(
    org_id: str, conversation_id: str, rel_path: str, *, for_write: bool = False
) -> Path:
    """Resolve a file inside a session, verified against escapes."""
    session = resolve_session_dir(org_id, conversation_id, create=for_write)
    validate_relative_path(rel_path)
    target = (session / rel_path).resolve()
    if not target.is_relative_to(session):
        raise WorkspaceError("file path escapes session", status_code=400)
    return target


@dataclass(frozen=True)
class SessionUsage:
    total_bytes: int
    file_count: int


def session_usage(session: Path) -> SessionUsage:
    total = 0
    count = 0
    for p in session.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
            count += 1
    return SessionUsage(total_bytes=total, file_count=count)


def enforce_write_quota(session: Path, incoming_bytes: int) -> None:
    usage = session_usage(session)
    if usage.file_count + 1 > settings.session_max_files:
        raise WorkspaceError("session file-count quota exceeded", status_code=413)
    if usage.total_bytes + incoming_bytes > settings.session_max_bytes:
        raise WorkspaceError("session byte quota exceeded", status_code=413)


def list_session_files(session: Path) -> list[dict]:
    """Non-recursive-unbounded listing: files with size and mtime, sorted."""
    entries = []
    for p in sorted(session.rglob("*")):
        if p.is_file() and not p.is_symlink():
            stat = p.stat()
            entries.append(
                {
                    "path": str(p.relative_to(session)),
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        if len(entries) >= settings.session_max_files:
            break
    return entries
