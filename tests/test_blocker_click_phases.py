"""Blocker: an exception after the click could still be reported as "not sent".

Repro, as reported: `_click_final` did everything -- resolve the control, click,
wait, adopt a new tab -- inside one `try`, and then decided whether the click had
happened by **string-matching the exception message** for "Timeout" or
"navigat". Anything else raised after `element.click()` returned was reported as

    clicked=False, sent=False  ->  FAILED

which is the one state a retry may start from. A page that closed, a tab that
could not be adopted, a browser that went away: all of them could invite a second
submission of an application the employer may already have.

The fix is control flow, not message inspection: explicit phases, and once the
click has been attempted nothing after it may be reported as "not sent".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import ClassVar

import pytest

from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState
from applyops.submission import FinalAction, _click_final

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}
ACTION = FinalAction(name="Submit application", success_patterns=("Application received",))


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-click-phases-"))


def _service(root: Path) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service = ApplicationService(root, memory=memory)
    service.answers.set_answer("Notice period", "Two weeks")
    return service


async def _ready_to_submit(service: ApplicationService, controller, ats, job_id: str):
    row = service.enqueue(
        job_url=f"{ats.url}/form", job_id=job_id, route="demo", platform="DemoATS"
    )
    outcome = await prepare_application(service, controller, row.id)
    assert outcome.ready, outcome.to_dict()
    grant = service.authorizer.approve_request(outcome.request_id, source="cli_human")
    assert grant is not None
    return service.get(row.id), grant


class _StubElement:
    def __init__(self, *, raises: Exception | None = None):
        self.raises = raises

    async def click(self, **_kwargs):
        if self.raises is not None:
            raise self.raises


class _StubController:
    """The smallest object `_click_final` needs, with one phase broken on demand."""

    def __init__(self, *, element=None, adopt_raises: Exception | None = None):
        self._element = element
        self._adopt_raises = adopt_raises
        self.clicked = False

    class _Page:
        url = "https://example.test/form"

    page = _Page()

    class _Context:
        pages: ClassVar[list] = []

    context = _Context()

    async def _adopt_new_tabs(self, _before):
        if self._adopt_raises is not None:
            raise self._adopt_raises


# ── the three phases, asserted directly ─────────────────────────────


@pytest.mark.asyncio
async def test_phase_one_failure_means_no_click_happened(monkeypatch):
    """The control could not be resolved: provably nothing was sent."""
    from applyops import locator as locator_module

    async def no_button(_page, _name):
        return None

    monkeypatch.setattr(locator_module, "find_button", no_button)

    result = await _click_final(_StubController(), ACTION)
    assert result["clicked"] is False
    assert result["phase"] == "pre_click"
    assert "button" in result["error"]


@pytest.mark.asyncio
async def test_a_click_that_raises_is_still_an_attempted_click(monkeypatch):
    """`element.click()` raising does not prove the click did not land."""
    from applyops import locator as locator_module

    async def element(_page, _name):
        return _StubElement(raises=RuntimeError("Target closed"))

    monkeypatch.setattr(locator_module, "find_button", element)

    result = await _click_final(_StubController(), ACTION)
    assert result["clicked"] is True, result
    assert result["phase"] == "click_attempted"
    assert result["settled"] is False


@pytest.mark.asyncio
async def test_post_click_failure_is_unknown_not_failed(monkeypatch):
    """Tab adoption blowing up after the click must not read as "not sent"."""
    from applyops import locator as locator_module

    async def element(_page, _name):
        return _StubElement()

    monkeypatch.setattr(locator_module, "find_button", element)

    controller = _StubController(adopt_raises=RuntimeError("no such window"))
    result = await _click_final(controller, ACTION)
    assert result["clicked"] is True, result
    assert result["settled"] is False
    assert result["phase"] == "post_click"


# ── what the service does with each phase ───────────────────────────


@pytest.mark.asyncio
async def test_a_pre_click_failure_lands_failed_and_unsent(monkeypatch):
    import applyops.submission as submission_module

    async def never_clicked(_controller, _action):
        return {"phase": "pre_click", "clicked": False, "settled": False, "error": "no button"}

    monkeypatch.setattr(submission_module, "_click_final", never_clicked)

    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _ready_to_submit(service, browser, ats, "job-pre")
            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=ACTION,
            )
            assert outcome.status == "failed", outcome.to_dict()
            assert outcome.evidence["sent"] is False
            assert service.get(row.id).state == ApplicationState.FAILED.value
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_page_that_dies_after_the_click_lands_unverified(monkeypatch):
    """The reported case: the click happened, then the page/tab handling failed."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _ready_to_submit(service, browser, ats, "job-dies")

            async def exploding_adopt(_before):
                raise RuntimeError("Target page, context or browser has been closed")

            monkeypatch.setattr(browser, "_adopt_new_tabs", exploding_adopt)

            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=ACTION,
            )

            # Unknown, not failed: the request left the machine.
            assert outcome.status == "unverified", outcome.to_dict()
            assert outcome.evidence["sent"] is True
            assert outcome.evidence.get("reconciliation_required") is True
            assert service.get(row.id).state == ApplicationState.SUBMITTED_UNVERIFIED.value
            assert ats.submission_count == 1

            # And it may not be submitted again: the state machine refuses.
            from applyops.state_machine import InvalidTransition
            from applyops.submission import SubmissionRefused

            with pytest.raises((SubmissionRefused, InvalidTransition)):
                await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=ACTION,
                )
            assert ats.submission_count == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_verified_submission_still_works():
    """The happy path must not regress: click, settle, evidence, verified."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row, grant = await _ready_to_submit(service, browser, ats, "job-happy")
            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=ACTION,
            )
            assert outcome.status == "verified", outcome.to_dict()
            assert outcome.evidence["sent"] is True
            assert service.get(row.id).state == ApplicationState.SUBMITTED_VERIFIED.value
            assert ats.submission_count == 1
            assert ats.last_submission["fields"]["name"] == "Jane Doe"
        finally:
            await browser.close()
