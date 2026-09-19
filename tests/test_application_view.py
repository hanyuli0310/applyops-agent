from __future__ import annotations

from applyops.ledger import ApplicationRow
from applyops.presentation import application_view


def _row(state: str, company: str = "Small Co") -> ApplicationRow:
    return ApplicationRow(
        id="app-1",
        job_key="job-1",
        job_url="https://example.test/job-1",
        route="demo",
        platform="DemoATS",
        state=state,
        title="Backend Engineer",
        company=company,
    )


def test_waiting_for_input_view_names_reason_and_actions():
    view = application_view(
        _row("waiting_for_input"),
        missing=["Notice period"],
    )

    assert view["display_state"] == "待你补充"
    assert view["reason_code"] == "missing_answer"
    assert "Notice period" in view["reason_text"]
    assert view["available_actions"] == ["answer", "skip"]


def test_review_waiting_view_is_data_for_a_thin_console():
    view = application_view(
        _row("waiting_for_approval", company="Google"),
        company_policy="review",
    )

    assert view["reason_code"] == "review_required"
    assert view["available_actions"] == ["approve", "skip", "allow_company"]


def test_unverified_view_never_offers_submit_or_retry():
    view = application_view(_row("submitted_unverified"))

    assert view["reason_code"] == "unverified_result"
    assert view["available_actions"] == ["reconcile"]
