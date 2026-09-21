"""Walking a two-hop application, and remembering how it was done.

The ask: walk it once, remember every step, and automate it next time. The
project already has the place to remember it in -- `RouteKnowledge` with
`RouteStep`, `entry_signature` and the `runs`/`successes`/`blocked_at` counters --
but nothing ever wrote a *successful* journey: the only writer records where an
attempt died, and the model's own docstring says it is "not to replay a
recording". So the first half is recording.

The fixture is a posting whose Apply control leaves for another host (the demo
ATS reached through `localhost` instead of `127.0.0.1`, which is a real host
change and still entirely local).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applyops.answers import AnswerStore
from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.filling import fill_application_form, resume_for_fill
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
    "location": "Seattle, WA",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-journey-"))


def _service(root: Path) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service = ApplicationService(root, memory=memory, answers=AnswerStore(root))
    service.answers.set_answer("Notice period", "Two weeks")
    return service


@pytest.mark.asyncio
async def test_the_default_is_still_to_stop_at_the_off_site_control():
    """No opt-in: nothing is clicked, and nothing is recorded as a journey."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/offsite?landing=1",
                job_id="offsite-default",
                route="easy_apply",
                platform="LinkedIn",
            )
            outcome = await prepare_application(service, browser, row.id)
            assert outcome.state == ApplicationState.WAITING_FOR_INPUT.value
            assert outcome.journey_key == "", outcome.to_dict()
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_walking_the_hop_records_every_step_of_the_journey():
    """Walk it once: the posting, the Apply click, the landing, the fill."""
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/offsite?landing=1",
                job_id="offsite-journey",
                route="easy_apply",
                platform="LinkedIn",
            )
            outcome = await prepare_application(
                service, browser, row.id, allow_offsite_hop=True
            )

            # It walked through to the employer's form and prepared it there.
            assert outcome.state == ApplicationState.WAITING_FOR_APPROVAL.value, outcome.to_dict()
            assert outcome.journey_key, outcome.to_dict()

            # The approval is bound to the page the form is actually on -- not to
            # the posting we started from.
            request = service.authorizer.get_request(outcome.request_id)
            assert "localhost" in request.page_identity, request.page_identity

            # And every step is remembered, in order.
            platform, _, route = outcome.journey_key.partition("/")
            knowledge = service.memory.get_route(platform, route)
            kinds = [step.kind for step in knowledge.steps]
            assert kinds[0] == "open", kinds
            assert "click" in kinds, kinds
            assert "hop" in kinds, kinds
            assert "fill" in kinds, kinds
            assert "upload" in kinds, kinds
            assert knowledge.entry_signature, "the journey must be matchable next time"

            # The fill steps carry what was written and where from: that is what a
            # replay needs, and what a person needs to review it.
            fills = [step for step in knowledge.steps if step.kind == "fill"]
            assert any("Full name" in step.detail for step in fills), [
                step.detail for step in fills
            ]
            assert all(step.selector for step in fills), [s.selector for s in fills]

            # The hop step names where it went, because that is the part a
            # next-time caller has to decide about.
            hop = next(step for step in knowledge.steps if step.kind == "hop")
            assert "localhost" in hop.detail, hop.detail
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_an_easy_apply_link_to_another_page_is_followed():
    """The real shape of LinkedIn Easy Apply: the form is one click away.

    Verified live on a real posting: the Apply control is an `<a>` to
    `.../jobs/view/<id>/apply/` -- same host, no modal -- and the posting page
    has no form at all. Preparing without following it parks the application
    under "no file input found on this form", which is what happened.
    """
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/onsite",
                job_id="onsite-1",
                route="easy_apply",
                platform="LinkedIn",
            )
            outcome = await prepare_application(service, browser, row.id)

            assert outcome.state == ApplicationState.WAITING_FOR_APPROVAL.value, outcome.to_dict()
            assert outcome.route == "easy_apply", outcome.route

            # Filled on the apply page, and the approval is bound to it.
            request = service.authorizer.get_request(outcome.request_id)
            assert request.page_identity.endswith("/apply"), request.page_identity
            assert len(outcome.fill_report.get("filled", [])) >= 5, outcome.fill_report

            # And the walk is remembered, click included.
            platform, _, route = outcome.journey_key.partition("/")
            kinds = [s.kind for s in service.memory.get_route(platform, route).steps]
            assert "click" in kinds, kinds
            assert "fill" in kinds, kinds
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_split_first_and_last_name_are_answered_from_the_full_name():
    """A real Easy Apply modal asks for first and last name separately.

    Verified live: the modal pre-fills both from the member's LinkedIn profile,
    but the profile here stores one `name`, so the filler had no value to write
    *or to check the existing one against* and reported both as missing. The
    split is mechanical -- last token is the family name, the rest is the given
    name -- and it comes from the user's own stored name, not from a guess.
    """
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await browser.goto(f"{ats.url}/apply", settle=0.6)
            report = await fill_application_form(
                browser,
                memory=service.memory,
                answers=service.answers,
                resume=resume_for_fill(service.memory),
                application_id="split-name",
            )
            assert report.ready, report.to_dict()
            labels = {f.label: f for f in report.filled}
            assert "First name" in labels, list(labels)
            assert "Last name" in labels, list(labels)
            # The value already on the page matches, so it is verified, not typed
            # over blindly.
            assert labels["First name"].verification == "verified"
            assert labels["Last name"].verification == "verified"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_city_question_is_answered_with_the_postings_location():
    """The rule: a city question asks *which* location, so it gets the employer's.

    Asked for explicitly, and it matters because the two values are easy to
    confuse on a form. The source is reported as `job:location`, so the approval
    summary shows the value did not come from the applicant's own profile.
    """
    root = _tmp()
    service = _service(root)
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/apply",
                job_id="city-1",
                route="easy_apply",
                platform="LinkedIn",
                company="Ordinary Co",
                location="Austin, TX",
            )
            assert row.location == "Austin, TX"

            outcome = await prepare_application(service, browser, row.id)
            assert outcome.state == ApplicationState.WAITING_FOR_APPROVAL.value, outcome.to_dict()

            city = next(
                f for f in outcome.fill_report["filled"] if f["label"] == "Location (city)"
            )
            assert city["source"] == "job:location", city
            assert city["verification"] == "verified", city
            assert "Austin" in await browser.page.input_value("#city")
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_without_a_posting_location_the_profile_answers_the_city_question():
    """The fallback, so an existing queue keeps working."""
    root = _tmp()
    service = _service(root)
    service.memory.update_profile({"location": "Seattle, WA"})
    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            row = service.enqueue(
                job_url=f"{ats.url}/apply",
                job_id="city-2",
                route="easy_apply",
                platform="LinkedIn",
            )
            outcome = await prepare_application(service, browser, row.id)
            city = next(
                f for f in outcome.fill_report["filled"] if f["label"] == "Location (city)"
            )
            assert city["source"] == "profile:location", city
        finally:
            await browser.close()
