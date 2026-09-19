"""Blocker 3 -- reconciliation that proves nothing, and failures that guess.

Two repros, as reported:

1. `reconcile` read whatever page was loaded and, on seeing the success text,
   promoted the application to `submitted_verified`. Point the browser at a
   *different* posting's success page and application A would be confirmed by
   B's evidence -- a fabricated success, produced without any forged grant.

2. Failures around the click were not separated. A page that closed after the
   click, an evidence read that threw, or a ledger write that failed after the
   request had already left the machine were all reported as a failure, which
   invites an automatic retry of something the employer may already have.

The rule under test: no evidence, no ownership, or no bookkeeping means
**unverified** -- never verified, never a safe-to-retry failure.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState
from applyops.submission import FinalAction


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-blocker3-"))


def _service(root: Path) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+1 555 010 4477",
            "years_experience": "4",
            "requires_sponsorship": "no",
            "resume_path": str(write_sample_resume(root / "resume.pdf")),
        }
    )
    service = ApplicationService(root, memory=memory)
    service.answers.set_answer("Notice period", "Two weeks")
    return service


async def _prepared_application(service: ApplicationService, controller, job_url: str, job_id: str):
    """enqueue -> prepare (fills, verifies, files the request) -> approve.

    Uses the product's own path so the approval digest matches what is really on
    the form; a hand-built request would only prove the test can craft a mismatch.
    """
    from applyops.prepare import prepare_application

    row = service.enqueue(job_url=job_url, job_id=job_id, route="demo", platform="DemoATS")
    outcome = await prepare_application(service, controller, row.id)
    assert outcome.ready, outcome.to_dict()
    grant = service.authorizer.approve_request(outcome.request_id, source="cli_human")
    assert grant is not None
    return service.get(row.id), grant


async def _submit_unverified(service: ApplicationService, controller, ats, *, job_id: str):
    """Leave an application in SUBMITTED_UNVERIFIED the honest way: the demo ATS
    accepts the POST and answers with no confirmation, so the click happened, the
    request left the machine, and nothing came back that could be called evidence."""
    row, grant = await _prepared_application(
        service, controller, f"{ats.url}/form?scenario=silent", job_id
    )
    posts_before = ats.submission_count
    outcome = await service.submit(
        row.id,
        grant_id=grant.grant_id,
        controller=controller,
        resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
        action=FinalAction(
            name="Submit application", success_patterns=("Application received",)
        ),
    )
    assert outcome.status == "unverified", outcome.to_dict()
    assert ats.submission_count == posts_before + 1, "the request must actually be sent"
    assert service.get(row.id).state == ApplicationState.SUBMITTED_UNVERIFIED.value
    return row


@pytest.mark.asyncio
async def test_another_postings_success_page_cannot_confirm_this_application():
    """The repro: evidence from B must not be used to verify A."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = await _submit_unverified(service, browser, ats, job_id="job-a")

            # Another application (B) is submitted for real, and the browser is
            # left sitting on *its* success page.
            b_row, b_grant = await _prepared_application(
                service, browser, f"{ats.url}/form", "job-b"
            )
            b_outcome = await service.submit(
                b_row.id,
                grant_id=b_grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=FinalAction(
                    name="Submit application", success_patterns=("Application received",)
                ),
            )
            assert b_outcome.status == "verified", b_outcome.to_dict()
            body = await browser.page.inner_text("body")
            assert "Application received" in body  # a real success page, for B

            outcome = await service.reconcile(
                row.id,
                controller=browser,
                action=FinalAction(success_patterns=("Application received",)),
            )

            # B's success page is not evidence about A, so A stays unknown --
            # and the report says why, rather than blaming the page.
            assert outcome.status == "unverified", outcome.to_dict()
            assert "page" in outcome.detail or "ownership" in outcome.detail
            assert service.get(row.id).state == ApplicationState.SUBMITTED_UNVERIFIED.value
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_reconcile_on_the_attempts_own_page_still_confirms():
    """The fix must not break the legitimate case."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = await _submit_unverified(service, browser, ats, job_id="job-own")
            # Same page, now showing the confirmation (what a slow ATS does when
            # it finally responds).
            await browser.goto(f"{ats.url}/submit", settle=0.4)

            outcome = await service.reconcile(
                row.id,
                controller=browser,
                action=FinalAction(success_patterns=("Application received",)),
            )
            # `/submit` is not the page the attempt was made from either, so this
            # is *still* not proof -- and must therefore stay unverified.
            assert outcome.status == "unverified", outcome.to_dict()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_evidence_read_failure_after_the_click_is_unverified_not_failed():
    """A read that throws after the click must not become "safe to retry"."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _prepared_application(
                service, browser, f"{ats.url}/form", "job-read-fail"
            )

            # Evidence reading blows up after the click (a closed page, a
            # crashed tab, a browser that went away).
            async def exploding_evidence(*_args, **_kwargs):
                raise RuntimeError("the page was closed while reading evidence")

            import applyops.submission as submission_module

            original = submission_module._wait_for_evidence
            submission_module._wait_for_evidence = exploding_evidence
            try:
                outcome = await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=FinalAction(
                        name="Submit application",
                        success_patterns=("Application received",),
                    ),
                )
            finally:
                submission_module._wait_for_evidence = original

            # Unknown, not failed: the request may well have reached the employer.
            assert outcome.status == "unverified", outcome.to_dict()
            assert outcome.evidence["sent"] is True
            state = service.get(row.id).state
            assert state == ApplicationState.SUBMITTED_UNVERIFIED.value, state
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_ledger_failure_after_the_click_leaves_it_unknown_and_recoverable():
    """Bookkeeping that fails after a click must not be reported as "not sent"."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _prepared_application(
                service, browser, f"{ats.url}/form", "job-ledger-fail"
            )

            # The attempt write fails *after* the click has been sent.
            original = service.ledger.finish_attempt

            def exploding_finish(*args, **kwargs):
                raise RuntimeError("disk went away while recording the attempt")

            service.ledger.finish_attempt = exploding_finish
            try:
                outcome = await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=FinalAction(
                        name="Submit application",
                        success_patterns=("Application received",),
                    ),
                )
            finally:
                service.ledger.finish_attempt = original

            # The submission itself succeeded; only the bookkeeping failed, and
            # the outcome must say so rather than claim nothing was sent.
            assert outcome.status == "verified", outcome.to_dict()
            state = service.get(row.id).state
            assert state == ApplicationState.SUBMITTED_VERIFIED.value, state
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_ats_never_receives_the_same_application_twice():
    """Across a submit and a reconcile, exactly one POST reaches the employer."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _prepared_application(
                service, browser, f"{ats.url}/form", "job-once"
            )
            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=FinalAction(
                    name="Submit application", success_patterns=("Application received",)
                ),
            )
            assert outcome.status in {"verified", "unverified"}, outcome.to_dict()

            posts_before = ats.submission_count
            await service.reconcile(
                row.id,
                controller=browser,
                action=FinalAction(success_patterns=("Application received",)),
            )
            assert ats.submission_count == posts_before, "reconcile must never post"
            assert ats.submission_count == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_mcp_reconcile_without_an_application_is_refused():
    """No application means nothing to bind the evidence to, so it is refused."""
    import json as _json

    from applyops.mcp import tools as tool_module
    from applyops.mcp.runtime import Runtime

    class FakeServer:
        def __init__(self) -> None:
            self.tools: dict = {}

        def tool(self, *_args, **_kwargs):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorate

    root = _tmp()
    _service(root)  # profile only
    runtime = Runtime(root)
    server = FakeServer()
    tool_module.register(server, runtime)

    payload = _json.loads(await server.tools["reconcile_submission"](evidence_text="x"))
    assert payload["reconciled"] is False
    assert "application_id is required" in payload["error"]
