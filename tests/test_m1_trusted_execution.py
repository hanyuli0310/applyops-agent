"""M1 -- Trusted Execution.

Every test here guards something that used to be quietly false:

| what was claimed | what was true |
|---|---|
| a filled field that read back empty was "ok" | the field did not take the value |
| `click_target` could press Submit | the token was checked *after* the click |
| `acknowledged=True` counted as approval | a boolean the caller writes itself is not approval |
| an application with `status: applied` counted as a success | nobody confirmed the employer received it |

Browser tests run against the **local demo ATS only**. No real employer, no real
account, no real application: the whole point is proving the machine will not
send things it was not authorized to send, and practising that on a stranger's
job posting would be indefensible.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from applyops.action_policy import TargetFacts, classify, decide_click
from applyops.authorization import (
    SubmissionAuthorizer,
    SubmissionRequest,
    snapshot_digest,
)
from applyops.browser import BrowserController
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.resume import (
    ResumeNotConfigured,
    ResumeNotFound,
    ResumeUnreadable,
    ResumeUnsupported,
    resolve_resume,
)
from applyops.submission import (
    FinalAction,
    SubmissionRefused,
    execute_authorized_submission,
    reconcile_submission,
)
from applyops.verification import (
    ObservedFile,
    Verification,
    verify_boolean_state,
    verify_choice,
    verify_text_value,
    verify_upload,
)

# ── helpers ──────────────────────────────────────────────────────────


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-m1-"))


async def _browser(root: Path) -> BrowserController:
    controller = BrowserController(headless=True, user_data_dir=root / "chrome-profile")
    await controller.launch()
    return controller


async def _ref_for(browser: BrowserController, label: str) -> str:
    state = await browser.get_page_state()
    for field in state.form_fields:
        if (field.label or "").strip() == label:
            return field.ref
    raise AssertionError(f"no field labelled {label!r}: {[f.label for f in state.form_fields]}")


async def _ref_by_type(browser: BrowserController, kind: str) -> str:
    """A radio/checkbox ref, whatever the page decided to call it."""
    state = await browser.get_page_state()
    for field in state.form_fields:
        if (field.field_type or "").lower() == kind:
            return field.ref
    raise AssertionError(f"no {kind} control found: {[(f.label, f.field_type) for f in state.form_fields]}")


async def _new_browser_and_form(ats_url: str, scenario: str = "") -> BrowserController:
    root = _tmp()
    browser = await _browser(root)
    query = f"?scenario={scenario}" if scenario else ""
    await browser.goto(f"{ats_url}/form{query}", settle=0.4)
    return browser


# ── A. verification semantics ────────────────────────────────────────


def test_empty_readback_is_not_success():
    """The regression this milestone exists to close.

    Under the old rule `bool(readback)` short-circuited, so *not knowing* scored
    the same as agreement. Typing into a control that rejects the value (a number
    field receiving letters) left the field empty and was reported as filled.
    """
    outcome = verify_text_value("Jane Doe", "")
    assert outcome.verification is Verification.UNVERIFIABLE
    assert outcome.ok is False


def test_unreadable_readback_is_not_success():
    outcome = verify_text_value("Jane Doe", None, readable=False)
    assert outcome.verification is Verification.UNVERIFIABLE
    assert outcome.ok is False


def test_exact_readback_is_verified():
    outcome = verify_text_value("Jane Doe", "Jane Doe")
    assert outcome.verification is Verification.VERIFIED
    assert outcome.ok is True


def test_different_readback_is_mismatch_not_unknown():
    """A wrong value is worse than an unknown one, and must not be blurred into it."""
    outcome = verify_text_value("150000", "150")
    assert outcome.verification is Verification.MISMATCH
    assert outcome.failed is True
    assert "150" in (outcome.observed or "")


def test_formatting_noise_still_verifies_but_data_change_does_not():
    """Whitespace is presentation; the value is data."""
    assert verify_text_value("Jane  Doe", "Jane Doe").verification is Verification.VERIFIED
    assert verify_text_value("Jane Doe", "jane doe").verification is Verification.MISMATCH


def test_clearing_a_field_is_verified_when_it_is_empty():
    verify = verify_text_value("", "")
    assert verify.verification is Verification.VERIFIED
    leftover = verify_text_value("", "Jane Doe")
    assert leftover.verification is Verification.MISMATCH


def test_choice_comparison_tolerates_case_only():
    assert verify_choice("United States", "united states").verification is Verification.VERIFIED
    assert verify_choice("immediately", "two_weeks").verification is Verification.MISMATCH
    assert verify_choice("immediately", None, readable=False).verification is (
        Verification.UNVERIFIABLE
    )


def test_boolean_state_must_match_the_request():
    assert verify_boolean_state(True, True).verification is Verification.VERIFIED
    assert verify_boolean_state(True, False).verification is Verification.MISMATCH
    assert verify_boolean_state(True, None).verification is Verification.UNVERIFIABLE


def test_upload_verification_reports_what_the_page_holds():
    nothing = verify_upload("resume.pdf", [], readable=True)
    assert nothing.verification is Verification.UNVERIFIABLE

    wrong = verify_upload("resume.pdf", [ObservedFile(name="old-cv.pdf", size=10)])
    assert wrong.verification is Verification.MISMATCH

    same_name_different_revision = verify_upload(
        "resume.pdf", [ObservedFile(name="resume.pdf", size=99)], expected_size=10
    )
    assert same_name_different_revision.verification is Verification.MISMATCH

    good = verify_upload(
        "resume.pdf", [ObservedFile(name="resume.pdf", size=10)], expected_size=10
    )
    assert good.verification is Verification.VERIFIED


# ── B. click policy ──────────────────────────────────────────────────


def _button_submits_form(name: str, *, tag: str = "button", input_type: str = "") -> TargetFacts:
    return TargetFacts(name=name, tag=tag, input_type=input_type, submits_form=True)


def test_a_form_button_submits_whatever_it_is_called():
    """`Save` inside a form submits it. Only structure catches that."""
    from applyops.action_policy import ClickClass

    assert classify(_button_submits_form("Save")) is ClickClass.FINAL_SUBMIT


def test_input_type_submit_is_final():
    from applyops.action_policy import ClickClass

    target = TargetFacts(name="Anything", tag="input", input_type="submit")
    assert classify(target) is ClickClass.FINAL_SUBMIT


def test_named_submit_custom_control_is_final():
    from applyops.action_policy import ClickClass

    # Plenty of ATS forms trigger their own POST from a JS handler whose button
    # has type="button"; the name is the only signal left.
    target = TargetFacts(name="Submit application", tag="button", input_type="button")
    assert classify(target) is ClickClass.FINAL_SUBMIT


def test_advancing_a_form_is_never_blocked():
    from applyops.action_policy import ClickClass

    for name in ("Next", "Continue", "Review", "Back"):
        target = TargetFacts(name=name, tag="button", input_type="button")
        assert classify(target) is ClickClass.ADVANCE
        assert decide_click(target, authorized=False).allowed is True


def test_starting_an_easy_apply_is_not_the_final_submit():
    """LinkedIn's Apply opens the modal; it does not send anything anywhere."""
    from applyops.action_policy import ClickClass

    target = TargetFacts(name="Apply", tag="button", input_type="button")
    assert classify(target) is not ClickClass.FINAL_SUBMIT
    assert decide_click(target, authorized=False).allowed is True


def test_generic_click_cannot_reach_a_final_submit():
    decision = decide_click(_button_submits_form("Submit application"), authorized=False)
    assert decision.allowed is False
    assert decision.requires_grant is True


def test_unsupported_route_final_action_is_manual_only():
    decision = decide_click(
        _button_submits_form("Submit application"), authorized=True, route_supported=False
    )
    assert decision.allowed is False
    assert decision.manual_required is True


# ── C. authorization boundary ────────────────────────────────────────


def _fields() -> dict[str, str]:
    return {"Full name": "Jane Doe", "Phone": "+1 555 010 4477"}


def _approved_grant(auth: SubmissionAuthorizer, *, job_key: str = "job-1", **kwargs):
    request = auth.create_request(
        job_key=job_key,
        job_url="https://example.test/jobs/1",
        route="demo",
        platform="DemoATS",
        fields=kwargs.pop("fields", _fields()),
        resume_filename=kwargs.pop("resume_filename", "resume.pdf"),
        resume_sha256=kwargs.pop("resume_sha256", "deadbeef"),
        requested_by="mcp",
        **kwargs,
    )
    return request, auth.approve_request(request.request_id, source="cli_human")


def test_requesting_permission_grants_nothing():
    """The asking side cannot be the granting side -- that was `acknowledged=True`."""
    auth = SubmissionAuthorizer(_tmp())
    request = auth.create_request(
        job_key="job-1",
        job_url="https://example.test/jobs/1",
        route="demo",
        platform="DemoATS",
        fields=_fields(),
        requested_by="mcp",
    )
    assert request.status == "pending"
    assert auth.pending() == []  # no usable grant exists yet

    # Nothing was authorized, so nothing can be verified against.
    verdict = auth.verify(
        request.request_id,  # a request id is deliberately NOT a grant id
        job_key="job-1",
        fields=_fields(),
        resume_sha256="deadbeef",
        answers_revision="",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False
    assert verdict.reason == "unknown grant id"


def test_grant_verifies_against_the_approved_snapshot(tmp_path=None):
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)
    verdict = auth.verify(
        grant.grant_id,
        job_key="job-1",
        fields=_fields(),
        resume_sha256="deadbeef",
        answers_revision="",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is True


def test_grant_refuses_another_job():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth, job_key="job-1")
    verdict = auth.verify(
        grant.grant_id,
        job_key="job-2",
        fields=_fields(),
        resume_sha256="deadbeef",
        answers_revision="",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False
    assert "job" in verdict.reason


def test_grant_refuses_a_form_that_changed_after_approval():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)
    changed = {"Full name": "Someone Else", "Phone": "+1 555 010 4477"}
    verdict = auth.verify(
        grant.grant_id,
        job_key="job-1",
        fields=changed,
        resume_sha256="deadbeef",
        answers_revision="",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False
    assert "no longer matches" in verdict.reason


def test_grant_refuses_a_different_resume():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)
    verdict = auth.verify(
        grant.grant_id,
        job_key="job-1",
        fields=_fields(),
        resume_sha256="0000different",
        answers_revision="",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False


def test_grant_refuses_updated_facts():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)
    verdict = auth.verify(
        grant.grant_id,
        job_key="job-1",
        fields=_fields(),
        resume_sha256="deadbeef",
        answers_revision="rev-2",
        profile_revision="",
        route="demo",
    )
    assert verdict.ok is False


def test_grant_is_single_use_even_under_concurrency():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)

    results: list[bool] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        results.append(auth.consume(grant.grant_id).ok)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1, "exactly one caller may spend a grant"


def test_revoked_and_expired_grants_do_not_work():
    auth = SubmissionAuthorizer(_tmp())
    _, grant = _approved_grant(auth)
    assert auth.revoke(grant.grant_id) is True
    revoked = auth.verify(
        grant.grant_id, job_key="job-1", fields=_fields(), resume_sha256="deadbeef",
        answers_revision="", profile_revision="", route="demo",
    )
    assert revoked.ok is False and revoked.reason == "grant was revoked"

    expiring = SubmissionAuthorizer(_tmp(), ttl_seconds=0)
    _, grant2 = _approved_grant(expiring)
    expired = expiring.verify(
        grant2.grant_id, job_key="job-1", fields=_fields(), resume_sha256="deadbeef",
        answers_revision="", profile_revision="", route="demo",
    )
    assert expired.ok is False and "expired" in expired.reason


def test_digest_is_order_independent():
    """Field order comes from the page; it must not invalidate a real approval."""
    first = snapshot_digest(fields={"a": "1", "b": "2"}, resume_sha256="x",
                            answers_revision="", profile_revision="", route="demo")
    second = snapshot_digest(fields={"b": "2", "a": "1"}, resume_sha256="x",
                             answers_revision="", profile_revision="", route="demo")
    assert first == second


def test_summary_shows_the_values_not_a_narrative():
    request = SubmissionRequest(
        request_id="r1", job_key="job-1", job_url="https://example.test/jobs/1",
        route="demo", platform="DemoATS", fields={"Phone": "+1 555 010 4477"},
        resume_filename="resume.pdf", resume_sha256="a" * 64,
    )
    summary = request.summary_for_human()
    assert "+1 555 010 4477" in summary
    assert "resume.pdf" in summary


# ── D. resume: one source of truth ───────────────────────────────────


def test_resume_refuses_instead_of_guessing():
    with pytest.raises(ResumeNotConfigured):
        resolve_resume("")
    with pytest.raises(ResumeNotFound):
        resolve_resume("/nonexistent/resume.pdf")
    with pytest.raises(ResumeNotFound):
        resolve_resume("relative/resume.pdf")  # not absolute -> refuse, do not guess cwd
    wrong_type = _tmp() / "resume.png"
    wrong_type.write_bytes(b"\x89PNG\r\n")
    with pytest.raises(ResumeUnsupported):
        resolve_resume(str(wrong_type))


def test_resume_is_content_addressed_and_refuses_empty_file():
    root = _tmp()
    path = root / "resume.pdf"
    path.write_text("Jane Doe\nengineer\n", encoding="utf-8")

    ref = resolve_resume(str(path))
    assert ref.filename == "resume.pdf"
    assert len(ref.sha256) == 64

    same_content_elsewhere = root / "copy.pdf"
    same_content_elsewhere.write_text("Jane Doe\nengineer\n", encoding="utf-8")
    assert resolve_resume(str(same_content_elsewhere)).sha256 == ref.sha256

    changed = root / "v2.pdf"
    changed.write_text("Jane Doe\nstaff engineer\n", encoding="utf-8")
    assert resolve_resume(str(changed)).sha256 != ref.sha256

    empty = root / "empty.pdf"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ResumeUnreadable):
        resolve_resume(str(empty))


def test_resume_describe_is_human_readable():
    root = _tmp()
    path = write_sample_resume(root / "resume.pdf")
    description = resolve_resume(str(path)).describe()
    assert "resume.pdf" in description
    assert "sha256:" in description


# ── E. browser integration, local demo ATS only ──────────────────────


@pytest.mark.asyncio
async def test_fill_verifies_against_the_page():
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        try:
            name_ref = await _ref_for(browser, "Full name")
            good = await browser.fill_field(name_ref, "Jane Doe")
            assert good.verification == "verified"
            assert good.ok is True

            # The number field rejects letters: the page keeps nothing, so the
            # read-back is empty and success must not be claimed for it.
            years_ref = await _ref_for(browser, "Years of experience")
            bad = await browser.fill_field(years_ref, "Jane Doe")
            assert bad.verification == "unverifiable"
            assert bad.ok is False

            numeric = await browser.fill_field(years_ref, "5")
            assert numeric.verification == "verified"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_select_checkbox_and_upload_are_verified():
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        try:
            select = await browser.select_option(await _ref_for(browser, "Notice period"), "Two weeks")
            assert select.verification == "verified", select.detail

            checkbox = await browser.set_checkbox(await _ref_by_type(browser, "radio"), True)
            assert checkbox.verification == "verified", checkbox.detail
            assert checkbox.checked is True

            resume = write_sample_resume(_tmp() / "resume.pdf")
            upload = await browser.upload_file(await _ref_for(browser, "Resume"), str(resume))
            assert upload.verification == "verified", upload.detail
            assert upload.attachments == ["resume.pdf"]
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_preselected_resume_is_not_trusted_as_the_users_choice():
    """A form that arrives holding a file is the normal case, not a success."""
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url, scenario="stale")
        try:
            resume_ref = await _ref_for(browser, "Resume")
            observed, readable = await browser.read_attachments(resume_ref)
            assert readable is True
            assert observed == []  # nothing is actually attached yet

            # The page offers a stale file. Choosing it is not the same as the
            # user's configured resume being attached, and nothing claims it is.
            from applyops.verification import verify_upload as _verify

            outcome = _verify("my-resume.pdf", observed, readable=readable)
            assert outcome.verification is Verification.UNVERIFIABLE
            assert outcome.ok is False
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_generic_click_refuses_the_final_submit_and_nothing_is_sent():
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        try:
            facts, error = await browser.inspect_target(name="Submit application")
            assert facts is not None, error
            decision = decide_click(facts, authorized=False)
            assert decision.allowed is False
            assert decision.requires_grant is True

            # Proof that refusing meant something: nothing was submitted.
            found, _ = await browser.page_indicates(["Application received"])
            assert found is False
            assert "/form" in browser.page.url
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_unauthorized_submission_refuses_before_touching_the_page():
    """`execute_authorized_submission` with no grant must not reach the click."""
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        auth = SubmissionAuthorizer(_tmp())
        resume = write_sample_resume(_tmp() / "resume.pdf")
        try:
            with pytest.raises(SubmissionRefused):
                await execute_authorized_submission(
                    controller=browser,
                    authorizer=auth,
                    grant_id="no-such-grant",
                    job_key="job-1",
                    resume=resolve_resume(str(resume)),
                    route="demo",
                    action=FinalAction(name="Submit application", success_patterns=("Application received",)),
                )
            found, _ = await browser.page_indicates(["Application received"])
            assert found is False
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_form_changed_after_approval_sends_nothing():
    """The approval covered a snapshot. When the snapshot moves, so does its authority."""
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        auth = SubmissionAuthorizer(_tmp())
        resume = write_sample_resume(_tmp() / "resume.pdf")
        try:
            await browser.fill_field(await _ref_for(browser, "Full name"), "Jane Doe")
            snapshot = await browser.field_snapshot()
            request = auth.create_request(
                job_key="job-1",
                job_url=f"{ats.url}/form",
                route="demo",
                platform="DemoATS",
                fields=snapshot,
                resume_filename=resume.name,
                resume_sha256=resolve_resume(str(resume)).sha256,
                requested_by="mcp",
            )
            grant = auth.approve_request(request.request_id, source="cli_human")

            # Somebody edits the form after the human approved it.
            await browser.fill_field(await _ref_for(browser, "Full name"), "Someone Else")

            outcome = await execute_authorized_submission(
                controller=browser,
                authorizer=auth,
                grant_id=grant.grant_id,
                job_key="job-1",
                resume=resolve_resume(str(resume)),
                route="demo",
                action=FinalAction(name="Submit application", success_patterns=("Application received",)),
            )
            assert outcome.status == "failed"
            assert outcome.evidence["sent"] is False
            found, _ = await browser.page_indicates(["Application received"])
            assert found is False
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_authorized_submission_is_verified_and_spent_once():
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        auth = SubmissionAuthorizer(_tmp())
        resume = write_sample_resume(_tmp() / "resume.pdf")
        try:
            await browser.fill_field(await _ref_for(browser, "Full name"), "Jane Doe")
            await browser.select_option(await _ref_for(browser, "Notice period"), "Two weeks")
            snapshot = await browser.field_snapshot()
            request = auth.create_request(
                job_key="job-1",
                job_url=f"{ats.url}/form",
                route="demo",
                platform="DemoATS",
                fields=snapshot,
                resume_filename=resume.name,
                resume_sha256=resolve_resume(str(resume)).sha256,
                requested_by="mcp",
            )
            grant = auth.approve_request(request.request_id, source="cli_human")

            outcome = await execute_authorized_submission(
                controller=browser,
                authorizer=auth,
                grant_id=grant.grant_id,
                job_key="job-1",
                resume=resolve_resume(str(resume)),
                route="demo",
                action=FinalAction(name="Submit application", success_patterns=("Application received",)),
            )
            assert outcome.status == "verified"
            assert outcome.verified is True
            assert outcome.evidence["matched_text"] == "Application received"

            # Replaying the same grant afterwards must be refused outright.
            with pytest.raises(SubmissionRefused):
                await execute_authorized_submission(
                    controller=browser,
                    authorizer=auth,
                    grant_id=grant.grant_id,
                    job_key="job-1",
                    resume=resolve_resume(str(resume)),
                    route="demo",
                    action=FinalAction(name="Submit application"),
                )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_unconfirmed_submission_is_never_retried_and_never_confirmed():
    """A submit whose result is unknown stops everything that follow-up would do."""
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url, scenario="slow")
        auth = SubmissionAuthorizer(_tmp())
        resume = write_sample_resume(_tmp() / "resume.pdf")
        try:
            snapshot = await browser.field_snapshot()
            request = auth.create_request(
                job_key="job-slow",
                job_url=f"{ats.url}/form",
                route="demo",
                platform="DemoATS",
                fields=snapshot,
                resume_filename=resume.name,
                resume_sha256=resolve_resume(str(resume)).sha256,
                requested_by="mcp",
            )
            grant = auth.approve_request(request.request_id, source="cli_human")

            outcome = await execute_authorized_submission(
                controller=browser,
                authorizer=auth,
                grant_id=grant.grant_id,
                job_key="job-slow",
                resume=resolve_resume(str(resume)),
                route="demo",
                action=FinalAction(name="Submit application", success_patterns=("Application received",)),
                evidence_timeout=2.0,
            )
            assert outcome.status == "unverified"
            assert outcome.verified is False
            assert outcome.evidence["reconciliation_required"] is True

            # Reconciliation re-reads and never presses anything. What proves
            # that is not the verdict it returns -- the page may legitimately
            # confirm later -- but that no further grant was spent, so no second
            # submission can have been sent.
            grants_before = len(auth._read_all())
            used_before = sum(1 for g in auth._read_all() if g.used)
            url_before = browser.page.url

            reconciled = await reconcile_submission(
                controller=browser,
                action=FinalAction(success_patterns=("Application received",)),
                evidence_timeout=2.0,
            )

            assert len(auth._read_all()) == grants_before
            assert sum(1 for g in auth._read_all() if g.used) == used_before == 1
            assert url_before == browser.page.url  # no navigation was triggered
            assert reconciled.status in {"unverified", "verified"}
            assert reconciled.evidence["reconciled"] is True or reconciled.verified
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_snapshot_names_unreadable_fields_instead_of_hiding_them():
    with DemoATS() as ats:
        browser = await _new_browser_and_form(ats.url)
        try:
            snapshot = await browser.field_snapshot()
            assert snapshot, "the demo form must present answerable fields"
            assert "Full name" in snapshot
            # Nothing about a blank field may read as proof it is filled.
            assert snapshot["Full name"] == ""
        finally:
            await browser.close()


# ── F. history records outcomes honestly ─────────────────────────────


def test_history_defaults_to_unverified_and_counts_only_verified():
    root = _tmp()
    store = MemoryStore(root / "memory.json")

    store.add_application(job_url="https://example.test/jobs/1", platform="DemoATS",
                          apply_route="demo", status="applied")
    store.add_application(job_url="https://example.test/jobs/2", platform="DemoATS",
                          apply_route="demo", status="applied", outcome="verified")

    history = store.get_history()
    first = next(r for r in history if r["job_url"].endswith("/1"))
    second = next(r for r in history if r["job_url"].endswith("/2"))
    assert first["outcome"] == "unverified"
    assert second["outcome"] == "verified"

    platform = store.get_platform("DemoATS")
    assert (platform.runs, platform.successes) == (2, 1)


def test_unverified_guardrail_spends_the_slot_without_claiming_health():
    from applyops.guardrails import Guardrails

    root = _tmp()
    rails = Guardrails(root / "guard_state.json", daily_cap=2)
    rails.record_outcome(success=False)  # a failure should never count as progress
    before = rails.stats()["consecutive_failures"]
    assert before >= 1

    rails.record_unverified()
    stats = rails.stats()
    assert stats["applied_today"] == 1
    assert stats["consecutive_failures"] == before  # breaker not reset by an unknown

    rails.record_outcome(success=True)
    assert rails.stats()["consecutive_failures"] == 0
