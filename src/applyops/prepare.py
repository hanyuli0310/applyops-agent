"""Preparing an application: the one implementation, for every driver.

Before this module there were three: the console had one inline in a route
handler, the runner had another, and MCP had none at all -- which is why the MCP
flow could enqueue an application and then be refused at submit time, with no
public way to move it forward.

Preparing means, in this order and for this reason:

1. **The application's own route decides whether we may drive it.** A route we
   have no verified submission path for (`external`) parks the application with
   that reason instead of being quietly treated as the demo route.
2. **Open the posting's own URL.**
3. **Fill** from the profile and scoped answers, verifying every read-back.
4. **Attach the configured resume** and verify the input holds it.
5. **Park** if anything is unresolved, unreadable or mismatched -- naming the
   fields, so the console can ask for exactly those.
6. **Only then** snapshot the form and file the approval request, carrying the
   application id, the page identity and the current revisions.

Every caller gets the same `PrepareOutcome`, and none of them can skip a step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .browser import BrowserController
from .filling import fill_application_form, resume_for_fill
from .platforms.naming import is_drivable_route
from .resume import ResumeError, ResumeRef, resolve_resume
from .service import ApplicationService
from .state_machine import ApplicationState


@dataclass
class PrepareOutcome:
    state: str
    route: str = ""
    request_id: str = ""
    missing: list[str] = field(default_factory=list)
    detail: str = ""
    fill_report: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == ApplicationState.WAITING_FOR_APPROVAL.value

    @property
    def filled(self) -> int:
        """How many fields were written and verified (0 when it parked early)."""
        return len(self.fill_report.get("filled", []))

    def to_dict(self) -> dict:
        payload = dict(self.__dict__)
        # The count is what a caller wants to show a person ("filled 6 fields,
        # missing 1"); the full report stays available under `fill_report`.
        payload["filled"] = self.filled
        return payload


class PrepareRefused(Exception):
    """This application cannot be prepared at all, and why."""


async def prepare_application(
    service: ApplicationService,
    controller: BrowserController,
    application_id: str,
    *,
    resume: ResumeRef | None = None,
) -> PrepareOutcome:
    """Fill, verify and file the approval request for one application."""
    row = service.get(application_id)
    if row is None:
        raise KeyError(f"unknown application {application_id}")

    if row.state == ApplicationState.SUBMITTED_UNVERIFIED.value:
        raise PrepareRefused(
            "this application may already have been submitted; reconcile it "
            "instead of preparing it again"
        )
    if not row.state in {
        ApplicationState.QUEUED.value,
        ApplicationState.PREPARING.value,
        ApplicationState.WAITING_FOR_INPUT.value,
        ApplicationState.WAITING_FOR_APPROVAL.value,
        ApplicationState.FAILED.value,
    }:
        raise PrepareRefused(f"application is in {row.state}, which is not preparable")

    route = row.route or ""
    if not is_drivable_route(route):
        missing = [f"route:{route}"]
        service.prepare(
            application_id,
            ready=False,
            detail=(
                f"route {route!r} has no verified submission path; this posting must "
                "be finished by hand"
            ),
            payload={"missing": missing, "route": route},
        )
        return PrepareOutcome(
            state=ApplicationState.WAITING_FOR_INPUT.value,
            route=route,
            missing=missing,
            detail="route cannot be driven automatically",
        )

    if resume is None:
        try:
            resume = resolve_resume(
                service.memory.profile.value("resume_path") if service.memory else None
            )
        except ResumeError as exc:
            service.prepare(
                application_id,
                ready=False,
                detail=f"needs input before it can be submitted: resume ({exc})",
                payload={"missing": ["resume"]},
            )
            return PrepareOutcome(
                state=ApplicationState.WAITING_FOR_INPUT.value,
                route=route,
                missing=["resume"],
                detail=str(exc),
            )

    await controller.goto(row.job_url, settle=1.0)

    report = await fill_application_form(
        controller,
        memory=service.memory,
        answers=service.answers,
        resume=resume,
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
        service.prepare(
            application_id,
            ready=False,
            detail=f"needs input before it can be submitted: {', '.join(missing)}",
            payload={"fill_report": report.to_dict(), "missing": missing},
        )
        return PrepareOutcome(
            state=ApplicationState.WAITING_FOR_INPUT.value,
            route=route,
            missing=missing,
            detail="the form is not complete",
            fill_report=report.to_dict(),
        )

    snapshot = await controller.field_snapshot()
    profile_revision, answers_revision = service.revisions()
    request = service.authorizer.create_request(
        job_key=row.job_key,
        job_url=row.job_url,
        route=route,
        platform=row.platform,
        fields=snapshot,
        resume_filename=resume.filename,
        resume_sha256=resume.sha256,
        answers_revision=answers_revision,
        profile_revision=profile_revision,
        application_id=application_id,
        page_url=controller.page.url,
        requested_by="prepare",
    )
    service.prepare(
        application_id,
        ready=True,
        detail=f"request {request.request_id} filed for approval",
        payload={"route": route},
    )
    return PrepareOutcome(
        state=ApplicationState.WAITING_FOR_APPROVAL.value,
        route=route,
        request_id=request.request_id,
        fill_report=report.to_dict(),
    )


def resume_for(service: ApplicationService) -> ResumeRef | None:
    """The configured resume, or None (the caller parks the application)."""
    return resume_for_fill(service.memory) if service.memory else None
