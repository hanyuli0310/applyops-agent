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

from .apply_target import (
    follow_offsite_apply,
    form_control_count,
    has_apply_control,
    host_of,
    offsite_apply_host,
    open_onsite_application,
)
from .browser import BrowserController
from .company_policy import CompanyDecision, CompanyPolicyStore
from .filling import fill_application_form, resume_for_fill
from .memory import RouteStep
from .platforms.naming import EXTERNAL_ROUTE, is_drivable_route, platform_for_url
from .resume import ResumeError, ResumeRef, resolve_resume
from .service import ApplicationService
from .state_machine import ApplicationState

#: The route label used for an application that had to leave the posting to be
#: completed on the employer's own system. Separate from `external`, which means
#: "we cannot drive this at all".
EXTERNAL_ATS_ROUTE = "external_ats"


@dataclass
class PrepareOutcome:
    state: str
    route: str = ""
    request_id: str = ""
    missing: list[str] = field(default_factory=list)
    detail: str = ""
    fill_report: dict = field(default_factory=dict)
    #: `<platform>/<route>` under which this journey was remembered, empty when
    #: nothing was walked (and therefore nothing learned).
    journey_key: str = ""

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
    allow_offsite_hop: bool = False,
) -> PrepareOutcome:
    """Fill, verify and file the approval request for one application."""
    row = service.get(application_id)
    if row is None:
        raise KeyError(f"unknown application {application_id}")

    # Never-listed companies are blocked at the shared prepare boundary too,
    # so a manual/API/MCP caller cannot bypass the queue runner's policy check.
    company_policy_store = CompanyPolicyStore(service.data_dir)
    if (
        company_policy_store.path.exists()
        and company_policy_store.get().decision(row.company) is CompanyDecision.NEVER
    ):
        detail = f"company {row.company!r} is in the never list"
        service.skip(application_id, reason=detail)
        return PrepareOutcome(
            state=ApplicationState.SKIPPED.value,
            route=row.route,
            detail=detail,
        )

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

    # Keep an already-open Easy Apply modal. LinkedIn renders the modal in
    # place while keeping the posting URL unchanged; navigating to the same
    # URL here silently closes it and leaves the filler staring at the posting
    # page with no file input. Only navigate when the active page is a
    # different posting.
    current_url = (controller.page.url or "").rstrip("/")
    target_url = (row.job_url or "").rstrip("/")
    if current_url != target_url:
        await controller.goto(row.job_url, settle=1.0)

    # The page, not the discovery URL, decides where the application happens.
    # A LinkedIn posting whose Apply control leaves for Greenhouse is filed as
    # `easy_apply` by `resolve_route`; here is where that gets corrected, before
    # anything is filled and before any request is filed on the wrong basis.
    offsite_host = await offsite_apply_host(controller)
    if offsite_host and allow_offsite_hop:
        return await _walk_the_hop(
            service, controller, application_id, resume, offsite_host
        )
    if offsite_host:
        service.set_route(application_id, EXTERNAL_ROUTE)
        detail = (
            f"the application form is not on this page: its Apply control leaves for "
            f"{offsite_host}, and this project has no verified submission path there. "
            "Open the posting and finish it on that site, or skip the posting."
        )
        missing = [f"off-site application on {offsite_host}"]
        service.prepare(
            application_id,
            ready=False,
            detail=detail,
            payload={"off_site_host": offsite_host, "missing": missing},
        )
        return PrepareOutcome(
            state=ApplicationState.WAITING_FOR_INPUT.value,
            route=EXTERNAL_ROUTE,
            missing=missing,
            detail=detail,
        )

    return await _fill_and_file(
        service,
        controller,
        application_id,
        row,
        route,
        resume,
        steps=[RouteStep(ordinal=1, kind="open", detail=row.job_url)],
    )


async def _fill_and_file(
    service: ApplicationService,
    controller: BrowserController,
    application_id: str,
    row,
    route: str,
    resume: ResumeRef,
    steps: list[RouteStep],
) -> PrepareOutcome:
    """Fill the form in front of us, verify it, and file the approval request.

    Shared by the direct path and the two-hop path so that the *ordering* rule
    holds in both: the request is created on the page the form is actually on,
    which is what binds the later submission to the right screen.
    """
    steps.append(RouteStep(ordinal=len(steps) + 1, kind="fill", detail="the form in front of us"))

    report = await fill_application_form(
        controller,
        memory=service.memory,
        answers=service.answers,
        resume=resume,
        application_id=application_id,
        company=row.company,
        job_location=row.location,
    )

    if not report.filled and await has_apply_control(controller):
        # Nothing here was fillable, and the page offers a control that opens the
        # application: the form is behind it. A real LinkedIn Easy Apply posting
        # looks exactly like this -- its own search boxes, the form one click
        # away at `/jobs/view/<id>/apply/` -- which is why "does this page have
        # controls" was the wrong question, and why the first live attempt parked
        # with the filler's "no file input found on this form".
        posted_url = controller.page.url
        opened_url, opened_label = await open_onsite_application(controller)
        if opened_url:
            steps.append(
                RouteStep(ordinal=len(steps) + 1, kind="click", detail=opened_label or "Apply")
            )
            steps.append(
                RouteStep(
                    ordinal=len(steps) + 1,
                    kind="open",
                    detail=f"{posted_url} -> {opened_url}",
                )
            )
            report = await fill_application_form(
                controller,
                memory=service.memory,
                answers=service.answers,
                resume=resume,
                application_id=application_id,
                company=row.company,
                job_location=row.location,
            )

    filled_steps = [
        RouteStep(
            ordinal=0,
            kind="fill",
            detail=f"{outcome.label} <- {outcome.source}",
            selector=outcome.ref,
        )
        for outcome in report.filled
    ]
    if report.resume is not None:
        filled_steps.append(
            RouteStep(
                ordinal=0,
                kind="upload",
                detail=f"resume <- {report.resume.source}",
                selector=report.resume.ref,
            )
        )

    if not report.ready:
        missing = (
            report.unfilled_required
            or report.unreadable
            or [m.label for m in report.mismatched]
            or report.problems
        )
        if not report.filled:
            # A page where nothing at all could be written is not a form with
            # gaps in it; it is not the form.
            detail = (
                "there is no application form on this page, so there is nothing to "
                "fill and nothing that could be submitted from here."
            )
            _remember_blockage(service, row, route, "no form on this page")
            service.prepare(
                application_id,
                ready=False,
                detail=detail,
                payload={"missing": ["no application form on this page"]},
            )
            return PrepareOutcome(
                state=ApplicationState.WAITING_FOR_INPUT.value,
                route=route,
                missing=["no application form on this page"],
                detail=detail,
            )
        _remember_blockage(service, row, route, f"fill: {', '.join(missing) or 'incomplete'}")
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

    # Remember the path that worked, so the next application of this kind does
    # not need a person to walk it again.
    journey_key = _remember_journey(service, row, route, steps + filled_steps, report)

    return PrepareOutcome(
        state=ApplicationState.WAITING_FOR_APPROVAL.value,
        route=route,
        request_id=request.request_id,
        fill_report=report.to_dict(),
        journey_key=journey_key,
    )


async def _walk_the_hop(
    service: ApplicationService,
    controller: BrowserController,
    application_id: str,
    resume: ResumeRef | None,
    offsite_host: str,
) -> PrepareOutcome:
    """Follow the Apply control to the employer's own site, and work there.

    This is the two-hop path: the posting is only a signpost, and the application
    lives somewhere else. Every step taken here is recorded, because the whole
    point of walking it once is that the next one can be automated.
    """
    row = service.get(application_id)
    assert row is not None
    started_at = controller.page.url
    landing_url, clicked = await follow_offsite_apply(controller)
    if not landing_url:
        detail = (
            f"the Apply control points at {offsite_host} but could not be followed; "
            "open the posting and finish the application there"
        )
        _remember_blockage(service, row, EXTERNAL_ATS_ROUTE, f"hop: {offsite_host}")
        service.prepare(
            application_id, ready=False, detail=detail, payload={"missing": ["off-site hop"]}
        )
        return PrepareOutcome(
            state=ApplicationState.WAITING_FOR_INPUT.value,
            route=EXTERNAL_ROUTE,
            missing=[f"off-site application on {offsite_host}"],
            detail=detail,
        )

    landing_host = host_of(landing_url)
    route = EXTERNAL_ATS_ROUTE
    service.set_route(application_id, route)
    steps = [
        RouteStep(ordinal=1, kind="open", detail=started_at),
        RouteStep(ordinal=2, kind="click", detail=clicked or "Apply"),
        RouteStep(ordinal=3, kind="hop", detail=f"{offsite_host} -> {landing_url}"),
    ]

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

    if await form_control_count(controller) == 0:
        detail = (
            f"followed the Apply control to {landing_host}, but there is no application "
            "form there yet -- it may need a login first"
        )
        _remember_blockage(service, row, route, f"hop landing: {landing_host}")
        service.prepare(
            application_id,
            ready=False,
            detail=detail,
            payload={"missing": ["no application form at the landing page"]},
        )
        return PrepareOutcome(
            state=ApplicationState.WAITING_FOR_INPUT.value,
            route=route,
            missing=["no application form at the landing page"],
            detail=detail,
        )

    outcome = await _fill_and_file(
        service, controller, application_id, row, route, resume, steps
    )
    outcome.journey_key = outcome.journey_key or ""
    return outcome


def _journey_key(row, route: str) -> tuple[str, str]:
    """Where a journey of this kind is filed: (`Generic` when the host is unknown).

    `Generic` mirrors the convention the flywheel already uses for platform
    buckets, so route knowledge and selector knowledge land in the same place.
    """
    platform = platform_for_url(row.job_url)
    if platform in {"", "Unknown"}:
        platform = "Generic"
    return platform, route


def _remember_journey(
    service: ApplicationService, row, route: str, steps: list[RouteStep], report
) -> str:
    """Write the path that worked, and return the key it was filed under."""
    if service.memory is None:
        return ""
    platform, route_name = _journey_key(row, route)
    ordered = [
        RouteStep(ordinal=index + 1, kind=s.kind, detail=s.detail, selector=s.selector)
        for index, s in enumerate(steps)
        if s.kind != "fill" or s.detail != "the form in front of us"
    ]
    signature = [
        f"apply:{row.platform or 'unknown'}",
        f"route:{route_name}",
        f"fields:{len(report.filled)}",
    ]
    service.memory.record_journey(
        platform,
        route_name,
        ordered,
        entry_signature=signature,
        notes=f"walked for {row.job_url}",
    )
    return f"{platform}/{route_name}"


def _remember_blockage(service: ApplicationService, row, route: str, where: str) -> None:
    """The other half of the flywheel: where this path died, and why."""
    if service.memory is None:
        return
    platform, route_name = _journey_key(row, route)
    service.memory.record_route_blockage(platform, route_name, where)


def resume_for(service: ApplicationService) -> ResumeRef | None:
    """The configured resume, or None (the caller parks the application)."""
    return resume_for_fill(service.memory) if service.memory else None
