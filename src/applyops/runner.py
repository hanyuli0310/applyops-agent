"""The supervised queue runner.

What a pass does, in order, and why the order is the safety design:

1. **Recover** crashed `SUBMITTING` rows (M2) -- before anything new starts, the
   books are honest.
2. **Reconcile** unverified results -- again read-only; an unknown result is
   investigated, never re-sent.
3. **Prepare** queued applications: open the posting, read the form, file an
   approval request. Anything that would need guessing (missing resume, an
   unreadable field, an answer the scopes cannot supply) parks the application
   in `WAITING_FOR_INPUT` with the reason recorded -- parking is the feature.
4. **Submit only what a human has already approved.** A pass never approves its
   own work; the grant for each submission must already exist, minted by a
   person (CLI or UI). Limited auto mode (`AutoPolicy`) does not change that --
   it only bounds how many pre-approved submissions a pass may consume, on which
   platforms, until when.

Pause/resume/stop are checked *between* applications and before each submit.
Nothing interrupts a submission in flight; the ledger's claim and the state
machine own that boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .answers import AnswerStore
from .browser import BrowserController
from .concurrency import atomic_write_json, data_lock_path, read_json
from .evidence import detect_final_action
from .filling import fill_application_form, resume_for_fill
from .memory import MemoryStore
from .resume import ResumeError, ResumeRef, resolve_resume
from .service import ApplicationService
from .state_machine import ApplicationState
from .submission import FinalAction, SubmissionRefused, SubmitOutcome

# ── policy ───────────────────────────────────────────────────────────


@dataclass
class AutoPolicy:
    """The bounds of what a pass may do without a new human decision.

    Defaults are the safe ones: disabled, zero budget. Enabled or not, a pass
    still cannot mint grants -- the policy only says how many *existing* grants
    a pass may spend, on which platforms, until when.
    """

    enabled: bool = False
    max_applications: int = 0
    allowed_platforms: tuple[str, ...] = ()
    expires_at_epoch: float = 0.0
    updated_at: str = ""
    updated_by: str = ""

    @property
    def expired(self) -> bool:
        return self.expires_at_epoch > 0 and time.time() >= self.expires_at_epoch

    @property
    def usable(self) -> bool:
        return self.enabled and not self.expired and self.max_applications > 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict) -> AutoPolicy:
        known = {k: v for k, v in payload.items() if k in cls.__annotations__}
        return cls(**known)


class PolicyStore:
    """One policy per data dir, behind a lock. Deliberately boring."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "auto_policy.json"
        self._lock_path = data_lock_path(self.data_dir, "auto_policy")

    def get(self) -> AutoPolicy:
        payload = read_json(self.path, default={}) or {}
        return AutoPolicy.from_dict(payload)

    def set(self, policy: AutoPolicy) -> AutoPolicy:
        with __import__("applyops.concurrency", fromlist=["FileLock"]).FileLock(self._lock_path):
            atomic_write_json(self.path, policy.to_dict())
        return policy


# ── runner ───────────────────────────────────────────────────────────


@dataclass
class PassReport:
    """What one pass did, item by item. Nothing is summarised into mush."""

    recovered: list[str] = field(default_factory=list)
    reconciled: list[dict] = field(default_factory=list)
    prepared: list[dict] = field(default_factory=list)
    submitted: list[dict] = field(default_factory=list)
    parked: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    stopped_reason: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class QueueRunner:
    """Drives the queue under the policy. One runner per process."""

    def __init__(
        self,
        service: ApplicationService,
        *,
        memory: MemoryStore | None = None,
        answers: AnswerStore | None = None,
        policy_store: PolicyStore | None = None,
    ):
        self.service = service
        self.memory = memory or service.memory
        self.answers = answers or AnswerStore(service.data_dir)
        self.policy_store = policy_store or PolicyStore(service.data_dir)
        self._paused = False
        self._stopped = False

    # ── controls ─────────────────────────────────────────────────────

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stopped = True

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ── the pass ─────────────────────────────────────────────────────

    async def run_pass(self, controller: BrowserController) -> PassReport:
        report = PassReport()

        # 1. Books first: crashed submissions become unknowns, never re-runs.
        for row in self.service.recover():
            report.recovered.append(row.id)

        # 2. Investigate unknown results without touching submit.
        for row in self.service.list(ApplicationState.SUBMITTED_UNVERIFIED.value):
            if self._stopped:
                report.stopped_reason = "stopped by operator"
                return report
            outcome = await self.service.reconcile(
                row.id,
                controller=controller,
                action=FinalAction(),
                evidence_timeout=4.0,
            )
            report.reconciled.append(
                {"application_id": row.id, "status": outcome.status}
            )

        # 3. Prepare queued work under the policy.
        policy = self.policy_store.get()
        budget = policy.max_applications if policy.usable else 0
        for row in self.service.list(ApplicationState.QUEUED.value):
            if self._stopped:
                report.stopped_reason = "stopped by operator"
                break
            if self._paused:
                report.stopped_reason = "paused by operator"
                break

            if policy.allowed_platforms and row.platform not in policy.allowed_platforms:
                report.refused.append(
                    {
                        "application_id": row.id,
                        "reason": f"platform {row.platform!r} is outside the policy",
                    }
                )
                continue

            try:
                prepared = await self._prepare_one(row.id, controller)
            except ResumeError as exc:
                self.service.prepare(row.id, ready=False, detail=str(exc))
                report.parked.append(
                    {"application_id": row.id, "reason": f"resume: {exc}"}
                )
                continue

            if prepared.get("state") == ApplicationState.WAITING_FOR_INPUT.value:
                report.parked.append(
                    {
                        "application_id": row.id,
                        "reason": prepared.get("detail", "needs human input"),
                    }
                )
                continue

            report.prepared.append(
                {
                    "application_id": row.id,
                    "request_id": prepared.get("request_id", ""),
                }
            )

        # 4. Submit only pre-approved work, within budget -- from this pass's
        #    preparations AND anything still waiting from an earlier one.
        if budget > 0 and not self._stopped and not self._paused:
            waiting = self.service.list(ApplicationState.WAITING_FOR_APPROVAL.value)
            for row in waiting:
                if budget <= 0:
                    report.stopped_reason = "policy budget spent"
                    break
                if self._stopped:
                    report.stopped_reason = "stopped by operator"
                    break
                if self._paused:
                    report.stopped_reason = "paused by operator"
                    break

                grant_id = self._pending_grant_for(row.job_key)
                if not grant_id:
                    continue  # waiting for a human; that is the default, not a failure
                try:
                    outcome = await self._submit_one(row.id, grant_id, controller, policy)
                except SubmissionRefused as exc:
                    report.refused.append(
                        {"application_id": row.id, "reason": exc.reason}
                    )
                    continue
                budget -= 1
                report.submitted.append(
                    {"application_id": row.id, "status": outcome.status}
                )

        if policy.usable and budget <= 0 and report.submitted:
            report.stopped_reason = report.stopped_reason or "policy budget spent"

        return report

    # ── internals ────────────────────────────────────────────────────

    def _resume(self) -> ResumeRef:
        configured = self.memory.profile.value("resume_path") if self.memory else None
        return resolve_resume(configured)

    async def _prepare_one(self, application_id: str, controller: BrowserController) -> dict:
        """Open the posting, **fill it**, then file the approval request.

        The filling step is not optional. A runner that only reads the form asks
        a human to approve an empty application, which is worse than asking for
        nothing: the approval is real and the submission it authorizes is not.

        Raises `ResumeError` when no resume is configured -- the caller parks the
        application, because attaching nothing is not a decision this code may
        make.
        """
        resume = self._resume()  # raises ResumeError -> parked by the caller
        row = self.service.get(application_id)
        assert row is not None

        await controller.goto(row.job_url, settle=1.0)
        report = await fill_application_form(
            controller,
            memory=self.memory,
            answers=self.answers,
            resume=resume_for_fill(self.memory) or resume,
            application_id=application_id,
            company=row.company,
        )
        if not report.ready:
            missing = (
                report.unfilled_required
                or report.unreadable
                or [m.label for m in report.mismatched]
                or report.problems
            )
            self.service.prepare(
                application_id,
                ready=False,
                detail=f"needs input before it can be submitted: {', '.join(missing)}",
                payload={"fill_report": report.to_dict()},
            )
            return {
                "state": ApplicationState.WAITING_FOR_INPUT.value,
                "detail": f"missing: {', '.join(missing)}",
            }

        snapshot = await controller.field_snapshot()
        profile_revision, answers_revision = self.service.revisions()
        request = self.service.authorizer.create_request(
            job_key=row.job_key,
            job_url=row.job_url,
            route=row.route,
            platform=row.platform,
            fields=snapshot,
            resume_filename=resume.filename,
            resume_sha256=resume.sha256,
            answers_revision=answers_revision,
            profile_revision=profile_revision,
            application_id=application_id,
            page_url=controller.page.url,
            requested_by="queue_runner",
        )
        self.service.prepare(application_id, ready=True, detail=f"request {request.request_id}")
        return {
            "state": ApplicationState.WAITING_FOR_APPROVAL.value,
            "request_id": request.request_id,
        }

    def _pending_grant_for(self, job_key: str) -> str:
        """A grant a human minted for this job, that no pass has spent yet."""
        for grant in self.service.authorizer.pending():
            if grant.job_key == job_key and grant.source in {
                "cli_human",
                "local_ui_human",
            }:
                return grant.grant_id
        return ""

    async def _submit_one(
        self,
        application_id: str,
        grant_id: str,
        controller: BrowserController,
        policy: AutoPolicy,
    ) -> SubmitOutcome:
        row = self.service.get(application_id)
        assert row is not None

        # Go back to *this* application's page and restore the form before
        # submitting. Two reasons, both about the same honesty:
        #
        # - the grant is bound to that page, so submitting from whatever page
        #   the previous iteration left loaded would be refused (correctly) and
        #   the pass would look broken;
        # - the approval covers the values that were on the form, so the form has
        #   to be put back into that state from the same sources. If it cannot be
        #   reproduced, the digest will not match and the grant refuses rather
        #   than sending something nobody saw.
        await controller.goto(row.job_url, settle=1.0)
        report = await fill_application_form(
            controller,
            memory=self.memory,
            answers=self.answers,
            resume=self._resume(),
            application_id=application_id,
            company=row.company,
        )
        if not report.ready:
            raise SubmissionRefused(
                "the form could not be restored to its approved state: "
                + ", ".join(report.unfilled_required or report.unreadable or ["unknown"])
            )

        action, detail = await detect_final_action(controller)
        if action is None:
            raise SubmissionRefused(detail)
        outcome = await self.service.submit(
            application_id,
            grant_id=grant_id,
            controller=controller,
            resume=self._resume(),
            action=action,
        )
        return outcome
