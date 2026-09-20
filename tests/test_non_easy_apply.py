"""Blocker: a non-Easy-Apply posting is treated as an Easy-Apply one.

Observed live on a real LinkedIn posting (`Senior Python Engineer`, MongoDB): the
route is decided from the *discovery URL*, so a linkedin.com posting is filed as
`easy_apply` and counts as drivable -- while its Apply control is

    <a href="https://www.linkedin.com/safety/go/?url=<encoded Greenhouse url>">Apply</a>

i.e. the application does not happen here at all. Three consequences, all of
which these tests pin down:

1. the queue believes it can drive a posting it cannot (P1);
2. `prepare` parks it under "no file input found on this form", which names
   neither the cause nor the place the application actually lives (P2);
3. a grant minted for such a page -- an empty snapshot -- is not stopped by the
   snapshot digest, because an empty form has an empty digest that matches
   itself (P3/P4). Nothing may be clicked from a page that has no form.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applyops.answers import AnswerStore
from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState
from applyops.submission import FinalAction, SubmissionRefused

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-offsite-"))


def _service(root: Path) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service = ApplicationService(root, memory=memory, answers=AnswerStore(root))
    service.answers.set_answer("Notice period", "Two weeks")
    return service


@pytest.mark.asyncio
async def test_an_offsite_posting_is_parked_as_external_not_as_missing_input():
    """The reported shape: nothing to fill, and the form is somewhere else."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/offsite",
                job_id="offsite-1",
                route="easy_apply",  # what the discovery URL produces
                platform="LinkedIn",
                title="Backend Engineer",
                company="Acme",
            )
            assert row.route == "easy_apply"

            outcome = await prepare_application(service, browser, row.id)

            assert outcome.state == ApplicationState.WAITING_FOR_INPUT.value, outcome.to_dict()
            # The reason has to name the real problem: the application lives on
            # another site, and greenhouse.io is where.
            assert "greenhouse.io" in (outcome.detail + " ".join(outcome.missing)).lower(), (
                outcome.to_dict()
            )
            assert "no file input found" not in " ".join(outcome.missing), outcome.missing

            # And the queue stops treating it as drivable.
            assert service.get(row.id).route == "external"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_page_with_no_form_at_all_says_so():
    """No form and no off-site signal must still name the real problem."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/",
                job_id="noform-1",
                route="easy_apply",
                platform="LinkedIn",
                title="Backend Engineer",
                company="Acme",
            )
            outcome = await prepare_application(service, browser, row.id)
            assert outcome.state == ApplicationState.WAITING_FOR_INPUT.value
            assert "no application form" in outcome.detail.lower(), outcome.to_dict()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_nothing_is_clicked_from_a_page_without_a_form():
    """An empty snapshot must not be submittable.

    This is the hazard behind the AUTO company policy: it issues a grant for
    whatever `prepare` filed, and `prepare` used to file requests for pages with
    no form at all -- a request whose digest is the digest of nothing.
    """
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/",
                job_id="noform-2",
                route="demo",
                platform="DemoATS",
                title="Backend Engineer",
                company="Acme",
            )
            # Force the state a mis-filed request would leave behind: an approval
            # request minted from a page that has no form.
            await browser.goto(row.job_url, settle=0.4)
            service.ledger.transition(row.id, ApplicationState.PREPARING)
            service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)
            request = service.authorizer.create_request(
                job_key=row.job_key,
                job_url=row.job_url,
                route="demo",
                platform="DemoATS",
                fields=await browser.field_snapshot(),  # empty
                resume_filename="resume.pdf",
                resume_sha256=resolve_resume(
                    str(service.memory.profile.value("resume_path"))
                ).sha256,
                application_id=row.id,
                page_url=browser.page.url,
                requested_by="test",
            )
            grant = service.authorizer.approve_request(request.request_id, source="cli_human")
            assert grant is not None

            with pytest.raises(SubmissionRefused) as excinfo:
                await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=FinalAction(
                        name="Submit application", success_patterns=("Application received",)
                    ),
                )
            assert "no application form" in str(excinfo.value).lower(), str(excinfo.value)
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_easy_apply_path_still_works_end_to_end():
    """The fix must not touch the route that does work."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/form",
                job_id="normal-1",
                route="demo",
                platform="DemoATS",
            )
            prepared = await prepare_application(service, browser, row.id)
            assert prepared.ready, prepared.to_dict()
            assert service.get(row.id).route == "demo"

            grant = service.authorizer.approve_request(prepared.request_id, source="cli_human")
            outcome = await service.submit(
                row.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=FinalAction(
                    name="Submit application", success_patterns=("Application received",)
                ),
            )
            assert outcome.status == "verified", outcome.to_dict()
            assert ats.submission_count == 1
        finally:
            await browser.close()
