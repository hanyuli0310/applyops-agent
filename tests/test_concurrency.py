"""Cross-process tests for the concurrency layer.

Everything here spawns real interpreters. The properties under test are
properties of the kernel and the filesystem -- a lock the kernel releases when
its holder is SIGKILLed, a merge that survives a writer which never took the
lock, a daily cap that holds when twelve processes race for the last slot -- so a
mock would only ever assert that the mock is wrong. The cost is a suite that
takes seconds instead of milliseconds, which is the right trade for the one part
of this project where being wrong is silent.

Two conventions worth knowing before reading:

* `data/` is never touched. Where a module reads its path from a module-level
  constant -- the batch runner does, at import time -- the child re-points that
  constant at a temporary directory *before* the import, because the flywheel is
  exactly the thing under test and a test that rewrites it is worse than no test.
* Every child gets an explicitly built environment. pytest runs the whole suite
  in one process, so a lock descriptor published by an earlier test would still
  be in `os.environ` and would make an unrelated child look like the holder of a
  lock it has never seen.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from applyops import concurrency
from applyops.guardrails import Guardrails
from applyops.memory import SCHEMA_VERSION, MemoryStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"

# A hung child must fail its test, not hang the suite. Also written into the
# child source below, where it cannot be interpolated -- the scripts are plain
# strings so their contents read exactly as the child will run them.
CHILD_TIMEOUT = 90


# ──────────────────────────────────────────────────────────────────────
# child plumbing
# ──────────────────────────────────────────────────────────────────────


def _preamble() -> str:
    """Every child's first two lines: make both trees importable.

    `applyops` is installed into the venv, but `tools/` is not a package on the
    path, so the batch runner needs the project root inserted explicitly.
    """
    return "import sys\n" f"sys.path[:0] = [{str(SRC)!r}, {str(PROJECT_ROOT)!r}]\n"


def _clean_env(lock_fd: int | None = None) -> dict:
    env = {key: value for key, value in os.environ.items() if key != concurrency.LOCK_FD_ENV}
    if lock_fd is not None:
        env[concurrency.LOCK_FD_ENV] = str(lock_fd)
    return env


def _spawn(code: str, *args: str, lock_fd: int | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        env=_clean_env(lock_fd),
        pass_fds=(lock_fd,) if lock_fd is not None else (),
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _collect(proc: subprocess.Popen) -> str:
    """Wait for a child and hand back its stdout, failing loudly if it died."""
    out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    )
    return out


def _run(code: str, *args: str, lock_fd: int | None = None) -> str:
    return _collect(_spawn(code, *args, lock_fd=lock_fd))


def _run_all(procs) -> list[str]:
    return [_collect(proc) for proc in procs]


# ──────────────────────────────────────────────────────────────────────
# the lock primitive
# ──────────────────────────────────────────────────────────────────────


def test_lock_excludes_strangers_and_is_recognised_by_a_child():
    """The two halves that make a supervising driver possible.

    A stranger must be refused, and a child the holder spawned must *not* be --
    which is the regression that cost this project a working scheduled loop:
    without the hand-off the kernel refused every phase child because its own
    parent held the file, so a pass looked like it was running while all three
    of its phases exited "another runner holds the browser".
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = concurrency.data_lock_path(tmp, "demo")
        probe = _preamble() + f"""
from pathlib import Path

from applyops import concurrency as C

lock = C.FileLock(Path({str(path)!r}), purpose="probe", block=False)
print("acquired=%s owned=%s" % (lock.acquire(), lock.owned), flush=True)
lock.release()
"""
        assert concurrency.is_free(path), "a lock nobody took must read as free"

        holder = concurrency.FileLock(path, purpose="test holder")
        assert holder.acquire()
        try:
            started = time.time()
            assert "acquired=False" in _run(probe)
            assert time.time() - started < 30, "a non-blocking refusal must not wait"

            inherited = _run(probe, lock_fd=holder.fd)
            assert "acquired=True owned=False" in inherited, inherited
            # The child was handed the descriptor, not given the lock: the
            # holder is still this process, which is what stops a worker from
            # unlocking the supervisor out from under it.
            assert holder.owned is True
        finally:
            holder.release()

        assert concurrency.is_free(path)
        assert concurrency.holder(path)["held"] is False
        assert "acquired=True owned=True" in _run(probe)


def test_lock_is_released_by_the_kernel_when_the_holder_is_killed():
    """Why there is no stale-lock cleanup anywhere in this project.

    A pid file would need one, and the check it needs -- "is that pid still
    alive" -- has no safe answer, because pids are recycled.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = concurrency.data_lock_path(tmp, "demo")
        hold = _preamble() + f"""
import time
from pathlib import Path

from applyops import concurrency as C

C.FileLock(Path({str(path)!r}), purpose="victim").acquire()
print("locked", flush=True)
time.sleep(180)
"""
        victim = _spawn(hold)
        assert victim.stdout.readline().strip() == "locked"
        try:
            assert not concurrency.is_free(path)
            assert concurrency.holder(path)["purpose"] == "victim"
            assert "victim" in concurrency.describe_holder(path)
        finally:
            os.kill(victim.pid, signal.SIGKILL)
            victim.wait(timeout=CHILD_TIMEOUT)

        assert concurrency.is_free(path), "SIGKILL must release the lock with no cleanup"
        assert concurrency.holder(path)["held"] is False
        # The note a dead process left behind is still there. `held` is the
        # answer to trust; the note is only read while `held` is true.
        assert concurrency.holder(path)["pid"] == victim.pid


def test_waiting_for_a_held_lock_gives_up_naming_the_holder():
    """A finite default, so a bug upstream fails instead of hanging a tool call."""
    with tempfile.TemporaryDirectory() as tmp:
        path = concurrency.data_lock_path(tmp, "demo")
        hold = _preamble() + f"""
import time
from pathlib import Path

from applyops import concurrency as C

C.FileLock(Path({str(path)!r}), purpose="slow pass").acquire()
print("locked", flush=True)
time.sleep(180)
"""
        victim = _spawn(hold)
        assert victim.stdout.readline().strip() == "locked"
        try:
            started = time.time()
            with pytest.raises(concurrency.LockTimeout) as caught:
                concurrency.FileLock(path, timeout=0.4).acquire()
            assert time.time() - started < 30
            assert "slow pass" in str(caught.value)
        finally:
            os.kill(victim.pid, signal.SIGKILL)
            victim.wait(timeout=CHILD_TIMEOUT)


def test_atomic_writes_leave_no_debris():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "state.json"
        concurrency.atomic_write_json(target, {"中文": "保留", "n": 1})
        concurrency.atomic_write_json(target, {"n": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"n": 2}
        # Only the target. A leftover temp file means a crash could be read as
        # state, and a shared temp name means two writers collide on it.
        assert sorted(p.name for p in Path(tmp).iterdir()) == ["state.json"]


# ──────────────────────────────────────────────────────────────────────
# the rails, under contention
# ──────────────────────────────────────────────────────────────────────

_CAP_WORKER = _preamble() + """
import sys
import time
from pathlib import Path

from applyops import concurrency
from applyops.guardrails import Guardrails
from applyops.memory import MemoryStore

root, n = Path(sys.argv[1]), sys.argv[2]
rails = Guardrails(
    path=root / "guard_state.json",
    memory=MemoryStore(root / "memory.json"),
    daily_cap=5,
    min_gap=(0.0, 0.0),
)

# The contract the real driver follows: decide and record inside the browser
# lock, because that is the only critical section wide enough to hold both. The
# sleep stands in for an application that takes a few seconds.
lock = concurrency.FileLock(
    concurrency.browser_lock_path(root), purpose="driver " + n, timeout=90
)
lock.acquire()
try:
    decision = rails.preflight("https://linkedin.com/jobs/view/" + n)
    if decision.allowed:
        time.sleep(0.05)
        rails.record_outcome(success=True)
        print("SUBMITTED", flush=True)
    else:
        print("REFUSED: " + decision.reason, flush=True)
finally:
    lock.release()
"""


def test_daily_cap_holds_when_twelve_processes_race_for_it():
    """The project's central safety claim, measured rather than asserted.

    Before this, each process read the rail state once at startup and wrote it
    back whole, so two drivers each got a full day's quota and the effective cap
    was whatever you multiplied it by.
    """
    with tempfile.TemporaryDirectory() as tmp:
        outs = _run_all([_spawn(_CAP_WORKER, tmp, str(i)) for i in range(12)])

        state = json.loads((Path(tmp) / "guard_state.json").read_text())
        assert state["applied_today"] == 5, f"cap violated: {state}"
        assert sum("SUBMITTED" in out for out in outs) == 5
        assert sum("REFUSED" in out for out in outs) == 7
        for out in outs:
            if "REFUSED" in out:
                assert "daily cap reached (5/5)" in out, out


_TOKEN_WORKER = _preamble() + """
import sys
from pathlib import Path

from applyops.guardrails import Guardrails
from applyops.memory import MemoryStore

root = Path(sys.argv[1])
rails = Guardrails(path=root / "guard_state.json", memory=MemoryStore(root / "memory.json"))
ok, message = rails.consume_confirmation(sys.argv[2])
print(("SPENT" if ok else "REFUSED") + ": " + message, flush=True)
"""


def test_a_confirmation_token_can_only_be_spent_once():
    """A double-spent token is two submissions from one approval.

    The token is what makes "confirm before submitting" a control rather than a
    request, so spending it has to be atomic: two processes finding the same
    unused token would both submit on a single human yes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rails = Guardrails(
            path=root / "guard_state.json", memory=MemoryStore(root / "memory.json")
        )
        token = rails.request_submit_confirmation(
            "submit application to Example Corp", "https://linkedin.com/jobs/view/9"
        )

        outs = _run_all([_spawn(_TOKEN_WORKER, tmp, token.id) for _ in range(4)])

        assert sum(out.startswith("SPENT") for out in outs) == 1, outs
        assert sum("already used" in out for out in outs) == 3


# ──────────────────────────────────────────────────────────────────────
# the flywheel, under contention
# ──────────────────────────────────────────────────────────────────────

_MEMORY_WORKER = _preamble() + """
import sys
from pathlib import Path

from applyops.memory import MemoryStore

root, n, word = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = MemoryStore(root / "memory.json")
store.add_application(
    "https://linkedin.com/jobs/view/" + n,
    job_title="Engineer " + n,
    company="Company " + n,
    platform="LinkedIn",
    apply_route="easy_apply",
)
store.learn("Do you hold the " + word + " certification?", "Yes " + word, source="user")
print("SAVED", flush=True)
"""

# Distinct words, not distinct numbers. `normalize_question` keeps only content
# words longer than one character, so "track 0" and "track 7" normalize to the
# same key and would collapse into a single learned answer -- which is the right
# behaviour for the flywheel and would quietly turn this test into a no-op.
_TOPICS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")


def test_concurrent_writers_lose_nothing():
    """Eight processes, one file, no rows lost and no version regression.

    This is the test that would have caught the original bug: a batch run
    started *before* a schema change saved its stale view back and deleted the
    whole route knowledge base, while the migration that had just created it
    looked like it had worked.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory.json"
        seeded = MemoryStore(path)  # create and seed before the race
        platform_count = len(seeded._data.platforms)

        outs = _run_all(
            [_spawn(_MEMORY_WORKER, tmp, str(i), _TOPICS[i]) for i in range(8)]
        )
        assert all("SAVED" in out for out in outs)

        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["version"] == SCHEMA_VERSION
        urls = {row["job_url"] for row in doc["application_history"]}
        assert urls == {f"https://linkedin.com/jobs/view/{i}" for i in range(8)}
        answers = {qa["answer"] for qa in doc["learned_qa"]}
        assert answers >= {f"Yes {word}" for word in _TOPICS}, answers
        # Every process seeded the same priors and merged the same ones back, so
        # the platform set must not have grown a duplicate entry per writer.
        assert len(doc["platforms"]) == platform_count
        # And the merge reports what it had to resolve rather than hiding it.
        assert doc["merge_conflicts"] >= 0


def test_a_stale_writer_cannot_delete_the_route_knowledge_base():
    """Merge, not overwrite -- and not only for writers that take the lock.

    An easy-apply batch running the previous version of this code was the actual
    author of the original loss. pydantic drops fields it does not know about,
    so its save wrote back a file with no `routes` key at all and version 3. The
    next save from current code has to put them back: the seeded knowledge is in
    memory too, and `version` only ever moves forward.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory.json"
        store = MemoryStore(path)
        assert store.get_route("Amazon", "external_ats").steps
        assert json.loads(path.read_text())["routes"]

        # An older process saves its own view: the facts it knows, version 3, no
        # `routes` key. Written by hand because that is exactly what its pydantic
        # dump produced.
        doc = json.loads(path.read_text())
        doc["version"] = 3
        doc.pop("routes")
        path.write_text(json.dumps(doc), encoding="utf-8")

        MemoryStore(path).learn("Are you authorized to work in the US?", "Yes", source="user")

        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["version"] == SCHEMA_VERSION, "a rollback must not survive a save"
        assert after["routes"], "the stale writeback must not be able to delete routes"
        assert "Amazon/external_ats" in after["routes"]
        assert after["routes"]["Amazon/external_ats"]["steps"], (
            "the seeded shape has to come back too, not just the key"
        )


# ──────────────────────────────────────────────────────────────────────
# the browser profile: one driver at a time
# ──────────────────────────────────────────────────────────────────────


def test_mcp_refuses_to_launch_a_second_browser_on_the_profile():
    """The refusal path, without launching a browser.

    Two Chromes on `data/browser-profile` rewrite each other's cookie database,
    and the logged-in session is the one thing this project cannot rebuild. So
    the server must refuse rather than open a second one -- and the refusal has
    to name the holder, because "busy" with no name is not actionable.
    """
    from applyops.mcp.runtime import BrowserBusy, Runtime

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        holder = concurrency.FileLock(concurrency.browser_lock_path(root), purpose="cron pass")
        assert holder.acquire()
        try:
            runtime = Runtime(data_dir=root)
            with pytest.raises(BrowserBusy) as caught:
                asyncio.run(runtime.get_browser())
            message = str(caught.value)
            assert "cron pass" in message
            assert "cron_apply.py --stop" in message
            assert runtime.browser is None, "a refusal must not leave a half-launched browser"
        finally:
            holder.release()

        # Free again: the claim succeeds, and claiming twice from one process
        # must not deadlock against itself. That self-deadlock is not
        # hypothetical -- `flock` is held per open file description, so a second
        # acquisition through a fresh descriptor is refused even by its own
        # process, which is why `acquire` publishes its descriptor.
        runtime = Runtime(data_dir=root)
        runtime._claim_profile()
        runtime._claim_profile()
        assert runtime.profile_lock.held
        assert runtime.profile_lock.owned
        runtime.profile_lock.release()
        assert concurrency.is_free(concurrency.browser_lock_path(root))


def test_mcp_releases_the_profile_it_claimed():
    """`browser_close` has to give the profile back, not just close the window.

    A server that kept the lock for its lifetime would leave the scheduled loop
    refusing every pass for the rest of the day -- a real cost, not a
    theoretical one.
    """
    from applyops.mcp.runtime import Runtime

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = Runtime(data_dir=root)
        runtime._claim_profile()
        assert not concurrency.is_free(concurrency.browser_lock_path(root))

        asyncio.run(runtime.shutdown())

        assert concurrency.is_free(concurrency.browser_lock_path(root))
        assert runtime.browser is None


# ──────────────────────────────────────────────────────────────────────
# the ledger: the dedupe surface of record
# ──────────────────────────────────────────────────────────────────────

# Every ledger child re-points the batch runner at its temporary directory
# *before* importing it: `data/application_log.json` is the deduplication
# surface of record, and a test that wrote the real one would be creating false
# "already applied" rows in the developer's own history.
_LEDGER_BOOTSTRAP = _preamble() + """
import sys
import types
from pathlib import Path

from applyops.mcp.runtime import Runtime

root = Path(sys.argv[1])

# `applyops.mcp.server` builds its module-level RUNTIME -- pointed at the real
# data directory -- at import time, and `tools.auto_apply` imports it. Swapping
# in a stand-in first is what keeps this test off the developer's flywheel.
fake = types.ModuleType("applyops.mcp.server")
fake.RUNTIME = Runtime(root)
fake.build_server = lambda: None
sys.modules["applyops.mcp.server"] = fake

import tools.auto_apply as batch  # noqa: E402

batch.DATA = root
batch.LOG_PATH = root / "application_log.json"
"""

_LEDGER_WORKER = _LEDGER_BOOTSTRAP + """
n = sys.argv[2]
ledger = batch.Ledger()
for i in range(3):
    ledger.record_application(
        {"job_id": "job-%s-%d" % (n, i), "outcome": "submitted", "company": "Company " + n}
    )
print("LEDGER DONE", flush=True)
"""

_LEDGER_DEDUPE_WORKER = _LEDGER_BOOTSTRAP + """
# First process: one posting applied to.
batch.Ledger().record_application({"job_id": "job-1", "outcome": "submitted"})

# Second process: the same posting, recorded again with more evidence -- a retry
# that got further. It has no idea the first row exists.
again = batch.Ledger()
again.data["applications"].append({"job_id": "job-1", "outcome": "submitted", "retries": 2})
again.flush()
print("LEDGER DONE", flush=True)
"""


def test_two_ledgers_keep_both_writers_rows():
    """Losing a ledger row is not a lost statistic -- it is a second submission.

    The ledger is rewritten in full after every attempt, so with two writers the
    second one to save used to delete the first one's rows. Those rows are what
    answer "have I already applied to this".
    """
    with tempfile.TemporaryDirectory() as tmp:
        outs = _run_all([_spawn(_LEDGER_WORKER, tmp, name) for name in ("a", "b")])
        assert all("LEDGER DONE" in out for out in outs)

        doc = json.loads((Path(tmp) / "application_log.json").read_text(encoding="utf-8"))
        assert {row["job_id"] for row in doc["applications"]} == {
            f"job-a-{i}" for i in range(3)
        } | {f"job-b-{i}" for i in range(3)}
        # Recomputed from the merged rows rather than carried across, so the
        # total can never disagree with what it counts.
        assert doc["applied_total"] == 6


def test_ledger_merge_does_not_duplicate_a_posting():
    """Two rows for one posting would inflate the totals and the retry queue."""
    with tempfile.TemporaryDirectory() as tmp:
        assert "LEDGER DONE" in _run(_LEDGER_DEDUPE_WORKER, tmp)

        doc = json.loads((Path(tmp) / "application_log.json").read_text(encoding="utf-8"))
        assert len(doc["applications"]) == 1, doc["applications"]
        # The row that recorded more attempts wins, which can only ever
        # under-report how many times we tried -- never invent evidence.
        assert doc["applications"][0]["retries"] == 2
        assert doc["applied_total"] == 1
