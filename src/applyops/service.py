"""The unified application service.

One place owns the lifecycle of an application. Before this module there were as
many lifecycles as there were entry points -- the MCP tools recorded one shape of
history, the batch runner another, and nothing connected "the user approved" to
"the thing that clicked" to "what we tell the user happened".

The service is deliberately thin over the parts that already work:

- `state_machine` decides which moves are legal;
- `ledger` makes them durable and claim-safe;
- `submission.execute_authorized_submission` remains the only thing that ever
  clicks a final submit (M1's boundary, unchanged);
- `memory` keeps recording the flywheel, because its counters are only fed with
  outcomes that were actually verified.

What this module adds is the connective tissue: claims so two entry points cannot
drive one application, attempts so a retry never overwrites history, and recovery
so a crashed process lands in an honest state instead of a hopeful one.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .answers import AnswerStore
from .authorization import SubmissionAuthorizer
from .browser import BrowserController
from .company_policy import CompanyDecision, CompanyPolicyStore
from .guardrails import Guardrails
from .ledger import ApplicationRow, Ledger
from .memory import MemoryStore
from .resume import ResumeRef
from .state_machine import ApplicationState, InvalidTransition
from .submission import (
    STATUS_UNVERIFIED,
    FinalAction,
    SubmissionRefused,
    SubmitOutcome,
    execute_authorized_submission,
    reconcile_submission,
)

#: How long a service instance's claim is trusted. Short enough that a crashed
#: process does not wedge an application for hours; long enough to cover a form
#: with a human in the loop.
CLAIM_TTL = 600.0


@dataclass
class ServiceConfig:
    data_dir: Path
    owner: str = ""


#: How long a submission may pause for the rails' interval. Bounded, because a
#: caller waiting on a web request should be told "not yet" rather than held.
MAX_INLINE_WAIT_SECONDS = 60.0


class ApplicationService:
    """Coordinates the ledger, the state machine and the M1 submission path."""

    def __init__(
        self,
        data_dir: str | Path,
        memory: MemoryStore | None = None,
        authorizer: SubmissionAuthorizer | None = None,
        ledger: Ledger | None = None,
        answers: AnswerStore | None = None,
        guardrails: Guardrails | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.owner = f"svc-{uuid.uuid4().hex[:8]}"
        self.ledger = ledger or Ledger(self.data_dir / "app.sqlite")
        self.memory = memory
        self.authorizer = authorizer or SubmissionAuthorizer(self.data_dir)
        self.answers = answers or AnswerStore(self.data_dir)
        self.guardrails = guardrails

    # ── revisions ────────────────────────────────────────────────────

    def revisions(self) -> tuple[str, str]:
        """(profile_revision, answers_revision) -- one definition, every driver.

        These used to be computed in three places with three meanings, and the
        M1 grant digest compared values that nobody was actually passing: the UI
        and the runner sent empty strings, so "the facts changed" was recorded as
        "nothing changed". A single implementation is the only way the check
        means anything.
        """
        profile_revision = "no-profile"
        if self.memory is not None:
            try:
                text = self.memory.profile.path.read_text(encoding="utf-8")
                profile_revision = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            except OSError:
                profile_revision = "unreadable"

        # Scoped answers *and* the flywheel's learned answers both count: an
        # answer changing at either layer invalidates approvals based on it.
        flywheel = ""
        if self.memory is not None:
            qa = self.memory.get_all_qa()
            flywheel = f"{len(qa)}:{max((getattr(q, 'updated_at', '') or '' for q in qa), default='')}"
        answers_revision = f"{self.answers.revision}:{flywheel}"
        return profile_revision, answers_revision

    # ── enqueue / read ───────────────────────────────────────────────

    def enqueue(
        self,
        *,
        job_url: str,
        job_id: str = "",
        route: str = "",
        platform: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
    ) -> ApplicationRow:
        """Accept a posting into the queue. Idempotent per job key."""
        return self.ledger.create_application(
            job_key=job_id or job_url,
            job_url=job_url,
            route=route,
            platform=platform,
            title=title,
            company=company,
            location=location,
        )

    def get(self, application_id: str) -> ApplicationRow | None:
        return self.ledger.get(application_id)

    def list(self, state: str | None = None) -> list[ApplicationRow]:
        return self.ledger.list_applications(state)

    def status(self, application_id: str) -> dict:
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")
        return {
            "application": row.to_dict(),
            "attempts": [a.to_dict() for a in self.ledger.attempts(application_id)],
            "events": self.ledger.events(application_id),
        }

    # ── lifecycle ────────────────────────────────────────────────────

    def prepare(
        self,
        application_id: str,
        *,
        ready: bool,
        detail: str = "",
        payload: dict | None = None,
    ) -> ApplicationRow:
        """Assemble an attempt: QUEUED or FAILED -> PREPARING -> next state.

        `ready` is the caller's statement that everything a submission needs is
        present (fields answered, resume attached, snapshot taken). Being not
        ready is not a failure -- it parks the application in
        `WAITING_FOR_INPUT`, which is exactly what that state is for.
        """
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")

        # Rails first, against the file rather than against memory: quota,
        # spacing, circuit breaker and the duplicate check apply to every
        # driver, or they are not rails at all.
        if self.guardrails is not None:
            decision = self.guardrails.preflight(row.job_url, row.job_key)
            if not decision.allowed:
                raise SubmissionRefused(decision.reason)

        if not self.ledger.claim(application_id, self.owner):
            raise InvalidTransition(ApplicationState(row.state), ApplicationState.PREPARING)

        try:
            if row.state in {
                ApplicationState.QUEUED.value,
                ApplicationState.FAILED.value,
                ApplicationState.WAITING_FOR_INPUT.value,
            }:
                row = self.ledger.transition(
                    application_id, ApplicationState.PREPARING, expected_state=row.state
                )
            target = (
                ApplicationState.WAITING_FOR_APPROVAL
                if ready
                else ApplicationState.WAITING_FOR_INPUT
            )
            row = self.ledger.transition(
                application_id, target, payload={"detail": detail, **(payload or {})}
            )
        finally:
            if row.state != ApplicationState.WAITING_FOR_APPROVAL.value:
                self.ledger.release_claim(application_id, self.owner)
        return row

    async def submit(
        self,
        application_id: str,
        *,
        grant_id: str,
        controller: BrowserController,
        resume: ResumeRef,
        action: FinalAction,
        route: str = "",
    ) -> SubmitOutcome:
        """Run the authorized submission inside a claim, and record the truth.

        The claim is what makes "two entry points" safe: the loser of the claim
        is told so before anything happens. The state moves to `SUBMITTING`
        *before* the click, so a crash lands in a state whose only legal exit is
        via reconciliation -- never a second send.
        """
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")

        # Re-check NEVER at the final boundary as well. A posting may have
        # entered the review queue before the user added its company to the
        # never list; that later policy decision must still prevent a send.
        company_policy_store = CompanyPolicyStore(self.data_dir)
        if (
            company_policy_store.path.exists()
            and company_policy_store.get().decision(row.company) is CompanyDecision.NEVER
        ):
            raise SubmissionRefused(
                f"company {row.company!r} is in the never list; submission refused"
            )

        # Rails first, against the file rather than against memory: quota,
        # spacing, circuit breaker and the duplicate check apply to every
        # driver, or they are not rails at all.
        if self.guardrails is not None:
            decision = self.guardrails.preflight(row.job_url, row.job_key)
            if not decision.allowed:
                raise SubmissionRefused(decision.reason)
            if decision.wait_seconds > 0:
                # The randomised spacing between submissions. Honouring it only
                # in the MCP preflight tool meant the console and the runner went
                # out back to back, so two drivers submitting in parallel ignored
                # the interval the rails asked for.
                await asyncio.sleep(min(decision.wait_seconds, MAX_INLINE_WAIT_SECONDS))
                # And then ask again, against the file: the wait is not a licence
                # to proceed. Another driver may have spent the last of the daily
                # cap, or tripped the breaker, while we were sleeping.
                rechecked = self.guardrails.preflight(row.job_url, row.job_key)
                if not rechecked.allowed:
                    raise SubmissionRefused(
                        f"after waiting for the required interval: {rechecked.reason}"
                    )
                if rechecked.wait_seconds > 1.0:
                    raise SubmissionRefused(
                        "the required interval has not elapsed (another driver is "
                        f"between submissions); {rechecked.wait_seconds:.0f}s remain"
                    )

        if not self.ledger.claim(application_id, self.owner):
            raise InvalidTransition(
                ApplicationState(row.state), ApplicationState.SUBMITTING
            )
        if row.state != ApplicationState.WAITING_FOR_APPROVAL.value:
            # Checked before an attempt exists, so a wrong-state call never
            # writes an attempt row at all.
            self.ledger.release_claim(application_id, self.owner)
            raise InvalidTransition(
                ApplicationState(row.state), ApplicationState.SUBMITTING
            )

        attempt = self.ledger.start_attempt(application_id)
        try:
            self.ledger.transition(
                application_id,
                ApplicationState.SUBMITTING,
                expected_state=row.state,
                payload={"grant_id": grant_id, "attempt": attempt.ordinal},
            )

            profile_revision, answers_revision = self.revisions()
            outcome = await execute_authorized_submission(
                controller=controller,
                authorizer=self.authorizer,
                grant_id=grant_id,
                job_key=row.job_key,
                resume=resume,
                route=route or row.route,
                action=action,
                answers_revision=answers_revision,
                profile_revision=profile_revision,
                application_id=application_id,
            )

            target = {
                "verified": ApplicationState.SUBMITTED_VERIFIED,
                "unverified": ApplicationState.SUBMITTED_UNVERIFIED,
                "failed": ApplicationState.FAILED,
            }[outcome.status]

            # The state moves first: whatever happens to the bookkeeping next,
            # the ledger must not be left claiming the application is still mid-
            # submission when a human asks.
            self.ledger.transition(application_id, target)
            try:
                self.ledger.finish_attempt(
                    attempt.id,
                    outcome=outcome.status,
                    detail=outcome.detail,
                    grant_id=grant_id,
                    evidence=outcome.evidence,
                )
                self._record_in_memory(row, outcome)
                self._record_in_rails(outcome)
            except Exception as exc:  # noqa: BLE001 - the submission already happened
                # Bookkeeping failed *after* the external action. The outcome is
                # still the truth of what the employer received, so it is
                # reported as-is and the failure is attached to it -- reporting
                # "failed" here would say nothing was sent when something was.
                outcome.evidence = {
                    **outcome.evidence,
                    "bookkeeping_error": f"{type(exc).__name__}: {exc}",
                }
                outcome.detail = (
                    f"{outcome.detail} [note: recording the attempt failed "
                    f"({type(exc).__name__}); the submission itself is unaffected]"
                )
            return outcome
        except Exception as exc:
            # An exception *after* the grant was spent means the click may have
            # happened: the request could be at the employer right now, so this
            # is unknown, not failed. Only an exception raised before the grant
            # was consumed -- i.e. provably before anything was sent -- lands the
            # attempt as FAILED, which is the one state a retry may start from.
            spent = self.authorizer.peek(grant_id)
            sent_possible = bool(spent and spent.used)
            status = "unverified" if sent_possible else "failed"
            landing = (
                ApplicationState.SUBMITTED_UNVERIFIED
                if sent_possible
                else ApplicationState.FAILED
            )

            current = self.ledger.get(application_id)
            if current is not None and current.state == ApplicationState.SUBMITTING.value:
                self.ledger.transition(application_id, landing)
            self.ledger.finish_attempt(
                attempt.id,
                outcome=status,
                detail=f"{type(exc).__name__}: {exc}",
                grant_id=grant_id,
                evidence={"sent": sent_possible, "exception": type(exc).__name__},
            )
            if sent_possible:
                return SubmitOutcome(
                    status=STATUS_UNVERIFIED,
                    job_key=row.job_key,
                    grant_id=grant_id,
                    detail=(
                        "the submission was pressed but the attempt could not be "
                        f"completed ({type(exc).__name__}: {exc}). Treat it as "
                        "possibly-submitted: do not submit again, reconcile instead."
                    ),
                    evidence={
                        "sent": True,
                        "reconciliation_required": True,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            raise
        finally:
            self.ledger.release_claim(application_id, self.owner)

    def set_route(self, application_id: str, route: str) -> None:
        """Record the route the page actually implies (see `applyops.apply_target`)."""
        self.ledger.set_route(application_id, route)

    async def reconcile(
        self,
        application_id: str,
        *,
        controller: BrowserController,
        action: FinalAction,
        evidence_timeout: float = 8.0,
    ) -> SubmitOutcome:
        """Re-read the page for a possibly-submitted application. Never clicks.

        Reconciliation can only *raise* confidence, and only when the evidence is
        provably about this application: the page on screen has to be the page the
        attempt was made from. A browser left on another posting's success page
        proves nothing here, and the application stays unverified.
        """
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")

        expected_page_identity = ""
        attempts = self.ledger.attempts(application_id)
        if attempts:
            grant = self.authorizer.peek(attempts[-1].grant_id) if attempts[-1].grant_id else None
            if grant is not None:
                expected_page_identity = grant.page_identity

        outcome = await reconcile_submission(
            controller=controller,
            action=action,
            evidence_timeout=evidence_timeout,
            expected_page_identity=expected_page_identity,
            expected_application_id=application_id,
        )
        outcome.job_key = row.job_key
        if outcome.verified and outcome.evidence.get("ownership_proven"):
            self.ledger.transition(
                application_id, ApplicationState.SUBMITTED_VERIFIED,
                payload={"via": "reconciliation"},
            )
            self._record_in_memory(row, outcome, reconcile=True)
            self._record_in_rails(outcome)
        return outcome

    def cancel(self, application_id: str, *, reason: str = "") -> ApplicationRow:
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")
        # Cancelling a submission in flight is not something a local flag can do;
        # the state machine simply refuses the move from SUBMITTING.
        return self.ledger.transition(
            application_id, ApplicationState.CANCELLED, payload={"reason": reason}
        )

    def skip(self, application_id: str, *, reason: str = "") -> ApplicationRow:
        """Record a policy decision that rules a posting out before prepare."""
        row = self.ledger.get(application_id)
        if row is None:
            raise KeyError(f"unknown application {application_id}")
        return self.ledger.transition(
            application_id, ApplicationState.SKIPPED, payload={"reason": reason}
        )

    def recover(self) -> list[ApplicationRow]:
        """Land crashed `SUBMITTING` rows in the safe unknown state."""
        return self.ledger.recover_expired()

    # ── migration ────────────────────────────────────────────────────

    def import_legacy_history(self, memory_json_path: str | Path | None = None) -> dict:
        """Bring old `memory.json` history into the ledger, with a backup first.

        The backup happens *before* anything is read, so a failed import leaves
        the original untouched and a re-run starts from the same data. Legacy
        rows land in `LEGACY_IMPORTED` and are never counted as successes.
        """
        path = Path(memory_json_path) if memory_json_path else (
            self.data_dir / "memory.json"
        )
        if not path.exists():
            return {"imported": 0, "skipped": 0, "backup": ""}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
        shutil.copy2(path, backup)

        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("application_history", []) if isinstance(payload, dict) else []
        result = self.ledger.import_legacy_history(records)
        result["backup"] = str(backup)
        return result

    # ── internals ────────────────────────────────────────────────────

    def _record_in_rails(self, outcome: SubmitOutcome) -> None:
        """Fold the outcome into quota and the breaker, once, from evidence."""
        if self.guardrails is None:
            return
        if outcome.verified:
            self.guardrails.record_outcome(success=True)
        elif outcome.failed:
            self.guardrails.record_outcome(success=False, note=outcome.detail)
        else:
            self.guardrails.record_unverified(note=outcome.detail)

    def _record_in_memory(self, row: ApplicationRow, outcome: SubmitOutcome, *, reconcile: bool = False) -> None:
        """Mirror the outcome into the flywheel's history.

        The flywheel's counters are the public "did it work" numbers, and they
        only move for verified outcomes; an unverified attempt is still recorded
        so the dedupe layer never lets the same posting be submitted twice while
        its result is unknown.
        """
        if self.memory is None:
            return
        self.memory.add_application(
            job_url=row.job_url,
            job_id=row.job_key,
            job_title=row.title,
            company=row.company,
            platform=row.platform,
            apply_route=row.route,
            status="applied",
            outcome=outcome.status,
            grant_id=outcome.grant_id,
            resume_sha256=outcome.evidence.get("resume", {}).get("sha256", row.resume_sha256),
            notes=("reconciled" if reconcile else "") or outcome.detail,
        )
