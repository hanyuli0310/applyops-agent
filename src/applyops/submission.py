"""The single path through which a final external submit may happen.

M1 requirement: *the final external Submit must sit behind the authorization
boundary*, and *nothing that was not verified is a success*.

This module is that path. Order of operations matters and is not negotiable:

1. **Inspect** the target: is it structurally a final submit?
2. **Snapshot** the live form -- read from the DOM, now, not what the harness
   thinks it typed.
3. **Verify the grant** against job key, that snapshot, the resume digest and the
   fact revisions. A changed form means the approval no longer applies.
4. **Consume** the grant inside its file lock, so two callers cannot both win.
5. **Click** -- the first and only external side effect.
6. **Collect evidence** and classify: `verified`, `unverified`, or `failed`.

Note where step 4 sits: *before* the click. Spending the grant first means a
second concurrent submit finds nothing to spend, which is the property that
matters. The cost is that a grant whose click then fails is burned and the user
has to approve again -- which is the correct trade, because the alternative is
a duplicate application.

There is deliberately **no retry here**. When the result is `unverified`, the
request probably reached the employer and possibly did not; clicking Submit
again is how one application becomes two. The only thing to do is reconcile --
re-read the page and find out -- which is what :func:`reconcile` is for.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .action_policy import ClickClass, ClickDecision, decide_click
from .authorization import SubmissionAuthorizer, page_identity_of, snapshot_digest
from .browser import BrowserController
from .resume import ResumeRef
from .verification import Verification

#: How long to wait for the page to say something conclusive after the click.
DEFAULT_EVIDENCE_TIMEOUT = 12.0
POLL_INTERVAL = 0.5

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_FAILED = "failed"


class SubmissionRefused(Exception):
    """The submitted would have been sent without authorization."""

    def __init__(self, reason: str, *, manual_required: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.manual_required = manual_required


@dataclass
class SubmitOutcome:
    """What happened, and the evidence for believing it."""

    status: str = STATUS_UNVERIFIED
    job_key: str = ""
    grant_id: str = ""
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "job_key": self.job_key,
            "grant_id": self.grant_id,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FinalAction:
    """Where the final submit button is, and how we know it was pressed."""

    ref: str = ""
    name: str = ""
    success_patterns: tuple[str, ...] = ()


async def _wait_for_evidence(
    controller: BrowserController, patterns: tuple[str, ...], timeout: float
) -> tuple[bool, str, str]:
    """Poll the page for an explicit success signal. Returns (found, pattern, url)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found, pattern = await controller.page_indicates(list(patterns))
        if found:
            return True, pattern, controller.page.url
        await asyncio.sleep(POLL_INTERVAL)
    return False, "", controller.page.url


async def execute_authorized_submission(
    *,
    controller: BrowserController,
    authorizer: SubmissionAuthorizer,
    grant_id: str,
    job_key: str,
    resume: ResumeRef,
    route: str,
    action: FinalAction,
    answers_revision: str = "",
    profile_revision: str = "",
    application_id: str = "",
    route_supported: bool = True,
    evidence_timeout: float = DEFAULT_EVIDENCE_TIMEOUT,
) -> SubmitOutcome:
    """Perform one final submit, or refuse. Authorization first, evidence after."""
    facts, inspect_error = await controller.inspect_target(ref=action.ref, name=action.name)
    if facts is None:
        raise SubmissionRefused(f"cannot identify the final action: {inspect_error}")

    decision: ClickDecision = decide_click(
        facts, authorized=False, route_supported=route_supported
    )
    if decision.target_class is not ClickClass.FINAL_SUBMIT:
        raise SubmissionRefused(
            f"{facts.label!r} does not resolve to a final submit "
            f"(classified {decision.target_class.value}). "
            "Point the grant at the control that actually ends the application."
        )
    if decision.manual_required:
        raise SubmissionRefused(decision.reason, manual_required=True)

    snapshot = await controller.field_snapshot()
    # `page_url` is what makes "the browser is on the right posting" checkable.
    # Values alone cannot do it: two postings on one ATS render identical forms.
    verdict = authorizer.verify(
        grant_id,
        job_key=job_key,
        fields=snapshot,
        resume_sha256=resume.sha256,
        answers_revision=answers_revision,
        profile_revision=profile_revision,
        route=route,
        application_id=application_id,
        page_url=controller.page.url,
    )
    if not verdict.ok:
        # Snapshot drift is not a refusal to authorize -- it is the news that the
        # authorization stopped applying. Reporting it as a distinct "nothing was
        # sent" outcome rather than throwing keeps the distinction visible: nobody
        # tried to submit anything, and the caller should re-review and re-ask.
        if verdict.grant is not None and (
            "no longer matches" in verdict.reason or "browser is on" in verdict.reason
        ):
            return SubmitOutcome(
                status=STATUS_FAILED,
                job_key=job_key,
                grant_id=grant_id,
                detail=(
                    "the form no longer matches what was approved; nothing was "
                    "sent. Re-review the updated form and request approval again."
                ),
                evidence={"sent": False, "reason": verdict.reason},
            )
        raise SubmissionRefused(verdict.reason)

    consumed = authorizer.consume(grant_id)
    if not consumed.ok:
        raise SubmissionRefused(consumed.reason)

    # The form must still look exactly like what was approved at the instant the
    # click happens. Re-reading here rather than trusting step 3 is what closes
    # the window between "verified" and "sent" -- the grant could have been
    # verified a while ago, and a page can rerender underneath it.
    pre_snapshot = await controller.field_snapshot()
    pre_digest = snapshot_digest(
        fields=pre_snapshot,
        resume_sha256=resume.sha256,
        answers_revision=answers_revision,
        profile_revision=profile_revision,
        route=route,
    )
    if pre_digest != verdict.grant.snapshot_digest:
        # Grant is spent and must stay spent, but the external side effect has
        # NOT happened and will not: return before any click.
        return SubmitOutcome(
            status=STATUS_FAILED,
            job_key=job_key,
            grant_id=grant_id,
            detail=(
                "the form changed after approval and before submission; nothing was "
                "sent. Re-review the updated form and issue a new grant."
            ),
            evidence={
                "sent": False,
                "approved_digest": verdict.grant.snapshot_digest[:12],
                "observed_digest": pre_digest[:12],
            },
        )

    result = await _click_final(controller, action)
    if not result.get("clicked"):
        return SubmitOutcome(
            status=STATUS_FAILED,
            job_key=job_key,
            grant_id=grant_id,
            detail=f"the final click did not happen: {result.get('error', 'unknown')}",
            evidence={"click": result, "sent": False},
        )

    if not result.get("settled", True):
        # The click landed but the page never settled afterwards. Anything could
        # have happened downstream, so the only defensible verdict is unknown --
        # and it must not be improved into a success by waiting longer.
        return SubmitOutcome(
            status=STATUS_UNVERIFIED,
            job_key=job_key,
            grant_id=grant_id,
            detail=(
                "the submission was pressed but the page never settled, so it is "
                "unknown whether the employer received it. Do not submit again; "
                "reconcile instead."
            ),
            evidence={
                "click": result,
                "sent": True,
                "reconciliation_required": True,
                "resume": resume.to_dict(),
            },
        )

    if action.success_patterns:
        try:
            found, pattern, url = await _wait_for_evidence(
                controller, action.success_patterns, evidence_timeout
            )
        except Exception as exc:  # noqa: BLE001 - the submit may already have happened
            # The request has left the machine and we could not read the answer
            # (the tab closed, the browser went away, the page crashed). That is
            # the definition of unknown: reporting a failure here would invite a
            # retry of something the employer may already have received.
            return SubmitOutcome(
                status=STATUS_UNVERIFIED,
                job_key=job_key,
                grant_id=grant_id,
                detail=(
                    "the submission was sent but the page could not be read "
                    f"afterwards ({type(exc).__name__}: {exc}). Treat it as "
                    "possibly-submitted: do not submit again, reconcile instead."
                ),
                evidence={
                    "sent": True,
                    "reconciliation_required": True,
                    "evidence_error": f"{type(exc).__name__}: {exc}",
                    "resume": resume.to_dict(),
                },
            )
    else:
        # No success signal configured for this route: saying "verified" would be
        # inventing evidence, so the only honest verdict is unknown.
        found, pattern, url = False, "", controller.page.url

    if found:
        return SubmitOutcome(
            status=STATUS_VERIFIED,
            job_key=job_key,
            grant_id=grant_id,
            detail="the page reported a successful submission",
            evidence={
                "matched_text": pattern,
                "url": url,
                "resume": resume.to_dict(),
                "sent": True,
            },
        )

    return SubmitOutcome(
        status=STATUS_UNVERIFIED,
        job_key=job_key,
        grant_id=grant_id,
        detail=(
            "the submit was sent but the page never confirmed it. Treat this as "
            "possibly-submitted: do not submit again, reconcile instead."
        ),
        evidence={
            "url": controller.page.url,
            "resume": resume.to_dict(),
            "sent": True,
            "reconciliation_required": True,
        },
    )


async def _click_final(controller: BrowserController, action: FinalAction) -> dict:
    """Press the final control. Returns the raw click result as a dict."""
    try:
        pages_before = list(controller.context.pages)
        if action.ref:
            from . import locator as locator_module

            element = await locator_module.resolve_ref(controller.page, action.ref)
            if element is None:
                return {"clicked": False, "error": f"unresolvable ref {action.ref!r}"}
        else:
            from . import locator as locator_module

            element = await locator_module.find_button(controller.page, action.name)
            if element is None:
                return {"clicked": False, "error": f"no button named {action.name!r}"}
        await element.click(timeout=15000, no_wait_after=True)
        await asyncio.sleep(0.6)
        new_tab = await controller._adopt_new_tabs(pages_before)
        return {"clicked": True, "settled": True, "new_tab": new_tab, "url": controller.page.url}
    except Exception as exc:  # noqa: BLE001 - any click failure is data, not a crash
        # A click that reached the page and then timed out while the browser
        # waited for the navigation cannot be called "failed": the request has
        # already left the machine. The honest word for it is unknown, and it is
        # the exact case `submission_unknown` exists for.
        #
        # This is why the click uses `no_wait_after`: waiting here would hang on
        # a slow employer and *then* throw, after the request had been sent.
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, Exception) and ("Timeout" in message or "navigat" in message.lower()):
            return {"clicked": True, "settled": False, "error": message}
        return {"clicked": False, "settled": False, "error": message}


async def reconcile_submission(
    *,
    controller: BrowserController,
    action: FinalAction,
    evidence_timeout: float = DEFAULT_EVIDENCE_TIMEOUT,
    expected_page_identity: str = "",
    expected_application_id: str = "",
) -> SubmitOutcome:
    """Find out what happened to a possibly-submitted application.

    Read-only by construction: it never clicks, never re-submits, never presses
    anything. It answers from whatever the page says right now, and if the page
    still says nothing, the honest answer remains `unverified`.

    **Evidence has to be about this attempt.** A confirmation page proves a
    submission happened *somewhere*; it does not say which application it belongs
    to, and a browser that has moved on to another posting's success page will
    happily show one. So when the caller can name the page the attempt was made
    from, that page must be the one on screen before any success text counts.
    Without that check, application A is confirmed by application B's receipt --
    a fabricated success produced without forging anything.
    """
    live_identity = page_identity_of(controller.page.url)
    observed = {
        "url": controller.page.url,
        "observed_page_identity": live_identity,
        "expected_page_identity": expected_page_identity,
        "application_id": expected_application_id,
        "reconciled": True,
    }
    if expected_page_identity and live_identity != expected_page_identity:
        return SubmitOutcome(
            status=STATUS_UNVERIFIED,
            detail=(
                f"the browser is on {live_identity!r}, but this attempt was made from "
                f"{expected_page_identity!r}. This page's evidence is not about this "
                "application, so ownership cannot be proven; it stays unverified. "
                "Open the application's own page and reconcile again."
            ),
            evidence={**observed, "ownership_proven": False},
        )

    if action.success_patterns:
        found, pattern, url = await _wait_for_evidence(
            controller, action.success_patterns, evidence_timeout
        )
        if found:
            return SubmitOutcome(
                status=STATUS_VERIFIED,
                detail="reconciled: the page reports a successful submission",
                evidence={
                    "matched_text": pattern,
                    "url": url,
                    "reconciled": True,
                    "ownership_proven": bool(expected_page_identity),
                    **observed,
                },
            )
    return SubmitOutcome(
        status=STATUS_UNVERIFIED,
        detail=(
            "still no confirmation on the page. Nothing was resubmitted. Check the "
            "employer's site or inbox directly, then record the real result."
        ),
        evidence={**observed, "ownership_proven": bool(expected_page_identity)},
    )


def verification_from_status(status: str) -> str:
    """Map a submission status onto the shared verification vocabulary."""
    if status == STATUS_VERIFIED:
        return Verification.VERIFIED.value
    if status == STATUS_FAILED:
        return Verification.MISMATCH.value
    return Verification.UNVERIFIABLE.value
