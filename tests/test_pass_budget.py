"""Per-pass budget: how many to send *this run*, chosen each time.

Before this, a pass took its budget from the stored auto-policy
(`policy.max_applications`), which is set once and then silently reused. A number
a person chose last week then governs tonight's run, and "run a pass" with no
policy at all quietly reverts to zero rather than asking.

The rule under test: **every pass states how many it may send.** No carry-over,
no default, and the outer policy bound still applies.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from applyops.api.app import create_app
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.runner import AutoPolicy, PassBudgetRequired, QueueRunner
from applyops.service import ApplicationService

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-budget-"))


def _service(root: Path) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service = ApplicationService(root, memory=memory)
    service.answers.set_answer("Notice period", "Two weeks")
    return service


def _policy(*, max_applications: int = 5) -> AutoPolicy:
    return AutoPolicy(
        enabled=True,
        max_applications=max_applications,
        allowed_platforms=("DemoATS",),
        expires_at_epoch=9e9,
        updated_by="test",
    )


async def _approved(service: ApplicationService, controller, ats, job_id: str):
    row = service.enqueue(
        job_url=f"{ats.url}/form", job_id=job_id, route="demo", platform="DemoATS"
    )
    outcome = await prepare_application(service, controller, row.id)
    assert outcome.ready, outcome.to_dict()
    service.authorizer.approve_request(outcome.request_id, source="cli_human")
    return service.get(row.id)


# ── the runner refuses to guess ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_pass_without_a_budget_is_refused():
    """No number, no run -- and the refusal is a refusal, not a zero."""
    root = _tmp()
    service = _service(root)
    runner = QueueRunner(service, memory=service.memory, answers=service.answers)
    runner.policy_store.set(_policy())

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await _approved(service, browser, ats, "job-no-budget")
            with pytest.raises(PassBudgetRequired):
                await runner.run_pass(browser)
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_budget_of_zero_or_less_is_refused():
    root = _tmp()
    service = _service(root)
    runner = QueueRunner(service, memory=service.memory, answers=service.answers)
    runner.policy_store.set(_policy())

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await _approved(service, browser, ats, "job-zero")
            for bad in (0, -1):
                with pytest.raises(PassBudgetRequired):
                    await runner.run_pass(browser, budget=bad)
            assert ats.submission_count == 0
        finally:
            await browser.close()


# ── the budget bounds this pass, and only this pass ─────────────────


@pytest.mark.asyncio
async def test_the_budget_bounds_one_pass_and_does_not_carry_over():
    root = _tmp()
    service = _service(root)
    runner = QueueRunner(service, memory=service.memory, answers=service.answers)
    runner.policy_store.set(_policy())

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await _approved(service, browser, ats, "job-a")
            await _approved(service, browser, ats, "job-b")

            report = await runner.run_pass(browser, budget=1)
            assert len(report.submitted) == 1, report.to_dict()
            assert report.budget == 1
            assert report.budget_remaining == 0
            assert "budget" in report.stopped_reason, report.stopped_reason
            assert ats.submission_count == 1

            # The next pass has to say how many again.
            with pytest.raises(PassBudgetRequired):
                await runner.run_pass(browser)

            again = await runner.run_pass(browser, budget=1)
            assert len(again.submitted) == 1, again.to_dict()
            assert ats.submission_count == 2
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_pass_budget_cannot_exceed_the_policy():
    root = _tmp()
    service = _service(root)
    runner = QueueRunner(service, memory=service.memory, answers=service.answers)
    runner.policy_store.set(_policy(max_applications=2))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            await _approved(service, browser, ats, "job-over")
            with pytest.raises(PassBudgetRequired) as excinfo:
                await runner.run_pass(browser, budget=5)
            assert "policy" in str(excinfo.value), excinfo.value
            assert ats.submission_count == 0
        finally:
            await browser.close()


# ── through the console ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_console_asks_for_the_number_every_time():
    """The API refuses a pass with no number, and reports what was spent."""
    root = _tmp()
    app = create_app(root, frontend_dist=None, headless=True)
    state = app.state.applyops
    memory = state.memory
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    state.service.answers.set_answer("Notice period", "Two weeks")

    with DemoATS() as ats:
        state.demo_ats = ats
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers.update({"X-ApplyOps-Token": state.session_token})

            client.post(
                "/api/runner/policy",
                json={
                    "enabled": True,
                    "max_applications": 5,
                    "allowed_platforms": ["DemoATS"],
                    "ttl_minutes": 60,
                },
            )

            # No number: refused, and nothing is sent.
            missing = client.post("/api/runner/pass", json={})
            assert missing.status_code == 422, missing.text

            zero = client.post("/api/runner/pass", json={"budget": 0})
            assert zero.status_code == 422, zero.text

            # With a number, the pass runs and reports it.
            run = client.post("/api/runner/pass", json={"budget": 2})
            assert run.status_code == 200, run.text
            body = run.json()
            assert body["budget"] == 2
            assert "budget_remaining" in body
            assert ats.submission_count <= 2

            # And the next one has to say it again.
            again = client.post("/api/runner/pass", json={})
            assert again.status_code == 422, again.text

        await state.close_browser()
