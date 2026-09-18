"""Cross-process coordination for everything under `data/`.

"One user, one machine" quietly means *several* processes, one directory. The
shape this project actually runs in is three writers at least:

* the MCP server a harness is talking to,
* the scheduled pass -- `tools/cron_apply.py` and the `tools/auto_apply.py`
  children it spawns,
* whatever the human runs by hand in a shell.

They share one `data/` tree and, more sharply, one Chrome profile. Two distinct
hazards come out of that, and they need two different mechanisms.

**Lost updates.** `memory.json` is the whole flywheel; `guard_state.json` holds
the daily cap. Both used to be read once at startup and written back whole, so
the last process to save silently undid everything the others had done since.
That is not hypothetical: an easy-apply batch started *before* a schema change
wrote its stale view back and deleted the entire route knowledge base, version
and all, while the migration that had just created it looked like it had
worked. A lock around read-modify-write fixes what can be made exact -- the
rails, see `guardrails.py` -- and a merge fixes what is a list of facts -- the
flywheel, see `memory.py`.

**Two drivers, one browser.** The MCP server and the scheduled pass launch
Chrome against the same `data/browser-profile`, because sharing the logged-in
session is the entire point. Two Chromes on one profile is not a race that
degrades gracefully; it is one account's cookies being rewritten under the
other's feet. So the browser has an exclusive lock, and every process that
intends to drive it has to hold that lock first.

Why `flock` rather than a pid file: the kernel drops the lock when the process
dies for any reason, so a crashed pass cannot wedge the schedule and no code
ever has to ask "is that pid still alive" -- a question with no safe answer,
since pids are recycled. There is deliberately no stale-lock cleanup here, and
no "detect whether an earlier process is still around" probe: there is nothing
to clean up and nothing to detect.

Scope, stated so nobody assumes more than is true:

* A lock only stops processes that *ask* for it. A writer that never takes it
  -- an older version of this code, a one-off script -- is not stopped by the
  lock at all. The merges in `memory.py` and `guardrails.py` are what make such
  a writer survivable, which is why they exist rather than being belt and
  braces.
* `flock` is advisory and local: one host, one filesystem. Two machines, or a
  network filesystem that does not carry locks, are out of scope.
"""

from __future__ import annotations

import errno
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

try:  # POSIX only. This project already hard-codes a macOS Chrome path, so a
    import fcntl  # Windows port would need real work in more places than this.
except ImportError:  # pragma: no cover - import-time platform check
    fcntl = None  # type: ignore[assignment]

# Set by a supervisor to hand its lock to a child it is about to spawn. Holds an
# fd *number*, not a secret: see `_inherited_fd` for why that is safe.
LOCK_FD_ENV = "APPLYOPS_LOCK_FD"

# How long a caller waits for a lock it has no business failing on -- a data
# file, written in milliseconds. The browser lock is taken differently: it is
# either free (start) or someone else is driving (refuse).
DEFAULT_TIMEOUT = 30.0

_POLL_SECONDS = 0.05

_FREE_NOTE = "# free\n"


class LockTimeout(RuntimeError):
    """Raised when a lock was still held at the end of its timeout."""


class LockUnsupported(RuntimeError):
    """Raised when the platform cannot provide the guarantee we depend on.

    Deliberately fatal rather than a silent no-op: a lock that does not lock
    would leave the daily cap and the flywheel unprotected while every log line
    claimed otherwise.
    """


# ── paths ────────────────────────────────────────────────────────────


def locks_dir(data_dir: str | Path) -> Path:
    """Where the data locks live. Inside `data/` so one directory is moved."""
    return Path(data_dir) / ".locks"


def data_lock_path(data_dir: str | Path, name: str) -> Path:
    return locks_dir(data_dir) / f"{name}.lock"


def browser_lock_path(data_dir: str | Path) -> Path:
    """The browser lock keeps its historical path and filename on purpose.

    `.cron_apply.lock` predates this module. Renaming it would be cosmetic, and
    it would let a process started before the rename run concurrently with one
    started after -- both holding "the" browser lock, neither blocking the
    other. Not worth it for a tidier name.
    """
    return Path(data_dir) / ".cron_apply.lock"


# ── reads and writes ─────────────────────────────────────────────────


def file_signature(path: str | Path) -> tuple[int, int, int] | None:
    """Cheap "has this file changed" fingerprint: (inode, mtime, size).

    Used to notice that another process wrote while we were holding an
    in-memory copy. `os.replace` gives every save a new inode, so this does not
    depend on timestamp resolution.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def read_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON, returning `default` for anything unreadable.

    A merge needs to look at what is on disk right now, and "not there yet" is
    the normal case for a first run, so this stays tolerant. Callers that must
    not confuse "absent" with "corrupt" take a distinct sentinel.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _fsync_dir(path: Path) -> None:
    """Flush the directory entry so the rename itself survives a power cut.

    Best effort by design: not every filesystem lets a directory be fsynced,
    and failing to flush a directory is not a reason to fail a save that the
    kernel has already committed. The file's own contents are fsynced above.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write a file so a reader sees the old one or the new one, never half.

    `write_text` truncates before it writes, so a crash -- or a full disk --
    halfway through leaves half a file. For `memory.json` that is the whole
    flywheel and for `application_log.json` it is the dedupe surface of record,
    so "half a file" is not an acceptable intermediate state.

    The temp name carries the pid because two processes saving the same target
    would otherwise share one temp path, and one of them would `os.replace` a
    file the other was still filling.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# ── the lock ─────────────────────────────────────────────────────────


def _require_flock() -> None:
    if fcntl is None:
        raise LockUnsupported(
            "this platform has no `fcntl.flock`, so cross-process locking "
            "cannot be provided. Running without it would leave the daily cap "
            "and the memory flywheel unprotected, so this refuses instead."
        )


def _metadata(fd: int) -> dict:
    try:
        raw = os.pread(fd, 4096, 0).decode("utf-8", "replace")
    except OSError:
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _inherited_fd(path: Path) -> int | None:
    """The lock descriptor a parent handed down, if it really is this file's.

    Verified by device and inode instead of trusted, which is what keeps the
    env var from being a hole: an unrelated process that happens to inherit
    `APPLYOPS_LOCK_FD` has no fd open on *this* file (the comparison rejects
    it) and must therefore acquire the lock the normal way, where it is
    correctly refused. The only way to pass the check is to have been spawned
    by the holder with `pass_fds`.

    Re-taking the flock on that fd is the second half of the proof: it is the
    same open file description the parent holds, so the kernel grants it
    immediately and without changing the lock.
    """
    raw = os.environ.get(LOCK_FD_ENV, "")
    if not raw.strip().isdigit():
        return None
    fd = int(raw.strip())
    try:
        held = os.fstat(fd)
        target = os.stat(path)
    except OSError:
        return None
    if (held.st_dev, held.st_ino) != (target.st_dev, target.st_ino):
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd


class FileLock:
    """An exclusive lock held by an open file description.

    Three properties, each of which the naive alternatives get wrong:

    * **The kernel releases it.** Any exit -- `return`, `SIGTERM`, `SIGKILL` --
      drops it, so there is no stale state to detect and no cleanup step to get
      wrong.
    * **It can be handed to a child.** A supervisor that spawns a worker must
      not deadlock against its own worker, and by default it does: the child's
      `acquire()` is refused because the parent holds the file. `child_kwargs`
      passes the descriptor down instead, so the child is recognised as the
      same holder.
    * **It carries its owner.** Pid, start time and purpose are written into
      the lock file, so `--status` can name the holder and its age without
      guessing.
    """

    def __init__(
        self,
        path: str | Path,
        purpose: str = "",
        block: bool = True,
        timeout: float | None = DEFAULT_TIMEOUT,
    ):
        self.path = Path(path)
        self.purpose = purpose
        self.block = block
        # A finite default, so a bug upstream fails as a `LockTimeout` naming
        # the holder instead of hanging a tool call forever. Waiting without a
        # deadline has to be asked for explicitly.
        self.timeout = timeout
        self._fd: int | None = None
        self._owned = False

    # ── state ────────────────────────────────────────────────────────

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def owned(self) -> bool:
        """True when *this* process took the lock and must give it back.

        False for a descriptor inherited from a parent: the parent owns the
        lock, and unlocking here would release it for the whole process tree.
        """
        return self._owned

    @property
    def fd(self) -> int | None:
        return self._fd

    # ── acquire / release ────────────────────────────────────────────

    def acquire(self) -> bool:
        """Take the lock. False only when non-blocking and someone else has it."""
        if self._fd is not None:
            return True
        _require_flock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        inherited = _inherited_fd(self.path)
        if inherited is not None:
            self._fd = inherited
            self._owned = False
            return True

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = None if self.timeout is None else time.time() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise
                if not self.block:
                    os.close(fd)
                    return False
                if deadline is not None and time.time() >= deadline:
                    os.close(fd)
                    raise LockTimeout(
                        f"{self.path.name} still held after {self.timeout:g}s "
                        f"({describe_holder(self.path)})"
                    )
                time.sleep(_POLL_SECONDS)

        self._fd = fd
        self._owned = True
        # Written after the lock is ours, so the note is never visible on a
        # lock that somebody else holds.
        self._write_owner()
        # Publish our own descriptor. This is what lets the *same* process --
        # or a child of it -- recognise the lock instead of deadlocking against
        # itself: `flock` is held per open file description, so a second
        # acquisition through a fresh descriptor is refused even by one's own
        # process. A batch runner that takes the browser lock and then drives
        # the in-process MCP tools would otherwise be refused by itself.
        os.environ[LOCK_FD_ENV] = str(fd)
        return True

    def _write_owner(self) -> None:
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "purpose": self.purpose or self.path.stem,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, (payload + "\n").encode("utf-8"))
        except OSError:
            pass

    def release(self) -> None:
        """Give the lock back. Safe to call twice, and safe on a lock never held.

        A descriptor inherited from a parent is deliberately *not* closed. It
        belongs to the process tree that holds the lock, and this process's
        later `acquire()` calls still need to recognise it; closing it here
        would make the next one look like a fresh request, which the kernel
        would then refuse. It drains when the process exits.
        """
        fd, owned = self._fd, self._owned
        self._fd = None
        self._owned = False
        if fd is None:
            return
        if not owned:
            return
        # Clear the note *before* unlocking: the other order would let a reader
        # see a free lock still naming the process that just left.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, _FREE_NOTE.encode("utf-8"))
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        if os.environ.get(LOCK_FD_ENV) == str(fd):
            os.environ.pop(LOCK_FD_ENV, None)

    def child_kwargs(self) -> dict:
        """`subprocess` kwargs that hand this lock to a child process.

        Returns `{}` when the lock is not held, so a caller can splat it
        unconditionally. Without this a supervisor deadlocks against the
        workers it spawns: the kernel refuses the child's own `acquire()`
        because the *parent* holds the file, and the worker exits believing
        somebody else is driving the browser.
        """
        if self._fd is None:
            return {}
        return {
            "pass_fds": (self._fd,),
            "env": {**os.environ, LOCK_FD_ENV: str(self._fd)},
        }

    def __enter__(self) -> Self:
        if not self.acquire():
            raise LockTimeout(f"{self.path.name} is held ({describe_holder(self.path)})")
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


# ── inspecting a lock without taking it ──────────────────────────────


def is_free(path: str | Path) -> bool:
    """Whether the lock is available, asked of the kernel rather than a file.

    This is the replacement for "read a pid file and see if that pid is alive".
    It takes the lock and immediately gives it back to find out, so it cannot
    be fooled by a recycled pid, a crashed holder, or a note left on disk.
    """
    _require_flock()
    path = Path(path)
    if not path.exists():
        return True
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    return True


def describe_holder(path: str | Path) -> str:
    """One line naming who holds the lock, for a log or an error message."""
    info = holder(path)
    if not info["held"]:
        return "free"
    age = info["age_seconds"]
    who = f"pid {info['pid']}" if info["pid"] else "another process"
    return f"{who} ({info['purpose'] or 'unknown purpose'}), held {age:.0f}s"


def holder(path: str | Path) -> dict:
    """Who holds this lock right now, and for how long.

    `held` comes from the kernel; the pid and purpose come from the note the
    holder wrote. `held` is the answer to trust -- if the note and the kernel
    disagree, the kernel is right and the note is a dead process's last words.
    """
    path = Path(path)
    info: dict = {
        "held": False,
        "pid": 0,
        "purpose": "",
        "started_at": "",
        "age_seconds": 0.0,
    }
    if path.exists():
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            fd = None
        if fd is not None:
            try:
                info.update(_metadata(fd))
            finally:
                os.close(fd)
    if is_free(path):
        return info
    info["held"] = True
    info["age_seconds"] = _age_seconds(info.get("started_at", ""))
    return info


def _age_seconds(started_at: str) -> float:
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


# ── the two convenient shapes ────────────────────────────────────────


@contextmanager
def exclusive(
    data_dir: str | Path,
    name: str,
    purpose: str = "",
    block: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> Iterator[FileLock]:
    """Hold the `<name>` data lock for the duration of the block.

    Every read-modify-write of a shared file goes through here. The lock makes
    the transaction atomic between processes; it does not make it durable, so
    the write inside it still has to be atomic on its own -- which is what
    `atomic_write_json` is for.
    """
    lock = FileLock(data_lock_path(data_dir, name), purpose=purpose, block=block, timeout=timeout)
    if not lock.acquire():
        raise LockTimeout(f"{name} is held ({describe_holder(lock.path)})")
    try:
        yield lock
    finally:
        lock.release()
