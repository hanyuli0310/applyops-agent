#!/usr/bin/env python3
"""Scheduled LinkedIn pass: check the rails, then discover and apply.

Personal / local development helper. **Not part of the shipped MCP surface.**

Running unattended is a different set of requirements from an interactive run:

* **It must not overlap itself.** A pass easily outlasts a 10-minute interval --
  the guardrails alone sleep 45-180s between applications, and discovery walks
  six keyword groups twice. Two passes on one browser would drive the same tab
  from two processes. An exclusive lock makes the second tick exit immediately
  instead of fighting the first. That lock is taken once per pass and *handed to
  the phase children*: they drive the same browser, so they are the same driver,
  and making them take it again is what used to make every phase of every pass
  fail with "another runner holds the browser".

* **It must fail loudly, not quietly.** A dead browser used to produce one
  bogus ledger row per posting: the run kept going, wrote 20 rows whose reason
  was a tool error, and looked like it had done work. Every abort path here
  prints why and returns non-zero.

* **It must not spend LinkedIn requests it cannot use.** When the daily quota is
  gone there is nothing a search can turn into an application, so the pass exits
  before touching a page.

* **It must not ask.** There is no human attached to a timer. A question the
  flywheel cannot answer is recorded and the posting abandoned, same as always.

Modes
-----
    --ensure        start the background loop if it is not running, else report
    --loop          run passes forever at --interval seconds (what --ensure starts)
    --status        is the loop alive, and what have the recent passes done
    --stop          stop the background loop
    (default)       exactly one pass

Why a background loop rather than one scheduled run per interval: the scheduler
this is driven by accepts a single hour per rule, so it cannot express "every 10
minutes" at all. The schedule therefore supervises -- it calls `--ensure` and
reports -- while the cadence lives here, where it is not limited to whole hours.
The lock is taken per pass, not for the daemon's lifetime, so a scheduled
`--ensure` and the loop can never drive the same form at the same time.

Liveness is a lock, not a pid file
----------------------------------
"Is the loop running" is answered by trying the daemon lock, which the kernel
releases the moment the process dies. The old answer was a pid file plus
`os.kill(pid, 0)`, and that question has no safe answer: pids get recycled, so a
dead daemon's file can name a live unrelated process, and a daemon that exits
through an unexpected path leaves a file that says it is still there. The
holder's pid is still written down -- `--stop` needs something to signal -- but
it is read out of the lock file and only consulted while the lock says somebody
is holding it.

Usage
-----
    python tools/cron_apply.py --ensure        # make sure the loop is running
    python tools/cron_apply.py --status
    python tools/cron_apply.py --stop
    python tools/cron_apply.py                 # one pass, in the foreground
    python tools/cron_apply.py --dry-run       # rails + browser only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from applyops import concurrency  # noqa: E402
from applyops.guardrails import Guardrails  # noqa: E402
from tools import singlewriter  # noqa: E402
from tools.attach import is_up, launch, session  # noqa: E402

LOG_PATH = PROJECT_ROOT / "data" / "application_log.json"
RUNS_PATH = PROJECT_ROOT / "data" / "cron_runs.jsonl"
# The pid file is kept only for the transition described in `_legacy_daemon_pid`.
# Liveness comes from DAEMON_LOCK_PATH, which the kernel maintains.
PID_PATH = PROJECT_ROOT / "data" / ".cron_apply.pid"
DAEMON_LOCK_PATH = concurrency.data_lock_path(PROJECT_ROOT / "data", "daemon")
STATE_PATH = PROJECT_ROOT / "data" / ".cron_state.json"
LOG_DIR = PROJECT_ROOT / "data" / "cron_logs"

DEFAULT_INTERVAL = 600
DEFAULT_MAX_PER_PASS = 6
DEFAULT_BUDGET_SECONDS = 3000
# Keyword groups searched per pass. There are six in auto_apply.KEYWORDS and a
# full sweep costs roughly eight minutes against the live site -- most of the
# interval, every interval. Rotating a couple of groups per pass means the whole
# set is still covered inside half an hour, but no single pass spends its budget
# on searching, and LinkedIn sees a fraction of the requests per ten minutes.
DEFAULT_KEYWORDS_PER_PASS = 2
# After a pass that long, pause at least this long before starting the next one.
MIN_NAP_SECONDS = 60
# Consecutive passes that fail for a reason a human has to fix (session gone,
# guardrails halted) before the loop stops itself rather than spinning.
MAX_CONSECUTIVE_FAILED_PASSES = 3

# LinkedIn answers these with a login wall or a challenge page. Continuing past
# one of them is the failure the whole project is built to avoid.
_DEAD_URL_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/login")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _say(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# single-instance lock
# --------------------------------------------------------------------------


def _acquire_lock(purpose: str = "cron pass"):
    """Exclusive, non-blocking. Returns the handle, or None if a driver holds it.

    Shared with `tools/auto_apply.py` and with the MCP server via
    `tools/singlewriter.py`: a manual run, a scheduled pass and an interactive
    session must not drive the same browser at the same time.
    """
    return singlewriter.acquire(purpose=purpose)


def _phase_kwargs(handle) -> dict:
    """Extras for a phase subprocess, so it shares this pass's lock.

    The phase children are the same driver as the pass that spawns them -- they
    act on the same tab, under the same rails check. Passing the lock down says
    so; making them acquire it themselves would have the kernel refuse them,
    because the parent is holding it.
    """
    return singlewriter.child_kwargs(handle)


def _submitted_count() -> int:
    try:
        data = json.loads(LOG_PATH.read_text())
    except (OSError, ValueError):
        return 0
    return len(
        [a for a in data.get("applications", []) if str(a.get("outcome")).startswith("submitted")]
    )


def _next_keyword_batch(count: int) -> list[str]:
    """The next `count` keyword groups, rotating through the whole set.

    The cursor is persisted because the loop is a sequence of short processes'
    worth of state in memory only -- and an unattended loop that restarted its
    rotation every pass would search the same two groups forever while never
    looking at the other four.
    """
    from tools.auto_apply import KEYWORDS

    state: dict = {}
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        pass
    cursor = int(state.get("keyword_cursor") or 0) % len(KEYWORDS)
    picked = [KEYWORDS[(cursor + i) % len(KEYWORDS)] for i in range(count)]
    state["keyword_cursor"] = (cursor + count) % len(KEYWORDS)
    state["keyword_batch_last_pass"] = picked
    state["updated_at"] = _now()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    return picked


# --------------------------------------------------------------------------
# one pass
# --------------------------------------------------------------------------


async def _browser_ready() -> tuple[bool, str]:
    """Launch if needed, then prove the LinkedIn session is still valid.

    The session is the one thing this project cannot rebuild for itself: once it
    expires, every later pass is wasted work. Checking costs one page load and
    turns "applied to nothing for six hours" into one clear line.
    """
    launch()
    if not is_up():
        return False, "Chrome did not open a debugging port on 9222"

    async with session(auto_launch=False) as bc:
        await bc.goto("https://www.linkedin.com/feed/", settle=2.5)
        url = await bc.get_current_url()
    for marker in _DEAD_URL_MARKERS:
        if marker in url:
            return False, f"LinkedIn session is not valid (landed on {url})"
    if "linkedin.com" not in url:
        return False, f"expected a LinkedIn page, got {url}"
    return True, url


def _run_phase(name: str, argv: list[str], log_file, budget: float, lock_kwargs: dict) -> dict:
    _say(f"phase {name}: {' '.join(argv)}")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "auto_apply.py"), *argv],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=max(30.0, budget),
            # The pass's browser lock, handed down. See `_phase_kwargs`.
            **lock_kwargs,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc = -1
        out = exc.stdout or ""
        err = f"phase exceeded its {budget:.0f}s budget and was killed"
        if isinstance(out, bytes):
            out = out.decode()
    elapsed = time.time() - started

    log_file.write(f"\n===== {name} (rc={rc}, {elapsed:.1f}s) =====\n")
    log_file.write(out or "")
    if err:
        log_file.write(f"\n--- stderr ---\n{err}")
    log_file.flush()
    for line in (out or "").strip().splitlines()[-3:]:
        _say(f"  {line}")
    if rc != 0:
        _say(f"  !! {name} exited {rc}: {(err or '')[:200]}")
    return {
        "rc": rc,
        "elapsed_seconds": round(elapsed, 1),
        "tail": (out or "").strip().splitlines()[-6:],
    }


async def _pass(args, lock=None) -> int:
    guards = Guardrails()
    lock_kwargs = _phase_kwargs(lock)
    stats = guards.stats()
    remaining = int(stats["remaining_today"])
    _say(
        f"rails: {stats['applied_today']}/{stats['daily_cap']} applied today "
        f"(UTC {stats['day']}), {remaining} left, halted={stats['halted']}"
    )

    if stats["halted"]:
        _say(f"ABORT: guardrails halted the run ({stats['halted_reason']}).")
        _say("Clear it with `op.py reset_halt` once the cause is fixed.")
        return 1

    # Checked before the browser is touched. A pass that cannot apply must cost
    # LinkedIn nothing -- an hourly idle pass that loads a page "just to check"
    # is exactly the traffic pattern the guardrails exist to avoid.
    if remaining <= 0 and not args.dry_run:
        _say("quota for this UTC day is gone - a search could not become an application.")
        _say("Exiting without loading a single results page.")
        return 0

    ok, detail = await _browser_ready()
    if not ok:
        _say(f"ABORT: {detail}")
        _say("Fix with: .venv/bin/python tools/import_chrome_session.py --verify")
        return 1
    _say(f"browser ok: {detail}")

    if args.dry_run:
        _say("dry run: rails and browser are usable, stopping before any search.")
        return 0

    per_pass = min(int(args.max_per_pass), remaining)
    _say(
        f"plan: {per_pass} application(s) this pass, "
        f"wall-clock budget {args.budget_seconds:.0f}s"
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    actions: dict[str, dict] = {}
    before = _submitted_count()
    deadline = time.time() + args.budget_seconds
    if args.keywords:
        batch = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        batch = _next_keyword_batch(max(1, int(args.keywords_per_pass)))
    _say(f"keyword batch: {', '.join(batch)}")
    keyword_args = ["--keywords", ",".join(batch)]

    with open(LOG_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.log", "w") as log_file:
        log_file.write(f"pass started {_now()}\n")
        log_file.write(f"rails: {json.dumps(stats)}\n")

        actions["discover"] = _run_phase(
            "discover",
            ["discover", "--location", args.location, *keyword_args],
            log_file,
            min(600, deadline - time.time()),
            lock_kwargs,
        )
        if actions["discover"]["rc"] != 0:
            _say("discovery failed - skipping apply rather than applying to a stale queue")

        if deadline - time.time() > 60:
            actions["apply"] = _run_phase(
                "apply",
                ["apply", "--limit", str(per_pass)],
                log_file,
                min(1800, deadline - time.time()),
                lock_kwargs,
            )
            sent = _submitted_count() - before
            if sent < per_pass and deadline - time.time() > 120:
                actions["retry"] = _run_phase(
                    "retry",
                    ["retry", "--limit", str(per_pass - sent)],
                    log_file,
                    min(900, deadline - time.time()),
                    lock_kwargs,
                )
        else:
            _say("out of budget before the apply phase")
            actions["apply"] = {"rc": None, "note": "skipped, budget exhausted"}

    after = _submitted_count()
    sent = after - before
    record = {
        "started_at": _now(),
        "keyword_batch": batch,
        "rails_before": stats,
        "submitted_this_pass": sent,
        "submitted_total": after,
        "actions": actions,
    }
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_PATH, "a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    _say(f"pass done: {sent} submitted ({after} total) -> {LOG_PATH}")

    # A pass where every phase failed is a failed pass, and the loop has to hear
    # about it. Reporting success here is how a broken harness looks like a
    # productive one: the run "finishes", the ledger stays empty, and the only
    # visible symptom is postings that were each skipped for their own reasons.
    phase_rcs = [a["rc"] for a in actions.values() if a.get("rc") is not None]
    if sent == 0 and phase_rcs and all(rc != 0 for rc in phase_rcs):
        _say("FAILED: every phase of this pass errored.")
        return 1
    return 0


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------


def _run_single_pass(args) -> int:
    # Held for the whole pass, and handed to the phase children. Everything the
    # rails decide and everything they record happens inside this, which is what
    # makes the daily cap exact against a manual run or an interactive session
    # rather than merely likely.
    handle = _acquire_lock()
    if handle is None:
        _say(f"another driver holds the browser ({singlewriter.describe()}) - skipping this tick")
        return 0
    try:
        return asyncio.run(_pass(args, handle))
    finally:
        singlewriter.release(handle)


def _daemon_lock():
    """The daemon's own lock, held for as long as it runs.

    Separate from the browser lock on purpose: the pass lock is taken and given
    back around each pass, while this one is the answer to "is a daemon alive".
    The kernel releases it on any exit, including a crash, so there is no pid to
    probe and no stale file to clean up.
    """
    return concurrency.FileLock(DAEMON_LOCK_PATH, purpose="cron daemon")


def _process_is(pid: int, needle: str) -> bool:
    """Whether `pid` is alive *and* looks like the process we think it is.

    Both halves matter. A recycled pid is alive but belongs to somebody else,
    and signalling it would be a real mistake -- so the command line is checked,
    which `os.kill(pid, 0)` cannot do.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            # Not `check=True`: `ps` exits non-zero for a pid that is simply
            # gone, and "no such process" is the answer we are looking for, not
            # an error to raise.
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return needle in (proc.stdout or "")


def _legacy_daemon_pid():
    """Pid of a daemon started by the *previous* version of this file.

    That version announced itself with a pid file rather than a lock, so a
    daemon still running from before this change holds nothing we can see, and
    `--ensure` would start a second loop beside it. This is a transitional check
    and nothing more: it can go once no such daemon is running, and it does
    nothing at all on a machine that never ran the old version.
    """
    try:
        pid = int(PID_PATH.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if not _process_is(pid, "cron_apply"):
        return None
    return pid


def _daemon_running():
    """The daemon's pid if one is alive, else None.

    The lock is the answer; the pid is only a convenience for `--stop`, read out
    of the lock file's own note. The pid file is consulted last and only when
    the lock is free, which is the one case it can still add information.
    """
    if concurrency.is_free(DAEMON_LOCK_PATH):
        return _legacy_daemon_pid()
    pid = int(concurrency.holder(DAEMON_LOCK_PATH).get("pid") or 0)
    return pid or None


def _daemon(args) -> int:
    lock = _daemon_lock()
    if not lock.acquire():
        _say(f"a daemon is already running ({concurrency.describe_holder(DAEMON_LOCK_PATH)})")
        return 1
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _say(
        f"daemon started (pid {os.getpid()}): a pass every {args.interval}s, "
        f"at most {args.max_per_pass} application(s) per pass"
    )
    failures = 0
    try:
        while True:
            started = time.time()
            rc = _run_single_pass(args)
            elapsed = time.time() - started
            if rc != 0:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILED_PASSES:
                    _say(
                        f"daemon: {failures} failed passes in a row - stopping rather than "
                        "repeating a broken attempt. Fix the cause, then re-run --ensure."
                    )
                    return 1
            else:
                failures = 0
            # A pass that outran the interval still gets a floor, so a slow run
            # cannot turn into a tight loop.
            nap = max(MIN_NAP_SECONDS, args.interval - elapsed)
            _say(f"daemon: pass took {elapsed:.0f}s, next pass in {nap:.0f}s")
            time.sleep(nap)
    finally:
        lock.release()
        try:
            PID_PATH.unlink()
        except OSError:
            pass


def _ensure(args) -> int:
    pid = _daemon_running()
    if pid is not None:
        print(f"daemon already running (pid {pid})")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "daemon.log", "a")
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--loop",
            "--interval",
            str(args.interval),
            "--max-per-pass",
            str(args.max_per_pass),
            "--location",
            args.location,
            "--keywords-per-pass",
            str(args.keywords_per_pass),
        ],
        cwd=str(PROJECT_ROOT),
        # stdin as well as stdout/stderr: the loop runs detached from whatever
        # started it, and an inherited dead stdin makes every child it spawns
        # die at interpreter startup. See `_run_phase`.
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        pid = _daemon_running()
        if pid is not None:
            print(f"daemon started (pid {pid})")
            return 0
        time.sleep(0.4)
    print("daemon did not come up - see data/cron_logs/daemon.log", file=sys.stderr)
    return 1


def _set_cap(args) -> int:
    """Raise or lower today's application ceiling.

    This is the only way to move the daily cap, and it lives in `tools/` rather
    than on the MCP surface on purpose: a harness that can raise its own rate
    limit does not have one. Requiring shell access means requiring the person
    whose account is on the line. The override covers today only and reverts on
    its own at the next UTC day boundary.
    """
    guards = Guardrails()
    before = guards.stats()
    after_cap = guards.set_daily_cap(args.set_cap, args.reason or "")
    after = guards.stats()
    print(
        f"daily cap: {before['daily_cap']} -> {after_cap} "
        f"for UTC day {after['day']} (base {after['base_daily_cap']})"
    )
    print(f"reason   : {after['cap_override']['reason']}")
    print(f"remaining today: {after['remaining_today']}")
    print("Reverts by itself at the next UTC day boundary; nothing to undo.")
    return 0


def _status(args) -> int:
    pid = _daemon_running()
    print(f"daemon : {'running (pid %d)' % pid if pid else 'not running'}")
    print(f"browser: {'held by ' + singlewriter.describe() if singlewriter.held_by_other() else 'free'}")
    guard = Guardrails().stats()
    print(
        f"rails  : {guard['applied_today']}/{guard['daily_cap']} today (UTC {guard['day']}), "
        f"{guard['remaining_today']} left, halted={guard['halted']}"
    )
    if guard.get("cap_override"):
        o = guard["cap_override"]
        print(
            f"cap    : overridden to {o['cap']} (base {guard['base_daily_cap']}) "
            f"at {o['set_at']} - {o['reason']}"
        )
    print(f"browser: {'up on 9222' if is_up() else 'down'}")
    if RUNS_PATH.exists():
        lines = RUNS_PATH.read_text().strip().splitlines()
        print(f"passes : {len(lines)} recorded")
        for line in lines[-args.tail :]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            print(f"  {r.get('started_at')}  submitted={r.get('submitted_this_pass')}  total={r.get('submitted_total')}")
    else:
        print("passes : none yet")
    return 0


def _stop(args) -> int:
    pid = _daemon_running()
    if pid is None:
        print("daemon is not running")
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    # Waiting on the lock rather than on `os.kill(pid, 0)`: the kernel drops it
    # when the process is truly gone, so this cannot report "stopped" while a
    # daemon is still draining a pass.
    while time.time() < deadline and _daemon_running() is not None:
        time.sleep(0.3)
    print(f"daemon stopped (pid {pid})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help="seconds between passes when looping")
    parser.add_argument("--location", default="United States")
    parser.add_argument("--keywords", default="", help="comma-separated; overrides the rotation")
    parser.add_argument("--keywords-per-pass", type=int, default=DEFAULT_KEYWORDS_PER_PASS)
    parser.add_argument("--max-per-pass", type=int, default=DEFAULT_MAX_PER_PASS)
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--loop", action="store_true", help="run passes forever")
    parser.add_argument("--ensure", action="store_true", help="start the loop if it is not running")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--set-cap",
        type=int,
        default=0,
        metavar="N",
        help="raise/lower today's application ceiling to N (today only)",
    )
    parser.add_argument("--reason", default="", help="why the cap was moved; recorded")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--tail", type=int, default=5, help="passes to show with --status")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.stop:
        return _stop(args)
    if args.set_cap:
        return _set_cap(args)
    if args.status:
        return _status(args)
    if args.ensure:
        return _ensure(args)
    if args.loop:
        return _daemon(args)
    return _run_single_pass(args)


if __name__ == "__main__":
    raise SystemExit(main())
