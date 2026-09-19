"""M4 -- Supervised Automation.

The safety properties under test:

- **Answer scopes are real boundaries.** A company answer never answers another
  company's form; withdrawal changes the revision, which voids grants.
- **The runner is supervised by default.** With the policy off, a pass prepares
  and files requests but submits nothing. Even with the policy on, it can only
  spend grants a human already minted, within the budget, on the allowed
  platforms.
- **Missing information parks an application** in `waiting_for_input` with the
  reason recorded -- it is never guessed past.
- **Pause and stop are honoured between applications.**
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from applyops.answers import AnswerScope, AnswerStore
from applyops.authorization import SubmissionAuthorizer
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.ledger import ApplicationRow
from applyops.runner import AutoPolicy, PassBudgetRequired, PolicyStore, QueueRunner
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-m4-"))


def _service(data_dir: Path | None = None) -> ApplicationService:
    from applyops.memory import MemoryStore

    root = data_dir or _tmp()
    return ApplicationService(root, memory=MemoryStore(root / "memory.json"))


# ── A. answer scopes ─────────────────────────────────────────────────


def test_scope_precedence_is_most_specific_first():
    root = _tmp()
    store = AnswerStore(root)
    store.set_answer("Are you authorised to work in the United States?", "Yes", scope=AnswerScope.GLOBAL)
    store.set_answer(
        "Are you authorised to work in the United States?",
        "No -- requires sponsorship",
        scope=AnswerScope.COMPANY,
        company="Overseas Corp",
    )

    assert store.resolve("Are you authorised to work in the United States?").answer == "Yes"
    overseas = store.resolve(
        "Are you authorised to work in the United States?", company="Overseas Corp"
    )
    assert overseas.answer == "No -- requires sponsorship"
    # A different company must NOT see the first company's answer.
    other = store.resolve(
        "Are you authorised to work in the United States?", company="Other Corp"
    )
    assert other.answer == "Yes"


def test_application_scope_beats_everything_and_never_leaks():
    root = _tmp()
    store = AnswerStore(root)
    store.set_answer("Why this role?", "Default pitch", scope=AnswerScope.GLOBAL)
    store.set_answer(
        "Why this role?", "Tailored for Acme", scope=AnswerScope.COMPANY, company="Acme"
    )
    store.set_answer(
        "Why this role?",
        "Written for requisition 42",
        scope=AnswerScope.APPLICATION,
        application_id="app-42",
    )

    assert store.resolve("Why this role?", application_id="app-42").answer == (
        "Written for requisition 42"
    )
    assert store.resolve("Why this role?", company="Acme").answer == "Tailored for Acme"
    assert store.resolve("Why this role?", application_id="app-43").answer == "Default pitch"


def test_similar_questions_are_different_entries():
    """近似文本 ≠ 相同语义。The plan names this explicitly."""
    root = _tmp()
    store = AnswerStore(root)
    store.set_answer("Do you now require sponsorship?", "No", scope=AnswerScope.GLOBAL)

    found = store.resolve("Will you in the future require sponsorship?")
    assert found is None, "a differently-worded question must not inherit an answer"


def test_scoped_answers_require_their_context():
    root = _tmp()
    store = AnswerStore(root)
    with pytest.raises(ValueError):
        store.set_answer("Q?", "A", scope=AnswerScope.COMPANY)
    with pytest.raises(ValueError):
        store.set_answer("Q?", "A", scope=AnswerScope.APPLICATION)
    with pytest.raises(ValueError):
        store.set_answer("Q?", "   ", scope=AnswerScope.GLOBAL)


def test_withdrawal_bumps_the_revision_and_stops_the_answer():
    root = _tmp()
    store = AnswerStore(root)
    entry = store.set_answer("Salary expectation?", "180k", scope=AnswerScope.GLOBAL)
    before = store.revision

    assert store.withdraw(entry.id) is True
    assert store.revision != before
    assert store.resolve("Salary expectation?") is None
    assert store.withdraw(entry.id) is False  # already withdrawn


def test_withdrawing_an_answer_voids_a_live_grant():
    """The whole point of answers_revision in the grant digest."""
    root = _tmp()
    store = AnswerStore(root)
    authorizer = SubmissionAuthorizer(root)
    entry = store.set_answer("Sponsorship?", "No", scope=AnswerScope.GLOBAL)

    request = authorizer.create_request(
        job_key="job-1",
        job_url="https://example.test/1",
        route="demo",
        platform="DemoATS",
        fields={"Sponsorship?": "No"},
        answers_revision=store.revision,
        requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")

    # The user withdraws the answer before submission.
    store.withdraw(entry.id)

    verdict = authorizer.verify(
        grant.grant_id,
        job_key="job-1",
        fields={"Sponsorship?": "No"},
        resume_sha256="",
        answers_revision=store.revision,  # changed
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False
    assert "no longer matches" in verdict.reason


# ── B. policy ────────────────────────────────────────────────────────


def test_policy_defaults_to_disabled_and_stays_bounded():
    root = _tmp()
    store = PolicyStore(root)
    policy = store.get()
    assert policy.usable is False  # disabled by default

    enabled = AutoPolicy(
        enabled=True, max_applications=2, allowed_platforms=("DemoATS",),
        expires_at_epoch=__import__("time").time() + 3600,
    )
    store.set(enabled)
    assert store.get().usable is True

    expired = AutoPolicy(
        enabled=True, max_applications=2, expires_at_epoch=1.0,
    )
    assert expired.usable is False


# ── C. the runner ────────────────────────────────────────────────────


def _service_with_resume(root: Path, *, with_resume: bool = True) -> ApplicationService:
    """A service whose profile is complete enough to fill the demo form.

    The runner fills before it asks for approval, so a profile with only a
    resume path now parks every application -- which is the correct behaviour
    and would make these tests assert the wrong thing.
    """
    service = _service(root)
    if with_resume:
        resume = write_sample_resume(root / "resume.pdf")
        service.memory.update_profile(
            {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "phone": "+1 555 010 4477",
                "years_experience": "4",
                "requires_sponsorship": "no",
                "resume_path": str(resume),
            }
        )
        # The one thing the profile does not know, answered once.
        service.answers.set_answer("Notice period", "Two weeks")
    return service


def _enqueue_demo(service: ApplicationService, demo_url: str, key: str) -> ApplicationRow:
    return service.enqueue(
        job_url=f"{demo_url}/form",
        job_id=key,
        route="demo",
        platform="DemoATS",
        title="Backend Engineer (demo)",
        company="ApplyOps Demo Co",
    )


@pytest.mark.asyncio
async def test_disabled_policy_prepares_but_never_submits():
    """Supervised by default: a pass files requests; the human does the rest."""
    root = _tmp()
    service = _service_with_resume(root)
    runner = QueueRunner(service, policy_store=PolicyStore(root))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            _enqueue_demo(service, ats.url, "job-1")
            report = await runner.run_pass(controller, budget=5)

            assert report.submitted == []  # nothing was sent, policy or no policy
            assert len(report.prepared) == 1
            assert report.prepared[0]["request_id"]

            # The application waits for a human -- exactly as designed.
            row = service.get(report.prepared[0]["application_id"])
            assert row.state == ApplicationState.WAITING_FOR_APPROVAL.value

            # And a pass that finds nothing new to prepare does nothing rash.
            second = await runner.run_pass(controller, budget=5)
            assert second.prepared == []
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_missing_resume_parks_the_application_with_the_reason():
    root = _tmp()
    service = _service(root)  # no resume configured
    runner = QueueRunner(service, policy_store=PolicyStore(root))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            _enqueue_demo(service, ats.url, "job-no-resume")
            report = await runner.run_pass(controller, budget=5)

            assert report.submitted == []
            assert len(report.parked) == 1
            assert "resume" in report.parked[0]["reason"]
            row = service.get(report.parked[0]["application_id"])
            assert row.state == ApplicationState.WAITING_FOR_INPUT.value
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_policy_bounds_how_many_preapproved_submissions_a_pass_spends():
    """Two pre-approved jobs: the pass asks for two, the policy allows one.

    The count is chosen for this pass (it always is, now) and the stored policy
    still clamps it -- asking for more than the policy is refused outright.
    """
    root = _tmp()
    service = _service_with_resume(root)
    runner = QueueRunner(service, policy_store=PolicyStore(root))
    policy = AutoPolicy(
        enabled=True,
        max_applications=1,
        allowed_platforms=("DemoATS",),
        expires_at_epoch=__import__("time").time() + 3600,
        updated_by="test",
    )
    runner.policy_store.set(policy)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            rows = [
                _enqueue_demo(service, ats.url, f"job-{i}") for i in range(2)
            ]

            # First pass: prepares both, submits nothing (no grants yet).
            first = await runner.run_pass(controller, budget=1)
            assert len(first.prepared) == 2 and first.submitted == []

            # The human approves BOTH (a batch review), minting two grants.
            for prepared in first.prepared:
                grant = service.authorizer.approve_request(
                    prepared["request_id"], source="cli_human"
                )
                assert grant is not None

            # Asking for more than the policy allows is refused, not clamped
            # silently: the operator should hear "no", not get 1 of the 2.
            with pytest.raises(PassBudgetRequired):
                await runner.run_pass(controller, budget=2)

            # Second pass: one -- exactly one submission.
            second = await runner.run_pass(controller, budget=1)
            assert len(second.submitted) == 1, second.to_dict()
            assert second.submitted[0]["status"] == "verified"
            assert "budget spent" in second.stopped_reason

            # Third pass: the budget is per policy lifetime; it was spent, and
            # the remaining application is still waiting for its human.
            row = service.get(rows[1].id)
            assert row.state in {
                ApplicationState.WAITING_FOR_APPROVAL.value,
                ApplicationState.SUBMITTED_VERIFIED.value,
            }
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_policy_platform_allowlist_refuses_outside_platforms():
    root = _tmp()
    service = _service_with_resume(root)
    runner = QueueRunner(service, policy_store=PolicyStore(root))
    runner.policy_store.set(
        AutoPolicy(
            enabled=True,
            max_applications=5,
            allowed_platforms=("LinkedIn",),  # demo ATS is NOT allowed
            expires_at_epoch=__import__("time").time() + 3600,
        )
    )

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            _enqueue_demo(service, ats.url, "job-off-platform")
            report = await runner.run_pass(controller, budget=5)
            assert report.submitted == []
            assert report.prepared == []  # not even prepared: outside the policy
            assert report.refused and "outside the policy" in report.refused[0]["reason"]
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_stop_and_pause_are_honoured_between_applications():
    root = _tmp()
    service = _service_with_resume(root)
    runner = QueueRunner(service, policy_store=PolicyStore(root))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            for i in range(3):
                _enqueue_demo(service, ats.url, f"job-{i}")

            runner.pause()
            paused_report = await runner.run_pass(controller, budget=5)
            assert paused_report.prepared == []
            assert "paused" in paused_report.stopped_reason

            runner.resume()
            runner.stop()
            stopped_report = await runner.run_pass(controller, budget=5)
            assert stopped_report.prepared == []
            assert "stopped" in stopped_report.stopped_reason
        finally:
            await controller.close()


@pytest.mark.asyncio
async def test_reconciliation_pass_never_submits():
    """An unverified result is investigated; nothing is ever re-sent."""
    root = _tmp()
    service = _service_with_resume(root)
    runner = QueueRunner(service, policy_store=PolicyStore(root))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        controller = BrowserController(headless=True, user_data_dir=root / "chrome")
        await controller.launch()
        try:
            row = _enqueue_demo(service, ats.url, "job-unknown")
            service.ledger.transition(row.id, ApplicationState.PREPARING)
            service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)
            service.ledger.transition(row.id, ApplicationState.SUBMITTING)
            service.ledger.transition(row.id, ApplicationState.SUBMITTED_UNVERIFIED)

            report = await runner.run_pass(controller, budget=5)
            assert len(report.reconciled) == 1
            assert report.submitted == []
            assert (
                service.get(row.id).state
                == ApplicationState.SUBMITTED_UNVERIFIED.value
            )  # page said nothing; the honest answer survives a pass
        finally:
            await controller.close()
