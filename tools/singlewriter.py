"""One writer for one browser.

Every process that drives the automation Chrome must hold this lock, because
two of them is not a race that degrades gracefully. They share a single tab, so
each one clicks on whatever the other left on screen, and each one then writes
an outcome into the ledger that belongs to neither. The resulting history would
look plausible and be wrong -- which is worse than an outright failure, since
the whole point of the ledger is to be the memory of record.

They also share one Chrome *profile* (`data/browser-profile`), which is a second
reason and a sharper one: two Chromes on one profile rewrite each other's cookie
database. The profile is shared on purpose -- the logged-in LinkedIn session is
the one thing this project cannot rebuild for itself -- so the lock is what
keeps sharing it from meaning corrupting it.

`flock` rather than a pid file: the kernel drops it when the process dies for
any reason, so a crashed pass can never wedge the schedule, and nobody has to
ask whether an earlier process is still alive. That question has no safe answer,
because pids are recycled.

The mechanism itself lives in `applyops.concurrency`, shared with the memory and
the guardrails. That is not tidiness: a supervisor has to be able to hand this
lock to the workers it spawns. The first version of this file could not, and the
consequence was that every phase of a scheduled pass -- which spawns
`auto_apply.py` as a child -- was refused by the kernel with "another runner
holds the browser", so a pass looked like it was running while all three of its
phases failed.

The lock file is still called `.cron_apply.lock`. The name predates this module
and is only cosmetic -- but changing it would let a daemon started before the
rename run concurrently with a process started after it, which is exactly the
hazard this module exists to prevent. Not worth it for a filename.
"""

from __future__ import annotations

from pathlib import Path

from applyops import concurrency

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = concurrency.browser_lock_path(PROJECT_ROOT / "data")


def acquire(block: bool = False, purpose: str = "browser driver"):
    """Take the browser lock. Returns a handle, or None if another driver has it."""
    lock = concurrency.FileLock(LOCK_PATH, purpose=purpose, block=block)
    return lock if lock.acquire() else None


def release(handle) -> None:
    """Give it back. Safe to call with None."""
    if handle is not None:
        handle.release()


def child_kwargs(handle) -> dict:
    """`subprocess` kwargs that let a child process share this lock.

    Splat it into `subprocess.run/Popen` for every child that will itself want
    the browser. Without it the child is refused by the kernel -- the parent
    holds the file -- and a supervisor deadlocks against its own workers.
    """
    return handle.child_kwargs() if handle is not None else {}


def held_by_other() -> bool:
    """Whether a driver is active right now, asked of the kernel.

    The replacement for reading a pid file and probing `os.kill(pid, 0)`: it
    cannot be fooled by a reused pid or a note left behind by a dead process,
    and it needs no cleanup after a crash.
    """
    return not concurrency.is_free(LOCK_PATH)


def describe() -> str:
    """One line naming the current holder, for a log or `--status`."""
    return concurrency.describe_holder(LOCK_PATH)


def holder() -> dict:
    """The current holder's pid, purpose and age, with `held` from the kernel."""
    return concurrency.holder(LOCK_PATH)
