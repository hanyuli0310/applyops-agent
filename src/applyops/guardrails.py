"""Deterministic safety rails around applying.

These live below the harness on purpose. A harness cannot be trusted to enforce
its own rate limit, and a prompt-level instruction ("do not apply to more than
20 jobs a day") is not a control -- it is a suggestion. Anything that must
actually hold therefore has to be implemented here, where it cannot be skipped.

Four rails:

* **Daily cap** -- a hard ceiling on applications per calendar day.
* **Minimum spacing** -- a randomized pause between applications, so the traffic
  pattern does not look like a fixed-interval machine.
* **Deduplication** -- refuse a posting we already applied to, keyed on the job
  id rather than the URL.
* **Circuit breaker** -- stop after N consecutive failures. A run that keeps
  failing is usually failing for one systemic reason (login expired, selector
  drift, a checkpoint page), and continuing just multiplies the damage.

Submission additionally requires a confirmation that was issued for *this* job
and is single-use. That is what makes "confirm before submitting" enforceable
rather than aspirational: the submit path checks for a token, and no token means
no submission.

Several processes run at once -- the MCP server, the scheduled pass, a manual
run -- so every rail here is read and written inside a lock, and every read
re-reads the file. A rail that lived only in one process's memory would not be
a rail: the daily cap used to be loaded once at startup, which meant two live
processes each got the whole day's quota, and a confirmation token checked
against a stale copy could be spent twice.

What the lock does *not* cover is the span of one application. The cap is made
exact by the caller instead: a driver holds the cross-process browser lock for
the whole "decide -> apply -> record" sequence (see `concurrency.py` and
`tools/singlewriter.py`), and a second driver re-checks at `browser_open` before
it touches a posting. That is the only critical section wide enough to contain
the decision and the record both, and it is one the browser forces on us anyway.
"""

from __future__ import annotations

import json
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from . import concurrency
from .memory import MemoryStore, dedup_key

# Applications permitted per calendar day. Deliberately conservative: LinkedIn
# rate-limits aggressive accounts, and a suspended account is a far worse
# outcome than a slower search.
DEFAULT_DAILY_CAP = 20
# Randomized gap between applications, in seconds.
DEFAULT_MIN_GAP = (45.0, 180.0)
# Consecutive failures that trip the breaker.
DEFAULT_MAX_FAILURES = 3
# A confirmation token is only good for this long.
CONFIRMATION_TTL_SECONDS = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


class Confirmation(BaseModel):
    """A one-shot approval to submit one specific application."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_key: str = ""
    job_url: str = ""
    summary: str = ""
    created_at: str = Field(default_factory=lambda: _now().isoformat())
    used: bool = False


class GuardState(BaseModel):
    """Mutable, persisted rail state. Kept apart from `memory.json` so a
    corrupted flywheel file cannot also wipe the safety bookkeeping."""

    day: str = ""
    applied_today: int = 0
    last_application_at: str = ""
    consecutive_failures: int = 0
    halted_reason: str = ""
    last_unverified_note: str = ""
    confirmations: dict[str, Confirmation] = Field(default_factory=dict)
    # A deliberate, human-made exception to the daily cap, valid for exactly one
    # day. Recorded with its provenance so that "why was this day different"
    # is answerable later from the state file alone.
    daily_cap_override: int = 0
    daily_cap_override_day: str = ""
    daily_cap_set_at: str = ""
    daily_cap_set_reason: str = ""


class Decision(BaseModel):
    """Verdict from `preflight`, with a reason a human can act on."""

    allowed: bool = False
    reason: str = ""
    job_key: str = ""
    remaining_today: int = 0
    wait_seconds: float = 0.0
    already_applied: bool = False


class Guardrails:
    def __init__(
        self,
        path: str | Path | None = None,
        memory: Optional[MemoryStore] = None,
        daily_cap: int = DEFAULT_DAILY_CAP,
        min_gap: tuple[float, float] = DEFAULT_MIN_GAP,
        max_consecutive_failures: int = DEFAULT_MAX_FAILURES,
    ):
        if path is None:
            path = Path(__file__).parent.parent.parent / "data" / "guard_state.json"
        self.path = Path(path)
        self.memory = memory or MemoryStore()
        self.daily_cap = daily_cap
        self.min_gap = min_gap
        self.max_consecutive_failures = max_consecutive_failures
        self._state = GuardState()
        self._load()

    # ── persistence ──────────────────────────────────────────────────

    def _read(self):
        """Load the state file into memory. Never writes.

        Split from the write so a transaction can read once, mutate, and write
        once -- the shape that makes a rail exact under concurrency. An
        unreadable file starts a clean day rather than raising: refusing to run
        at all would turn a corrupt byte into a system that cannot apply, and
        every caller that could produce that byte is already gone by the time
        anyone reads it.
        """
        if self.path.exists():
            try:
                self._state = GuardState.model_validate(
                    json.loads(self.path.read_text(encoding="utf-8"))
                )
            except Exception:
                self._state = GuardState()
        else:
            self._state = GuardState()

    def _write(self):
        concurrency.atomic_write_json(self.path, self._state.model_dump(mode="json"))

    def _load(self):
        self._read()
        if self._roll_over_if_new_day():
            self._write()

    @contextmanager
    def _transaction(self):
        """Read, mutate and write the rail state with nobody else in between.

        Every mutation goes through here. Holding the lock across the whole
        read-modify-write is what stops two processes from each spending the
        last slot of the day, or from each finding the same one-shot
        confirmation token unused and both submitting on it.
        """
        with concurrency.exclusive(self.path.parent, "guardrails", purpose="guard_state.json"):
            self._read()
            if self._roll_over_if_new_day():
                self._write()
            yield
            self._write()

    def _refresh(self):
        """Re-read from disk, so a decision is made on the file's numbers.

        A long-lived process -- the MCP server is the one that matters -- would
        otherwise decide against what it knew at startup, and hand out a quota
        the scheduled pass already spent.
        """
        with concurrency.exclusive(self.path.parent, "guardrails", purpose="guard_state.json"):
            self._read()
            if self._roll_over_if_new_day():
                self._write()

    def _roll_over_if_new_day(self) -> bool:
        """Apply the day boundary. Returns True when the state changed.

        Deliberately does not write: it is called from inside a transaction that
        owns the write, and from tests that inspect the effect directly. A
        second, internal write path would be a second place for the rails to
        drift out of sync with the file.
        """
        changed = False
        today = _today()
        if self._state.day != today:
            self._state.day = today
            self._state.applied_today = 0
            # The breaker is per-run, not per-day, so a new day clears it too.
            self._state.consecutive_failures = 0
            self._state.halted_reason = ""
            changed = True
        # An override answers a question about *one* day ("I have a batch ready
        # now"), so it expires with that day instead of quietly becoming the new
        # normal. A standing change belongs in DEFAULT_DAILY_CAP, where it is
        # visible in code review.
        if self._state.daily_cap_override and self._state.daily_cap_override_day != today:
            self._state.daily_cap_override = 0
            self._state.daily_cap_override_day = ""
            self._state.daily_cap_set_at = ""
            self._state.daily_cap_set_reason = ""
            changed = True
        return changed

    @property
    def effective_daily_cap(self) -> int:
        """The cap actually in force right now."""
        if (
            self._state.daily_cap_override
            and self._state.daily_cap_override_day == _today()
        ):
            return self._state.daily_cap_override
        return self.daily_cap

    def set_daily_cap(self, cap: int, reason: str = "") -> int:
        """Raise or lower today's ceiling, deliberately and on the record.

        Deliberately NOT reachable from the MCP tool surface. The whole point of
        this rail is that a harness cannot relax its own rate limit, so changing
        it requires shell access to the machine that owns the account -- which
        means it requires the human whose account it is. The reason string is
        mandatory for the same purpose: an unexplained raised cap in the state
        file is indistinguishable from a bug.
        """
        if int(cap) < 1:
            raise ValueError("daily cap must be at least 1")
        with self._transaction():
            self._state.daily_cap_override = int(cap)
            self._state.daily_cap_override_day = _today()
            self._state.daily_cap_set_at = _now().isoformat()
            self._state.daily_cap_set_reason = reason or "(no reason recorded)"
        return self.effective_daily_cap

    # ── preflight ────────────────────────────────────────────────────

    def preflight(self, job_url: str, job_id: str = "") -> Decision:
        """Decide whether this application may proceed, and why not if not.

        Re-reads the rail state first. This is the *permission*, and permission
        has to be granted against the file rather than against whatever this
        process remembered when it started -- otherwise a second driver spends
        the quota the first one already used.

        It is still only a permission: making the cap hold end to end also needs
        the check and the `record_outcome` to sit inside one critical section,
        which the caller supplies by holding the browser lock across both.
        """
        self._refresh()

        # Computed up front and attached to *every* outcome. Reporting it only
        # on the allowed path made each refusal read as `remaining_today: 0`,
        # which looks like "quota exhausted for the day" -- a caller that
        # checks it before trying a different posting would stop a whole batch
        # for no reason. The refusal reason is what should stop the caller, not
        # a misleading zero.
        remaining = max(0, self.effective_daily_cap - self._state.applied_today)

        key = dedup_key(job_url, job_id)
        if not key:
            return Decision(
                allowed=False,
                reason="no job URL or job id supplied",
                remaining_today=remaining,
            )

        if self.memory.is_already_applied(job_url, job_id):
            return Decision(
                allowed=False,
                reason="already applied to this posting (matched on job id)",
                job_key=key,
                already_applied=True,
                remaining_today=remaining,
            )

        if self._state.halted_reason:
            return Decision(
                allowed=False,
                reason=f"run halted: {self._state.halted_reason}",
                job_key=key,
                remaining_today=remaining,
            )

        if self._state.applied_today >= self.effective_daily_cap:
            return Decision(
                allowed=False,
                reason=(
                    f"daily cap reached ({self._state.applied_today}/"
                    f"{self.effective_daily_cap}); resume tomorrow"
                ),
                job_key=key,
                remaining_today=remaining,
            )

        if self._state.consecutive_failures >= self.max_consecutive_failures:
            return Decision(
                allowed=False,
                reason=(
                    f"{self._state.consecutive_failures} consecutive failures; "
                    "stopping rather than repeating the same mistake"
                ),
                job_key=key,
                remaining_today=remaining,
            )

        wait = self._seconds_until_next_allowed()
        return Decision(
            allowed=True,
            reason="ok",
            job_key=key,
            remaining_today=remaining,
            wait_seconds=wait,
        )

    def _seconds_until_next_allowed(self) -> float:
        """Randomized spacing, minus the time already elapsed."""
        last = self._state.last_application_at
        if not last:
            return 0.0
        try:
            previous = datetime.fromisoformat(last)
        except ValueError:
            return 0.0
        target = random.uniform(*self.min_gap)
        elapsed = (_now() - previous).total_seconds()
        return max(0.0, target - elapsed)

    # ── submit confirmation ──────────────────────────────────────────

    def request_submit_confirmation(
        self, summary: str, job_url: str, job_id: str = ""
    ) -> Confirmation:
        """Issue a single-use approval token for one application.

        The summary is what the human is meant to see, so it is stored with the
        token and echoed back on consumption -- a caller that never showed the
        summary still produced a token, which is why the returned summary is
        part of the contract rather than optional.
        """
        confirmation = Confirmation(
            job_key=dedup_key(job_url, job_id),
            job_url=job_url,
            summary=summary,
        )
        with self._transaction():
            self._state.confirmations[confirmation.id] = confirmation
            self._prune_confirmations()
        return confirmation

    def peek_confirmation(self, confirmation_id: str) -> Optional[Confirmation]:
        """Look at a token without spending it.

        Needed so a caller can be told *what* it forgot to acknowledge without
        first invalidating the token it is about to need.
        """
        self._refresh()
        return self._state.confirmations.get(confirmation_id)

    def consume_confirmation(self, confirmation_id: str) -> tuple[bool, str]:
        """Validate and spend an approval token. Returns (ok, message).

        The whole validate-and-spend runs inside one transaction, because two
        processes reading `used: false` and both then submitting is the exact
        failure this token exists to prevent. A check-then-set across a process
        boundary is not a check.
        """
        with self._transaction():
            confirmation = self._state.confirmations.get(confirmation_id)
            if confirmation is None:
                return False, "unknown confirmation id -- call request_submit_confirmation first"
            if confirmation.used:
                return False, "this confirmation was already used; ask again"

            age = (_now() - datetime.fromisoformat(confirmation.created_at)).total_seconds()
            if age > CONFIRMATION_TTL_SECONDS:
                return False, (
                    f"confirmation expired ({int(age)}s old, limit {CONFIRMATION_TTL_SECONDS}s); "
                    "re-confirm before submitting"
                )

            confirmation.used = True
            return True, confirmation.summary

    def _prune_confirmations(self):
        """Drop spent or expired tokens so the file does not grow forever."""
        keep: dict[str, Confirmation] = {}
        for key, confirmation in self._state.confirmations.items():
            if confirmation.used:
                continue
            try:
                age = (_now() - datetime.fromisoformat(confirmation.created_at)).total_seconds()
            except ValueError:
                continue
            if age <= CONFIRMATION_TTL_SECONDS:
                keep[key] = confirmation
        self._state.confirmations = keep

    # ── outcome bookkeeping ──────────────────────────────────────────

    def record_outcome(self, success: bool, note: str = ""):
        """Fold one application's result into the cap, spacing and breaker.

        Called from inside the driver's browser-lock critical section, which is
        what makes the cap exact: the permission and the record that consumes it
        are then serialised against every other driver, instead of two processes
        each being told the last slot was theirs.
        """
        with self._transaction():
            self._state.last_application_at = _now().isoformat()

            if success:
                self._state.applied_today += 1
                self._state.consecutive_failures = 0
            else:
                self._state.consecutive_failures += 1
                if self._state.consecutive_failures >= self.max_consecutive_failures:
                    self._state.halted_reason = (
                        f"{self._state.consecutive_failures} consecutive failures"
                        + (f": {note}" if note else "")
                    )

    def record_unverified(self, note: str = "") -> None:
        """Spend a slot on a submission whose result was never confirmed.

        Two properties, and they pull apart deliberately:

        - **It consumes quota.** The form was submitted; whether the employer has
          it is unknown, and pretending the slot is still free would let an
          unconfirmed result pay for another attempt.
        - **It does not reset the breaker.** An unconfirmed outcome is not
          evidence that whatever was failing has started working, so it does not
          clear the failure count. Only a verified success does that.

        Recording it as a success would make the "successes so far" number
        untrue; recording it as a failure would halt runs whose submissions were
        probably fine. Unverified is its own thing.
        """
        with self._transaction():
            self._state.last_application_at = _now().isoformat()
            self._state.applied_today += 1
            if note:
                self._state.last_unverified_note = note

    def reset_halt(self):
        """Clear a tripped breaker so a human can resume deliberately."""
        with self._transaction():
            self._state.halted_reason = ""
            self._state.consecutive_failures = 0

    def stats(self) -> dict:
        self._refresh()
        cap = self.effective_daily_cap
        return {
            "day": self._state.day,
            "applied_today": self._state.applied_today,
            "daily_cap": cap,
            "base_daily_cap": self.daily_cap,
            "cap_override": (
                {
                    "cap": self._state.daily_cap_override,
                    "day": self._state.daily_cap_override_day,
                    "set_at": self._state.daily_cap_set_at,
                    "reason": self._state.daily_cap_set_reason,
                }
                if cap != self.daily_cap
                else None
            ),
            "remaining_today": max(0, cap - self._state.applied_today),
            "consecutive_failures": self._state.consecutive_failures,
            "halted": bool(self._state.halted_reason),
            "halted_reason": self._state.halted_reason,
            "last_application_at": self._state.last_application_at,
            "last_unverified_note": self._state.last_unverified_note,
            "min_gap_seconds": list(self.min_gap),
        }
