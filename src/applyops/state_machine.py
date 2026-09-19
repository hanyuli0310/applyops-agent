"""The application state machine.

An application's state is not a label; it is a promise about what has and has
not happened to it. That only holds if the set of legal moves is explicit. The
states below are the vocabulary every other module speaks:

| state | meaning |
|---|---|
| `QUEUED` | accepted, nothing done yet |
| `PREPARING` | an attempt is being assembled (fields, resume, snapshot) |
| `WAITING_FOR_INPUT` | blocked on answers only a human has |
| `WAITING_FOR_APPROVAL` | ready and priced; waiting for a grant |
| `SUBMITTING` | inside the single submission path right now |
| `SUBMITTED_VERIFIED` | the page confirmed it |
| `SUBMITTED_UNVERIFIED` | sent, possibly received, never confirmed |
| `FAILED` | this attempt did not happen, or was refused before sending |
| `CANCELLED` | withdrawn before sending |
| `SKIPPED` | ruled out before preparing |
| `LEGACY_IMPORTED` | arrived from history without evidence; never upgraded |

The transitions encode the safety rules rather than describing them:

- **`SUBMITTING` is one-way out of `WAITING_FOR_APPROVAL`, and it cannot go
  back.** A claim that reaches here either ends verified, unverified or failed.
  There is no `SUBMITTING -> WAITING_FOR_APPROVAL`, because "we were about to
  click and decided not to" is not something the ledger can know -- and treating
  uncertainty as a chance to re-submit is the duplicate-application machine.
- **`SUBMITTED_UNVERIFIED` may only become `SUBMITTED_VERIFIED` or `FAILED`**
  through reconciliation evidence, never by trying again.
- **`LEGACY_IMPORTED` is terminal.** History without evidence is recorded as an
  attempt and never promoted into a success (`PLAN.md` §4.2).
- **`FAILED` may prepare again** -- a retry is a *new attempt*, which is why
  attempts exist as their own rows. The previous attempt is never overwritten.

Transitions are checked in :func:`require_transition` and *also* enforced
optimistically by the ledger (`UPDATE ... WHERE state = :expected`), so two
processes cannot both believe they moved the same application.
"""

from __future__ import annotations

from enum import Enum


class ApplicationState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUBMITTING = "submitting"
    SUBMITTED_VERIFIED = "submitted_verified"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    LEGACY_IMPORTED = "legacy_imported"


class InvalidTransition(Exception):
    """A move the state machine does not allow, with the reason spelled out."""

    def __init__(self, current: ApplicationState, target: ApplicationState):
        self.current = current
        self.target = target
        super().__init__(f"cannot move from {current.value} to {target.value}")


_TERMINAL_SUCCESS = {
    ApplicationState.SUBMITTED_VERIFIED,
    ApplicationState.SUBMITTED_UNVERIFIED,
    ApplicationState.CANCELLED,
    ApplicationState.SKIPPED,
    ApplicationState.LEGACY_IMPORTED,
}

TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.QUEUED: {
        ApplicationState.PREPARING,
        ApplicationState.WAITING_FOR_INPUT,
        ApplicationState.SKIPPED,
        ApplicationState.CANCELLED,
    },
    ApplicationState.PREPARING: {
        ApplicationState.WAITING_FOR_INPUT,
        ApplicationState.WAITING_FOR_APPROVAL,
        ApplicationState.FAILED,
        ApplicationState.CANCELLED,
        ApplicationState.SKIPPED,
    },
    ApplicationState.WAITING_FOR_INPUT: {
        ApplicationState.PREPARING,  # answers arrived; assemble again
        ApplicationState.CANCELLED,
        ApplicationState.SKIPPED,
    },
    ApplicationState.WAITING_FOR_APPROVAL: {
        ApplicationState.SUBMITTING,
        ApplicationState.PREPARING,  # snapshot drifted; prepare anew
        ApplicationState.CANCELLED,
        ApplicationState.SKIPPED,
    },
    ApplicationState.SUBMITTING: {
        ApplicationState.SUBMITTED_VERIFIED,
        ApplicationState.SUBMITTED_UNVERIFIED,
        ApplicationState.FAILED,
    },
    ApplicationState.SUBMITTED_UNVERIFIED: {
        # Reconciliation evidence only. There is no path back to SUBMITTING:
        # an unknown result forbids another send.
        ApplicationState.SUBMITTED_VERIFIED,
        ApplicationState.FAILED,
    },
    ApplicationState.FAILED: {
        # A retry is a new attempt; the failed attempt stays on the record.
        ApplicationState.PREPARING,
        ApplicationState.CANCELLED,
        ApplicationState.SKIPPED,
    },
    ApplicationState.SUBMITTED_VERIFIED: set(),
    ApplicationState.CANCELLED: set(),
    ApplicationState.SKIPPED: set(),
    ApplicationState.LEGACY_IMPORTED: set(),
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in TRANSITIONS.get(current, set())


def require_transition(
    current: ApplicationState | str, target: ApplicationState | str
) -> tuple[ApplicationState, ApplicationState]:
    """Validate a move. Returns the parsed pair or raises `InvalidTransition`."""
    cur = ApplicationState(current)
    nxt = ApplicationState(target)
    if not can_transition(cur, nxt):
        raise InvalidTransition(cur, nxt)
    return cur, nxt


def is_terminal(state: ApplicationState | str) -> bool:
    """Whether an application can still move somewhere useful.

    `SUBMITTED_UNVERIFIED` counts as settled-but-watchable: it cannot be
    submitted again, and that is the property callers actually care about.
    """
    return ApplicationState(state) in _TERMINAL_SUCCESS


def can_submit(state: ApplicationState | str) -> bool:
    """Whether the single submission path may run from here."""
    return ApplicationState(state) in {
        ApplicationState.WAITING_FOR_APPROVAL,
    }
