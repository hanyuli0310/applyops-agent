from __future__ import annotations

from pathlib import Path

import pytest

from applyops.company_policy import CompanyPolicy, CompanyPolicyStore
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.evidence import detect_final_action
from applyops.ledger import ApplicationRow
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.resume import resolve_resume
from applyops.runner import AutoPolicy, PolicyStore, QueueRunner
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState
from applyops.submission import SubmissionRefused


def _service(root: Path, *, answer_notice: bool = True) -> ApplicationService:
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
    if answer_notice:
        service.answers.set_answer("Notice period", "Two weeks")
    return service


def _runner(root: Path, service: ApplicationService) -> QueueRunner:
    CompanyPolicyStore(root).set(CompanyPolicy())
    PolicyStore(root).set(
        AutoPolicy(
            enabled=True,
            max_applications=10,
            allowed_platforms=("DemoATS",),
            expires_at_epoch=9e9,
        )
    )
    return QueueRunner(
        service,
        policy_store=PolicyStore(root),
        company_policy_store=CompanyPolicyStore(root),
    )


def _enqueue(service: ApplicationService, ats: DemoATS, key: str, company: str) -> ApplicationRow:
    return service.enqueue(
        job_url=f"{ats.url}/form?job={key}",
        job_id=key,
        route="demo",
        platform="DemoATS",
        title="Backend Engineer",
        company=company,
    )


@pytest.mark.asyncio
async def test普通公司自动提交(tmp_path: Path):
    service = _service(tmp_path)
    runner = _runner(tmp_path, service)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "auto-1", "Small Co")
            report = await runner.run_pass(browser, budget=1)
            assert [item["application_id"] for item in report.submitted] == [row.id]
            assert service.get(row.id).state == ApplicationState.SUBMITTED_VERIFIED.value
            assert ats.submission_count == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_review_company_prepares_but_waits_for_human(tmp_path: Path):
    service = _service(tmp_path)
    runner = _runner(tmp_path, service)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "review-1", "Google LLC")
            report = await runner.run_pass(browser, budget=1)
            assert report.submitted == []
            assert report.prepared[0]["company_policy"] == "review"
            assert service.get(row.id).state == ApplicationState.WAITING_FOR_APPROVAL.value
            assert len(service.authorizer.pending_requests()) == 1
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_approved_review_company_submits_on_next_pass(tmp_path: Path):
    service = _service(tmp_path)
    runner = _runner(tmp_path, service)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "review-approve", "Google")
            first = await runner.run_pass(browser, budget=1)
            grant = service.authorizer.approve_request(
                first.prepared[0]["request_id"], source="cli_human"
            )
            assert grant is not None
            second = await runner.run_pass(browser, budget=1)
            assert [item["application_id"] for item in second.submitted] == [row.id]
            assert service.get(row.id).state == ApplicationState.SUBMITTED_VERIFIED.value
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_removing_review_entry_allows_next_application_to_auto_submit(tmp_path: Path):
    service = _service(tmp_path)
    policy_store = CompanyPolicyStore(tmp_path)
    policy_store.set(CompanyPolicy(review_companies=["Google"]))
    runner = _runner(tmp_path, service)
    policy_store.set(CompanyPolicy(review_companies=["Google"]))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            first = _enqueue(service, ats, "review-remove", "Google")
            first_report = await runner.run_pass(browser, budget=1)
            assert first_report.submitted == []
            policy_store.set(CompanyPolicy(review_companies=[]))
            second = _enqueue(service, ats, "review-removed-auto", "Google")
            second_report = await runner.run_pass(browser, budget=1)
            assert [item["application_id"] for item in second_report.submitted] == [second.id]
            assert service.get(first.id).state == ApplicationState.WAITING_FOR_APPROVAL.value
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_aws_alias_uses_amazon_review_policy(tmp_path: Path):
    service = _service(tmp_path)
    policy_store = CompanyPolicyStore(tmp_path)
    policy_store.set(CompanyPolicy(review_companies=["Amazon"]))
    runner = _runner(tmp_path, service)
    policy_store.set(CompanyPolicy(review_companies=["Amazon"]))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "aws-review", "AWS")
            report = await runner.run_pass(browser, budget=1)
            assert report.submitted == []
            assert report.prepared[0]["company_policy"] == "review"
            assert service.get(row.id).state == ApplicationState.WAITING_FOR_APPROVAL.value
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_never_company_is_skipped_without_prepare(tmp_path: Path):
    service = _service(tmp_path)
    policy_store = CompanyPolicyStore(tmp_path)
    policy_store.set(CompanyPolicy(never_companies=["Never Co"]))
    runner = _runner(tmp_path, service)
    policy_store.set(CompanyPolicy(never_companies=["Never Co"]))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "never-1", "Never Co")
            report = await runner.run_pass(browser, budget=1)
            assert report.submitted == []
            assert report.skipped[0]["application_id"] == row.id
            assert service.get(row.id).state == ApplicationState.SKIPPED.value
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_never_company_cannot_be_manually_prepared(tmp_path: Path):
    service = _service(tmp_path)
    CompanyPolicyStore(tmp_path).set(CompanyPolicy(never_companies=["Never Co"]))

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "never-manual", "Never Co")
            outcome = await prepare_application(service, browser, row.id)
            assert outcome.state == ApplicationState.SKIPPED.value
            assert service.get(row.id).state == ApplicationState.SKIPPED.value
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_never_policy_blocks_an_existing_approval_before_submit(tmp_path: Path):
    service = _service(tmp_path)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "never-existing", "Never Co")
            prepared = await prepare_application(service, browser, row.id)
            assert prepared.ready
            grant = service.authorizer.approve_request(prepared.request_id, source="cli_human")
            assert grant is not None
            CompanyPolicyStore(tmp_path).set(CompanyPolicy(never_companies=["Never Co"]))
            action, detail = await detect_final_action(browser)
            assert action is not None, detail
            with pytest.raises(SubmissionRefused, match="never list"):
                await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(service.memory.profile.value("resume_path")),
                    action=action,
                )
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_unknown_question_never_gets_guessed(tmp_path: Path):
    service = _service(tmp_path, answer_notice=False)
    runner = _runner(tmp_path, service)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            row = _enqueue(service, ats, "unknown-1", "Small Co")
            report = await runner.run_pass(browser, budget=1)
            assert report.submitted == []
            assert report.parked[0]["application_id"] == row.id
            assert service.get(row.id).state == ApplicationState.WAITING_FOR_INPUT.value
            assert ats.submission_count == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_three_auto_companies_are_independent(tmp_path: Path):
    service = _service(tmp_path)
    runner = _runner(tmp_path, service)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=tmp_path / "chrome")
        await browser.launch()
        try:
            rows = [_enqueue(service, ats, f"auto-{i}", f"Small Co {i}") for i in range(3)]
            report = await runner.run_pass(browser, budget=3)
            assert {item["application_id"] for item in report.submitted} == {r.id for r in rows}
            assert ats.submission_count == 3
            assert all(service.get(row.id).state == ApplicationState.SUBMITTED_VERIFIED.value for row in rows)
        finally:
            await browser.close()
