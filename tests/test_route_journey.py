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
