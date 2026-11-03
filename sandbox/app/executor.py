"""Rlimited subprocess execution of model-authored Python and, behind the
``harness:pi`` entrypoint (hyp-90f.2 spike), a third-party agent harness.

Isolation layers, in order:
1. Service-level: this container holds no credentials of any kind, except the
   one LLM key the unix-socket proxy spends on a child's behalf (hyp-90f.2;
   see app/llm_proxy.py and config.py). It never enters a child environment.
2. One kernel uid per session (hyp-90f.1): when the service runs as
   container-root (the hardened deploy), each session directory is owned by a
   uid of its own (allocated in workspace.py) and the child executes as that
   uid via ``Popen(user=uid, group=gid, extra_groups=[])``. Every other
   session's tree (mode 0700, different owner) and all service-owned state
   (``_jobs``, ``_meta`` at 0700) is then denied by the kernel's DAC — even to
   a child that knows the exact path. ``_shared`` (0755) is the one tree every
   session may read. Fail closed: a session dir that is not owned by a
   per-session uid never launches a child as root. When the service itself
   runs unprivileged (local dev, tests), this degrades to same-uid execution
   with a warning and ``/ready`` reports ``per_session_uid: false``.
3. Process-level: ``resource.setrlimit`` caps CPU, memory, processes, file
   size, and open files; the child gets a scrubbed environment and runs with
   ``python3 -I`` (isolated mode: no user site-packages, no PYTHONPATH).
   ``RLIMIT_NPROC`` counts per uid, so with one uid per session it is a real
   per-session cap and is always on under root. The harness runs under its
   own ``Limits`` (Node-shaped: RLIMIT_DATA caps real memory because V8
   reserves multi-GB PROT_NONE regions that RLIMIT_AS would count).
4. Seccomp network block: a kernel-enforced filter denies ``socket(AF_INET)``
   and ``socket(AF_INET6)`` in the child, so executed code cannot open a TCP or
   UDP connection. AF_UNIX is left allowed (CPython internals use it; the
   harness reaches its LLM proxy over it). The filter needs no capability,
   is inherited across ``execve``, and cannot be lifted from inside the
   sandboxed process.
5. Best-effort network namespace: we also try ``unshare -rn``. Railway does not
   grant ``CAP_SYS_ADMIN`` so this degrades to a log line there; seccomp is the
   layer that actually holds. Railway offers no egress control of its own
   (verified 2026-08-27) — see docs/DEPLOY.md.

Trade-off note: the service runs as root *inside the container* so it can
deprivilege children — confining the untrusted half (model code) is worth more
than deprivileging the trusted half (our ~500-line service with no secrets).
This is process isolation, not a microVM. The multi-tenant gate is tracked in
beads (security-gate ticket) — see docs/harness-audit.md §5.
"""

from __future__ import annotations

import logging
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from app import llm_proxy, workspace
from app.config import settings

logger = logging.getLogger(__name__)

_SCRIPT_NAME = "_exec_script.py"
_PI_DIR = ".pi"

# Resolved once at import: whether `unshare -rn true` works here.
_UNSHARE: list[str] | None = None


class IsolationError(RuntimeError):
    """The child cannot be launched under a per-session uid. Never fall back
    to running it as root."""


class HarnessError(RuntimeError):
    """The harness entrypoint cannot run (proxy disabled, runtime missing,
    session already running a harness job)."""


def child_identity(session_dir: Path) -> tuple[int, int] | None:
    """``(uid, gid)`` the child must run as, or ``None`` in dev mode (service
    not root: the child inherits the service uid).

    Source of truth is the session directory's ``st_uid``: no lookup table,
    no passwd. Under root, a directory owned below ``uid_base`` means the
    workspace layer never re-owned it (or could not: user-namespace uid map
    too small) — refuse rather than execute model code as root.
    """
    identity = workspace.session_identity(session_dir)
    if identity is None:
        return None
    uid, gid = identity
    if uid < settings.uid_base or gid != uid:
        raise IsolationError(
            f"session dir {session_dir} owned by {uid}:{gid}, not a per-session uid "
            f"(>= {settings.uid_base}); refusing to launch a root child"
        )
    return uid, gid


def _uid_mapped(uid: int) -> bool:
    """Whether ``uid`` is inside this process's user-namespace uid map.

    ``/proc/self/uid_map`` lines are ``inside outside count``. Absent (not
    Linux) counts as mapped: the chown probe is the authoritative check.
    """
    try:
        lines = Path("/proc/self/uid_map").read_text().splitlines()
    except OSError:
        return True
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        inside, _outside, count = (int(x) for x in parts)
        if inside <= uid < inside + count:
            return True
    return False


def probe_per_session_uid(meta: Path) -> bool:
    """Startup probe behind ``/ready.per_session_uid``.

    Chowns a directory under ``_meta`` to ``uid_base``, reads it back, and
    checks the uid map covers it. This surfaces the Railway risk — a user
    namespace with a limited uid map makes ``chown`` fail with EINVAL — before
    any session is touched. A ``False`` here means execs will be refused
    (fail closed), never that they run unisolated.
    """
    if os.geteuid() != 0:
        logger.warning("service not root; children share the service uid (dev mode)")
        return False
    base = settings.uid_base
    probe = meta / "_probe"
    try:
        probe.mkdir(exist_ok=True)
        probe.chmod(0o700)
        os.chown(probe, base, base)
        st = os.lstat(probe)
        if (st.st_uid, st.st_gid) != (base, base):
            raise OSError(f"chown did not stick: got {st.st_uid}:{st.st_gid}")
        if not _uid_mapped(base):
            raise OSError(f"uid {base} not covered by /proc/self/uid_map")
    except OSError as exc:
        logger.error(
            "per-session uid probe FAILED (%s): this container cannot own files as "
            "uid %d; every exec will be refused until the uid map or image is fixed",
            exc,
            base,
        )
        return False
    logger.info("per-session uid isolation active (base uid %d)", base)
    return True


def _detect_unshare() -> list[str] | None:
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    try:
        probe = subprocess.run(
            [unshare, "-rn", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - any probe failure means "unavailable"
        return None
    if probe.returncode == 0:
        return [unshare, "-rn"]
    return None


def unshare_prefix() -> list[str]:
    global _UNSHARE
    if _UNSHARE is None:
        detected = _detect_unshare()
        if detected is None:
            logger.warning(
                "unshare -rn unavailable; relying on service-level egress cut only"
            )
            _UNSHARE = []
        else:
            logger.info("network namespace isolation active (unshare -rn)")
            _UNSHARE = detected
    return list(_UNSHARE)


def _clean_env(session_dir: Path) -> dict[str, str]:
    """Environment for the child: no secrets, no inherited config.

    The ``*_NUM_THREADS=1`` pins are load-bearing, not tuning. OpenBLAS (via
    numpy/pandas) reserves per-core thread buffers at import time sized to the
    HOST's core count — 48 on Railway. Against ``RLIMIT_AS`` (512 MB) that
    import aborts with "OpenBLAS error: Memory allocation still failed after
    10 retries", so ``import numpy`` fails outright. Single-threaded is also
    the right posture for a sandbox that already caps CPU seconds.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(session_dir),
        "TMPDIR": str(session_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": str(session_dir / ".mpl"),
        "MPLBACKEND": "Agg",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def _set_limit(kind: int, value: int) -> None:
    """Lower one rlimit, clamped to the current hard cap; skip when the host
    kernel refuses (macOS dev boxes reject some limits Linux accepts). On the
    production image (Linux) every limit applies — the Linux-only tests assert
    that."""
    try:
        _soft, hard = resource.getrlimit(kind)
        capped = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (capped, capped))
    except (OSError, ValueError):
        pass


def _seccomp_available() -> bool:
    try:
        import pyseccomp  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_SECCOMP_OK: bool | None = None


def seccomp_active() -> bool:
    """Whether the child network block can be installed here."""
    global _SECCOMP_OK
    if _SECCOMP_OK is None:
        _SECCOMP_OK = _seccomp_available()
        if _SECCOMP_OK:
            logger.info("seccomp network block active (AF_INET/AF_INET6 denied)")
        else:
            logger.error(
                "pyseccomp unavailable — executed code CAN reach the network; "
                "do not enable the sandbox in this deployment"
            )
    return _SECCOMP_OK


def _install_network_block() -> None:
    """Deny IP sockets in the calling process. Runs in the child, pre-exec.

    Default-allow with two targeted denies: a default-deny filter would have to
    enumerate every syscall CPython needs. ``socket()`` is the single chokepoint
    for creating an IP socket, so filtering on its address-family argument
    blocks connect/sendto/etc. transitively without touching AF_UNIX, which
    CPython uses internally.

    EAFNOSUPPORT (not KILL) so the child sees a normal Python exception it can
    report, rather than dying with an opaque signal.
    """
    import errno
    import socket as _socket

    import pyseccomp as seccomp

    flt = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for family in (_socket.AF_INET, _socket.AF_INET6):
        flt.add_rule(
            seccomp.ERRNO(errno.EAFNOSUPPORT),
            "socket",
            seccomp.Arg(0, seccomp.EQ, int(family)),
        )
    flt.load()


# ---------------------------------------------------------------- limits ----


@dataclass(frozen=True)
class Limits:
    """Per-child rlimits. ``data_bytes`` (RLIMIT_DATA) is ``None`` for
    CPython, whose allocations RLIMIT_AS already bounds."""

    cpu_seconds: int
    memory_bytes: int | None  # RLIMIT_AS; None leaves it unset (see config)
    data_bytes: int | None  # RLIMIT_DATA
    max_processes: int
    max_file_bytes: int
    max_open_files: int


def python_limits() -> Limits:
    return Limits(
        cpu_seconds=settings.exec_cpu_seconds,
        memory_bytes=settings.exec_memory_bytes,
        data_bytes=None,
        max_processes=settings.exec_max_processes,
        max_file_bytes=settings.exec_max_file_bytes,
        max_open_files=settings.exec_max_open_files,
    )


def harness_limits() -> Limits:
    """Node-shaped: no AS cap (V8's wasm guard regions are ~10 GB each), DATA
    as the real memory cap, NPROC for pi's bash tool, NOFILE for its bundle
    loading. Rationale and measurements in config.py."""
    return Limits(
        cpu_seconds=settings.harness_cpu_seconds,
        memory_bytes=settings.harness_memory_bytes,
        data_bytes=settings.harness_data_bytes,
        max_processes=settings.harness_max_processes,
        max_file_bytes=settings.exec_max_file_bytes,
        max_open_files=settings.harness_max_open_files,
    )


def _make_preexec(limit_nproc: bool, install_netblock: bool, limits: Limits):
    """Build the child preexec. RLIMIT_NPROC counts processes per *uid*, so it
    is only meaningful (and only safe) when the child has a uid of its own —
    under a shared dev uid it would count the developer's unrelated processes
    and break every fork. CPython applies ``user``/``group``/``extra_groups``
    before ``preexec_fn``, so everything here already runs deprivileged."""

    def _rlimit_preexec() -> None:
        # Runs in the child between fork and exec. Keep it async-signal-safe.
        os.setsid()  # own process group so SIGKILL reaps grandchildren
        _set_limit(resource.RLIMIT_CPU, limits.cpu_seconds)
        if limits.memory_bytes is not None:
            _set_limit(resource.RLIMIT_AS, limits.memory_bytes)
        if limits.data_bytes is not None and hasattr(resource, "RLIMIT_DATA"):
            _set_limit(resource.RLIMIT_DATA, limits.data_bytes)
        if limit_nproc:
            _set_limit(resource.RLIMIT_NPROC, limits.max_processes)
        _set_limit(resource.RLIMIT_FSIZE, limits.max_file_bytes)
        _set_limit(resource.RLIMIT_NOFILE, limits.max_open_files)
        _set_limit(resource.RLIMIT_CORE, 0)
        if install_netblock:
            # Last: an exception here propagates as SubprocessError and fails
            # the job closed rather than running unsandboxed.
            _install_network_block()

    return _rlimit_preexec


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    files_created: list[str]


@dataclass(frozen=True)
class HarnessResult(ExecResult):
    llm_usage: dict


def _snapshot_files(session_dir: Path) -> set[str]:
    return {
        str(p.relative_to(session_dir))
        for p in session_dir.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def _cap(data: bytes) -> str:
    cap = settings.exec_output_cap_bytes
    text = data[:cap].decode("utf-8", errors="replace")
    if len(data) > cap:
        text += "\n... [output truncated]"
    return text


def _write_script(script_path: Path, code: str) -> None:
    """Write the script into a directory the *child* owns, without ever
    following a symlink the child may have left under that name (a root
    service following it would turn the next exec into a root write
    anywhere). Remove whatever is there, then create exclusively."""
    try:
        script_path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(script_path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code)


def _own_private_dir(session_dir: Path, name: str, identity: tuple[int, int] | None) -> Path:
    """Create ``session_dir/name`` for the child (0700, session-owned)."""
    d = session_dir / name
    d.mkdir(exist_ok=True)
    if identity is not None:
        os.lchown(d, *identity)
        d.chmod(0o700)
    return d


# Jobs in flight per session uid. A uid is per *session*, not per job, and
# the pipeline may run several execs of one conversation concurrently, so the
# uid-wide reap below only fires when the last job of that uid finishes.
_inflight: dict[int, int] = {}
_inflight_lock = threading.Lock()


def _inflight_enter(uid: int) -> None:
    with _inflight_lock:
        _inflight[uid] = _inflight.get(uid, 0) + 1


def _inflight_leave(uid: int) -> bool:
    """Decrement; True when this was the last in-flight job for ``uid``."""
    with _inflight_lock:
        left = _inflight.get(uid, 1) - 1
        if left <= 0:
            _inflight.pop(uid, None)
            return True
        _inflight[uid] = left
        return False


def _reap_uid(uid: int) -> None:
    """SIGKILL every remaining process of ``uid``. Catches double-fork escapes
    from the process-group kill (a grandchild that called setsid). Linux only:
    ``/proc/<pid>`` is owned by the process's uid."""
    if sys.platform != "linux":
        return
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
            os.kill(int(entry.name), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            continue


_HIDDEN_PREFIXES = (".mpl", _PI_DIR)


def run_process(
    session_dir: Path,
    argv: list[str],
    env: dict[str, str],
    limits: Limits,
    timeout_s: float,
) -> ExecResult:
    """Launch ``argv`` inside ``session_dir`` under the full isolation stack
    (identity drop, rlimits, scrubbed env, seccomp, best-effort netns).
    Blocking; call via a worker. Entry points wrap this with their own argv,
    env, and limits."""
    identity = child_identity(session_dir)
    before = _snapshot_files(session_dir)

    popen_kwargs: dict = {}
    if identity is not None:
        uid, gid = identity
        # extra_groups=[] is load-bearing: without it the child keeps root's
        # supplementary group 0 (CPython 3.12.0 silently skipped setgroups for
        # an empty list, CVE-2023-6507; the image pins 3.11 and the in-image
        # test asserts getgroups()==[]). umask 077 keeps the child's own files
        # private to its uid (the root service reads them regardless).
        popen_kwargs.update(user=uid, group=gid, extra_groups=[], umask=0o077)
        _inflight_enter(uid)

    timed_out = False
    try:
        proc = subprocess.Popen(
            [*unshare_prefix(), *argv],
            cwd=session_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            # preexec runs only async-signal-safe work (setsid, setrlimit) plus
            # the seccomp load, which must happen post-fork to bind to this
            # child.
            preexec_fn=_make_preexec(  # noqa: PLW1509
                limit_nproc=identity is not None,
                install_netblock=seccomp_active(),
                limits=limits,
            ),
            start_new_session=False,  # setsid happens in preexec
            **popen_kwargs,
        )
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
    finally:
        if identity is not None and _inflight_leave(identity[0]):
            _reap_uid(identity[0])

    after = _snapshot_files(session_dir)
    created = sorted(f for f in (after - before) if not f.startswith(_HIDDEN_PREFIXES))
    return ExecResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=_cap(stdout),
        stderr=_cap(stderr),
        timed_out=timed_out,
        files_created=created,
    )


def run_code(session_dir: Path, code: str, timeout_s: float) -> ExecResult:
    """Execute Python ``code`` inside ``session_dir``. Blocking; call via a worker."""
    timeout_s = min(max(timeout_s, 1.0), settings.exec_timeout_max_s)
    identity = child_identity(session_dir)  # raises before anything is written
    script_path = session_dir / _SCRIPT_NAME
    _write_script(script_path, code)
    mpl_dir = session_dir / ".mpl"
    mpl_dir.mkdir(exist_ok=True)
    if identity is not None:
        os.lchown(mpl_dir, *identity)
    try:
        return run_process(
            session_dir,
            ["python3", "-I", _SCRIPT_NAME],
            _clean_env(session_dir),
            python_limits(),
            timeout_s,
        )
    finally:
        script_path.unlink(missing_ok=True)


# --------------------------------------------------------------- harness ----


def harness_env(session_dir: Path) -> dict[str, str]:
    """``_clean_env`` plus pi's knobs. No secret: ``OPENROUTER_API_KEY`` is a
    placeholder the proxy strips and replaces."""
    env = _clean_env(session_dir)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(session_dir / _PI_DIR),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
            "OPENROUTER_API_KEY": "sandbox-proxy",
            "LLM_PROXY_SOCKET": settings.llm_proxy_socket,
            "NODE_OPTIONS": f"--import={settings.harness_shim} --max-old-space-size=768",
            "NO_COLOR": "1",
        }
    )
    return env


def harness_argv(model: str, task: str) -> list[str]:
    return [
        "node",
        settings.harness_pi_cli,
        "-p",
        "--no-session",
        "--no-extensions",
        "--model",
        f"openrouter/{model}",
        "--",
        task,
    ]


def run_harness(
    session_dir: Path, task: str, model: str, job_id: str, timeout_s: float
) -> HarnessResult:
    """Run pi on ``task`` inside ``session_dir`` as the session uid, with the
    LLM proxy as its only egress. Blocking; call via a worker."""
    timeout_s = min(max(timeout_s, 1.0), settings.harness_timeout_s)
    if not settings.llm_proxy_socket:
        raise HarnessError("LLM proxy disabled (SANDBOX_LLM_PROXY_SOCKET empty)")
    if not Path(settings.harness_pi_cli).is_file():
        raise HarnessError(f"harness runtime missing: {settings.harness_pi_cli}")
    identity = child_identity(session_dir)  # raises before anything is written
    uid = identity[0] if identity is not None else os.geteuid()
    _own_private_dir(session_dir, _PI_DIR, identity)
    try:
        budget = llm_proxy.register_job(uid, job_id)
    except llm_proxy.ProxyError as exc:
        raise HarnessError(str(exc)) from exc
    try:
        result = run_process(
            session_dir,
            harness_argv(model, task),
            harness_env(session_dir),
            harness_limits(),
            timeout_s,
        )
    finally:
        llm_proxy.unregister_job(uid)
    return HarnessResult(**asdict(result), llm_usage=budget.as_dict())
