"""Acceptance fixes for v0.2 -- regression tests for each reported problem.

One test file per concern would be tidier, but these belong together: they are
the nine things that were wrong in the same review, and keeping them adjacent
makes it obvious that fixing one did not quietly re-break another.

Each test names the problem it closes, and each one fails against the code as it
was before this pass.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from applyops.api.app import create_app
from applyops.authorization import SubmissionAuthorizer, page_identity_of
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.ledger import Ledger
from applyops.memory import MemoryStore
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.state_machine import ApplicationState
from applyops.submission import SubmissionRefused

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "location": "Austin, TX",
    "work_authorization": "authorized to work",
    "requires_sponsorship": "no",
    "years_experience": "4",
    "current_title": "Backend Engineer",
    "current_company": "Acme",
    "expected_salary": "180000",
    "salary_currency": "USD",
    "willing_locations": "Remote, Austin",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-fix-"))


def _client(root: Path, frontend: Path | None = None):
    """A client that carries the session token.

    This is what the real console does: the server bakes the token into
    index.html, and every state-changing request from that page sends it back.
    """
    app = create_app(root, frontend_dist=frontend, headless=True)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update({"X-ApplyOps-Token": app.state.applyops.session_token})
    return client, app


async def _browser(root: Path):
    from applyops.browser import BrowserController

    controller = BrowserController(headless=True, user_data_dir=root / "chrome")
    await controller.launch()
    return controller


def _set_up(root: Path) -> None:
    """Profile + resume, the two things every run needs."""
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )


# ── 1. MCP authorization flow ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_request_grant_is_callable_and_the_approve_channel_exists():
    """`request_submission_grant` used to raise TypeError on `source=`.

    The parameter is `requested_by`; the tool passed `source`, so the MCP
    request flow could not run at all. This drives the real tool function.
    """
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
    # Profile before the runtime: a MemoryStore reads its file when it is built,
    # and a runtime built against an empty profile would legitimately refuse.
    _set_up(root)
    runtime = Runtime(root)
    server = FakeServer()
    tool_module.register(server, runtime)
    assert "request_submission_grant" in server.tools

    with DemoATS() as ats:
        browser = await _browser(root)
        assert runtime.browser is None
        runtime._browser = browser  # the harness owns the browser in tests
        try:
            await browser.goto(f"{ats.url}/form", settle=0.4)

            row = runtime.service.enqueue(
                job_url=f"{ats.url}/form", job_id="job-mcp", route="demo", platform="DemoATS"
            )
            payload = json.loads(
                await server.tools["request_submission_grant"](
                    job_url=f"{ats.url}/form", job_id="job-mcp", application_id=row.id
                )
            )
            assert payload["status"] == "pending", payload
            request_id = payload["request_id"]
            assert "job" in payload["summary_to_show"]

            # The documented human channel must exist: `applyops approve`.
            listed = subprocess.run(  # noqa: ASYNC221, PLW1510 - returncode asserted below
                [sys.executable, "-m", "applyops.main", "--data-dir", str(root), "approve"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert listed.returncode == 0
            assert request_id in listed.stdout

            # A human approves (this is exactly what the CLI does internally).
            grant = runtime.authorizer.approve_request(request_id, source="cli_human")
            assert grant is not None and grant.application_id == row.id
        finally:
            runtime._browser = None
            await browser.close()


def test_approve_refuses_to_decide_non_interactively():
    """The one thing the human channel must not become: scriptable by an agent."""
    from applyops.approve import decide
    from applyops.authorization import SubmissionAuthorizer

    root = _tmp()
    authorizer = SubmissionAuthorizer(root)
    request = authorizer.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"Full name": "Jane Doe"}, requested_by="mcp",
    )
    # stdin is not a tty under pytest, which is the case being asserted.
    assert decide(authorizer, request.request_id) == 2
    assert authorizer.get_request(request.request_id).status == "pending"


# ── 2. prepare must fill, verify and attach -- or park ────────────────


def test_prepare_fills_the_form_and_the_ats_receives_jane_doe():
    """The bug: prepare read an empty form and asked for approval of nothing.

    Now: fill -> verify -> upload -> verify attachment -> *then* ask. And the
    assertion is about what the ATS actually received, not about a page saying
    "received".
    """
    root = _tmp()
    _set_up(root)
    client, app = _client(root)
    with client:
        demo = client.post("/api/demo/start").json()
        app_id = demo["application"]["id"]

        prepared = client.post(f"/api/applications/{app_id}/prepare").json()
        assert prepared["state"] == "waiting_for_input", prepared
        # The demo form asks for a notice period, which nothing knows yet.
        assert any("notice" in m.lower() or "Notice" in m for m in prepared["missing"]), prepared

        # Answer it for this application only, then prepare again.
        client.post(
            f"/api/applications/{app_id}/answer",
            json={"question": "Notice period", "answer": "Two weeks"},
        )
        again = client.post(f"/api/applications/{app_id}/prepare").json()
        assert again["state"] == "waiting_for_approval", again
        assert again["filled"] >= 3

        # Approve and submit for real; then check what the ATS received.
        grant = client.post(f"/api/requests/{again['request_id']}/approve").json()
        outcome = client.post(
            f"/api/applications/{app_id}/submit", json={"grant_id": grant["grant_id"]}
        ).json()
        assert outcome["status"] == "verified", outcome

        received = app.state.applyops.demo_ats.last_submission
        assert received["fields"]["name"] == "Jane Doe"
        assert received["fields"]["email"] == "jane@example.com"
        assert received["fields"]["phone"] == "+1 555 010 4477"
        assert received["fields"]["years"] == "4"
        # The ATS receives the option's value attribute, not its visible text --
        # which is what a real backend stores.
        assert received["fields"]["notice_period"] == "two_weeks"
        # The profile answers "requires_sponsorship: no", so the radio for "no"
        # is the one that must have been selected -- and it is the value the ATS
        # receives, not the label we clicked.
        assert received["fields"]["needs_sponsorship"] == "no"
        assert received["files"]["resume"] == "resume.pdf"


@pytest.mark.asyncio
async def test_demo_ats_rejects_an_empty_or_partial_application():
    """A demo that accepts anything cannot tell a working filler from a broken one."""
    from applyops.browser import BrowserController

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=_tmp() / "chrome")
        await browser.launch()
        try:
            await browser.goto(f"{ats.url}/form", settle=0.4)
            # Click submit with nothing filled in -- the old demo said "received".
            from applyops.evidence import success_patterns_for

            await browser.click(name="Submit application")
            await asyncio.sleep(1.0)

            # Layer one: the page itself refuses to submit (required fields), so
            # nothing reaches the server at all.
            found, _ = await browser.page_indicates(list(success_patterns_for(browser.page.url)))
            assert found is False
            assert ats.last_submission == {}, "an invalid form must not be posted"

            # Layer two: even if something did post an empty body, the ATS
            # rejects it rather than answering "received".
                        
            request = urllib.request.Request(
                f"{ats.url}/submit",
                data=b"",
                headers={"Content-Type": "multipart/form-data; boundary=none"},
                method="POST",
            )
            with urllib.request.urlopen(  # noqa: ASYNC210 - test-local HTTP call
                request, timeout=10
            ) as response:
                body = response.read().decode("utf-8").lower()
            assert "application received" not in body
            assert "problem with your submission" in body
            assert ats.last_submission["problems"]
        finally:
            await browser.close()


# ── 3. grants are bound to their application ─────────────────────────


def test_three_applications_each_submit_with_their_own_grant():
    """The whole loop, three times over, one browser and one ledger.

    Order matters and mirrors real use: prepare, approve and submit each
    application **while its page is loaded**. Looking back at this test later,
    note that this is not incidental -- submitting app 1 after navigating to
    app 2's page is refused by the identity check, which the next test covers.
    """
    root = _tmp()
    _set_up(root)
    client, app = _client(root)
    with client:
        # This test is about grants being bound to their application, not about
        # the interval between submissions: three in a row would otherwise be
        # spaced 45-180s apart by the rails (which is correct behaviour, and is
        # asserted in tests/test_blocker_concurrency.py).
        app.state.applyops.guardrails.min_gap = (0.0, 0.0)

        submitted: list[str] = []
        for _ in range(3):
            demo = client.post("/api/demo/start").json()
            app_id = demo["application"]["id"]
            assert app_id not in submitted

            first = client.post(f"/api/applications/{app_id}/prepare").json()
            if first["state"] == "waiting_for_input":
                field = first["missing"][0]
                client.post(
                    f"/api/applications/{app_id}/answer",
                    json={"question": field, "answer": "Two weeks"},
                )
                first = client.post(f"/api/applications/{app_id}/prepare").json()
            assert first["state"] == "waiting_for_approval", first

            approved = client.post(f"/api/requests/{first['request_id']}/approve").json()
            # The API tells the UI which application the grant belongs to.
            assert approved["application_id"] == app_id

            outcome = client.post(
                f"/api/applications/{app_id}/submit", json={"grant_id": approved["grant_id"]}
            ).json()
            assert outcome["status"] == "verified", outcome
            submitted.append(app_id)

            # This application's grant is spent; replaying it is refused.
            replay = client.post(
                f"/api/applications/{app_id}/submit", json={"grant_id": approved["grant_id"]}
            )
            assert replay.status_code in {403, 409}

        assert len(set(submitted)) == 3
        # Every application ended verified, each with its own attempt row.
        for app_id in submitted:
            detail = client.get(f"/api/applications/{app_id}").json()
            assert detail["application"]["state"] == "submitted_verified"
            assert [a["outcome"] for a in detail["attempts"]] == ["verified"]

        # And the ATS saw three separate submissions, each with its own data.
        assert len(app.state.applyops.service.list("submitted_verified")) == 3


def test_wrong_grant_for_the_wrong_application_is_refused():
    """A grant minted for A must be refused when aimed at B."""
    root = _tmp()
    _set_up(root)
    service = ApplicationService(
        root, memory=MemoryStore(root / "memory.json"), guardrails=None
    )
    authorizer = SubmissionAuthorizer(root)

    a = service.enqueue(job_url="https://example.test/a", job_id="job-a", route="demo")
    b = service.enqueue(job_url="https://example.test/b", job_id="job-b", route="demo")
    for row in (a, b):
        service.ledger.transition(row.id, ApplicationState.PREPARING)
        service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)

    request_a = authorizer.create_request(
        job_key="job-a", job_url="https://example.test/a", route="demo", platform="DemoATS",
        fields={"Full name": "Jane Doe"}, application_id=a.id,
        page_url="https://example.test/a", requested_by="test",
    )
    grant_a = authorizer.approve_request(request_a.request_id, source="cli_human")

    verdict = authorizer.verify(
        grant_a.grant_id,
        job_key="job-b",
        fields={"Full name": "Jane Doe"},
        resume_sha256="", answers_revision="", profile_revision="", route="demo",
        application_id=b.id,
        page_url="https://example.test/b",
    )
    assert verdict.ok is False
    assert "not transferable" in verdict.reason or "job" in verdict.reason


def test_expired_and_used_grants_are_both_refused():
    root = _tmp()
    authorizer = SubmissionAuthorizer(root, ttl_seconds=0)
    request = authorizer.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"x": "y"}, requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")
    expired = authorizer.verify(
        grant.grant_id, job_key="job-1", fields={"x": "y"}, resume_sha256="",
        answers_revision="", profile_revision="", route="demo",
    )
    assert expired.ok is False and "expired" in expired.reason

    fresh = SubmissionAuthorizer(_tmp())
    request2 = fresh.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"x": "y"}, requested_by="test",
    )
    grant2 = fresh.approve_request(request2.request_id, source="cli_human")
    assert fresh.consume(grant2.grant_id).ok is True
    used = fresh.verify(
        grant2.grant_id, job_key="job-1", fields={"x": "y"}, resume_sha256="",
        answers_revision="", profile_revision="", route="demo",
    )
    assert used.ok is False and "already used" in used.reason


# ── 4. page identity: the browser must be on the right posting ───────


def test_page_identity_ignores_noise_and_keeps_the_posting():
    same = page_identity_of("https://example.test/jobs/42/?tracking=x#top")
    assert same == page_identity_of("http://example.test/jobs/42")
    assert same != page_identity_of("https://example.test/jobs/43")


def test_grant_refuses_when_the_browser_sits_on_another_posting():
    """Two postings, identical forms: the snapshot matches, the page does not."""
    root = _tmp()
    authorizer = SubmissionAuthorizer(root)
    request = authorizer.create_request(
        job_key="job-a", job_url="https://example.test/jobs/a", route="demo",
        platform="DemoATS", fields={"Full name": ""}, application_id="app-a",
        page_url="https://example.test/jobs/a", requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")

    verdict = authorizer.verify(
        grant.grant_id, job_key="job-a", fields={"Full name": ""}, resume_sha256="",
        answers_revision="", profile_revision="", route="demo",
        application_id="app-a",
        page_url="https://example.test/jobs/b",
    )
    assert verdict.ok is False
    assert "browser is on" in verdict.reason


@pytest.mark.asyncio
async def test_submit_refuses_when_the_browser_moved_to_another_application():
    """Prepare A, prepare B, then try to submit A while the page shows B."""
    root = _tmp()
    _set_up(root)

    with DemoATS() as ats:
        browser = await _browser(root)
        service = ApplicationService(root, memory=MemoryStore(root / "memory.json"))
        authorizer = SubmissionAuthorizer(root)
        try:
            a = service.enqueue(job_url=f"{ats.url}/form", job_id="job-a", route="demo", platform="DemoATS")
            b = service.enqueue(
                job_url=f"{ats.url}/form?other=1", job_id="job-b", route="demo", platform="DemoATS"
            )

            # Approve A while the browser is on A's page.
            await browser.goto(a.job_url, settle=0.4)
            request_a = authorizer.create_request(
                job_key="job-a", job_url=a.job_url, route="demo", platform="DemoATS",
                fields=await browser.field_snapshot(), application_id=a.id,
                page_url=browser.page.url, requested_by="test",
            )
            grant_a = authorizer.approve_request(request_a.request_id, source="cli_human")
            for row in (a, b):
                service.ledger.transition(row.id, ApplicationState.PREPARING)
                service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)

            # Now the browser is showing B.
            await browser.goto(b.job_url, settle=0.4)

            from applyops.evidence import detect_final_action

            action, detail = await detect_final_action(browser)
            assert action is not None, detail

            outcome = await service.submit(
                a.id,
                grant_id=grant_a.grant_id,
                controller=browser,
                resume=resolve_resume(str(root / "resume.pdf")),
                action=action,
            )

            # Refused *before* anything was sent, and reported as exactly that:
            # a failure to submit, not an unknown result.
            assert outcome.status == "failed"
            assert outcome.evidence["sent"] is False
            assert "browser is on" in outcome.evidence["reason"]

            # The approval is not burned by the mistake -- the user can navigate
            # back to A's page and use it, which is what makes the refusal kind.
            assert authorizer.peek(grant_a.grant_id).used is False

            # And nothing reached the demo ATS.
            assert "Application received" not in (
                await browser.page.inner_text("body")
            )
        finally:
            await browser.close()


# ── 5. one execution core ────────────────────────────────────────────


def test_submit_application_reports_evidence_and_cannot_invent_a_verdict():
    """`submit_application(outcome="verified")` used to write a success."""
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
    runtime = Runtime(root)
    server = FakeServer()
    tool_module.register(server, runtime)

    # The tool no longer takes an outcome at all.
    signatures = server.tools["submit_application"].__code__.co_varnames
    assert "outcome" not in signatures

    # An unspent grant reports "nothing to report" rather than success.
    authorizer = SubmissionAuthorizer(root)
    request = authorizer.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"x": "y"}, requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")

    payload = json.loads(
        asyncio.run(
            server.tools["submit_application"](
                grant_id=grant.grant_id, job_url="https://example.test/1"
            )
        )
    )
    assert payload["recorded"] is False
    assert "never spent" in payload["error"]


def test_service_applies_the_rails_for_every_driver():
    """Daily cap and dedupe used to live only in the MCP wrapper."""
    from applyops.guardrails import Guardrails

    root = _tmp()
    memory = MemoryStore(root / "memory.json")
    rails = Guardrails(root / "guard_state.json", memory=memory, daily_cap=0)
    service = ApplicationService(root, memory=memory, guardrails=rails)
    row = service.enqueue(job_url="https://example.test/1", job_id="job-1", route="demo")
    service.ledger.transition(row.id, ApplicationState.PREPARING)
    service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)

    async def attempt():
        return await service.submit(
            row.id,
            grant_id="whatever",
            controller=None,  # never reached: the rails refuse first
            resume=resolve_resume(str(write_sample_resume(root / "r.pdf"))),
            action=None,
        )

    with pytest.raises(SubmissionRefused) as excinfo:
        asyncio.run(attempt())
    assert "daily cap" in str(excinfo.value)


# ── 6. browser concurrency ───────────────────────────────────────────


_PROFILE_LOCK_CHILD = """
import sys
from pathlib import Path

from applyops.concurrency import FileLock

lock = FileLock(Path(sys.argv[1]), purpose="child", block=False)
print("ACQUIRED" if lock.acquire() else "REFUSED")
"""


def test_the_console_profile_lock_is_held_across_processes():
    """Two Chrome instances on one profile rewrite each other's cookies.

    The check has to be made from another *process*: within one process the lock
    is deliberately shareable (that is how a supervisor drives the in-process
    tools without deadlocking against itself), so an in-process test would prove
    nothing about two programs.
    """
    import os

    from applyops.concurrency import FileLock, browser_lock_path

    root = _tmp()
    lock_path = browser_lock_path(root)
    holder = FileLock(lock_path, purpose="ui browser", block=False)
    assert holder.acquire() is True
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
        # The real path for descriptor inheritance is a supervisor passing the
        # fd; a plain subprocess must not inherit the claim by accident.
        env.pop("APPLYOPS_LOCK_FD", None)
        result = subprocess.run(  # noqa: PLW1510 - the output is the assertion
            [sys.executable, "-c", _PROFILE_LOCK_CHILD, str(lock_path)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert "REFUSED" in result.stdout, (result.stdout, result.stderr)
    finally:
        holder.release()

    # Released: the next process may take it.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    env.pop("APPLYOPS_LOCK_FD", None)
    after = subprocess.run(  # noqa: PLW1510 - the output is the assertion
        [sys.executable, "-c", _PROFILE_LOCK_CHILD, str(lock_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "ACQUIRED" in after.stdout, (after.stdout, after.stderr)


# ── 7. revision binding ──────────────────────────────────────────────


def test_revisions_are_real_values_and_profile_changes_void_a_grant():
    root = _tmp()
    _set_up(root)
    memory = MemoryStore(root / "memory.json")
    service = ApplicationService(root, memory=memory)
    authorizer = SubmissionAuthorizer(root)

    profile_revision, answers_revision = service.revisions()
    assert profile_revision not in {"", "no-profile"}
    assert answers_revision

    request = authorizer.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"Full name": "Jane Doe"},
        answers_revision=answers_revision, profile_revision=profile_revision,
        requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")

    # The user edits their profile after approving.
    memory.update_profile({"phone": "+1 555 010 9999"})
    new_profile_revision, _ = service.revisions()
    assert new_profile_revision != profile_revision

    verdict = authorizer.verify(
        grant.grant_id, job_key="job-1", fields={"Full name": "Jane Doe"},
        resume_sha256="", answers_revision=answers_revision,
        profile_revision=new_profile_revision, route="demo",
    )
    assert verdict.ok is False


def test_a_new_scoped_answer_voids_a_pending_grant():
    root = _tmp()
    _set_up(root)
    memory = MemoryStore(root / "memory.json")
    service = ApplicationService(root, memory=memory)
    authorizer = SubmissionAuthorizer(root)

    profile_revision, answers_revision = service.revisions()
    request = authorizer.create_request(
        job_key="job-1", job_url="https://example.test/1", route="demo",
        platform="DemoATS", fields={"Notice period": ""},
        answers_revision=answers_revision, profile_revision=profile_revision,
        requested_by="test",
    )
    grant = authorizer.approve_request(request.request_id, source="cli_human")

    service.answers.set_answer("Notice period", "Two weeks")
    _, new_answers_revision = service.revisions()
    assert new_answers_revision != answers_revision

    verdict = authorizer.verify(
        grant.grant_id, job_key="job-1", fields={"Notice period": "Two weeks"},
        resume_sha256="", answers_revision=new_answers_revision,
        profile_revision=profile_revision, route="demo",
    )
    assert verdict.ok is False


# ── 8. M4 flow in the UI ─────────────────────────────────────────────


def test_waiting_for_input_names_what_is_missing_and_can_be_resumed():
    root = _tmp()
    _set_up(root)
    client, _app = _client(root)
    with client:
        demo = client.post("/api/demo/start").json()
        app_id = demo["application"]["id"]
        prepared = client.post(f"/api/applications/{app_id}/prepare").json()
        assert prepared["state"] == "waiting_for_input"

        status = client.get("/api/runner/status").json()
        entry = next(
            w for w in status["waiting_for_input"] if w["application_id"] == app_id
        )
        assert entry["missing"], "the UI must be able to say what is missing"

        client.post(
            f"/api/applications/{app_id}/answer",
            json={"question": entry["missing"][0], "answer": "Two weeks"},
        )
        resumed = client.post(f"/api/applications/{app_id}/prepare").json()
        assert resumed["state"] == "waiting_for_approval"


def test_runner_controls_and_policy_reach_the_real_runner():
    root = _tmp()
    _set_up(root)
    client, _app = _client(root)
    with client:
        assert client.post("/api/runner/pause").json()["paused"] is True
        assert client.get("/api/runner/status").json()["paused"] is True
        assert client.post("/api/runner/resume").json()["paused"] is False
        policy = client.post(
            "/api/runner/policy",
            json={"enabled": True, "max_applications": 1, "allowed_platforms": [], "ttl_minutes": 30},
        ).json()
        assert policy["enabled"] is True and policy["max_applications"] == 1
        status = client.get("/api/runner/status").json()
        assert status["policy_usable"] is True

        # A pass must say how many it may send, and with the policy on but no
        # approvals it submits nothing.
        refused = client.post("/api/runner/pass", json={})
        assert refused.status_code == 422, refused.text

        report = client.post("/api/runner/pass", json={"budget": 1}).json()
        assert report["submitted"] == []
        assert report["budget"] == 1
        assert report["budget_remaining"] == 1


def test_preferences_explain_why_a_posting_is_kept_or_filtered():
    root = _tmp()
    client, _app = _client(root)
    with client:
        client.post(
            "/api/preferences",
            json={
                "target_titles": ["backend engineer"],
                "locations": ["Remote"],
                "include_keywords": [],
                "exclude_keywords": ["intern"],
                "exclude_companies": [],
            },
        )
        kept = client.post(
            "/api/preferences/preview",
            json={"title": "Senior Backend Engineer", "location": "Remote, US"},
        ).json()
        assert kept["keep"] is True and kept["reasons"]

        filtered = client.post(
            "/api/preferences/preview",
            json={"title": "Frontend Engineer", "location": "Remote"},
        ).json()
        assert filtered["keep"] is False
        assert "target title" in filtered["reasons"][0]

        # A location rule explains itself too, and says what it compared.
        off_location = client.post(
            "/api/preferences/preview",
            json={"title": "Backend Engineer", "location": "Austin, TX"},
        ).json()
        assert off_location["keep"] is False
        assert "Austin" in json.dumps(off_location["reasons"])


# ── 9. M5 finishing touches ──────────────────────────────────────────


def test_local_api_requires_the_session_token_for_state_changes():
    root = _tmp()
    app = create_app(root, headless=True)
    token = app.state.applyops.session_token
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Reads stay open on loopback so the CLI and doctor keep working.
        assert client.get("/api/status").status_code == 200

        # A state change without the token is refused -- this is what stops a
        # random page the user has open from approving their submission.
        assert client.post("/api/runner/pause").status_code == 403
        assert (
            client.post("/api/runner/pause", headers={"X-ApplyOps-Token": "wrong"}).status_code
            == 403
        )

        ok = client.post("/api/runner/pause", headers={"X-ApplyOps-Token": token})
        assert ok.status_code == 200


def test_local_api_refuses_a_foreign_host_or_origin():
    root = _tmp()
    client, _app = _client(root)
    with client:
        assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 403
        assert (
            client.get("/api/status", headers={"origin": "http://evil.example"}).status_code
            == 403
        )


def test_stop_does_not_signal_a_pid_that_is_not_our_console():
    """macOS has no /proc; "the file says so" was the whole check."""
    from applyops.main import stop

    root = _tmp()
    (root / "ui.pid").write_text(json.dumps({"pid": 999999999, "port": 1}), encoding="utf-8")
    # Nothing answers on the recorded port, so this must refuse to signal.
    assert stop(root) == 1
    assert not (root / "ui.pid").exists()


def test_stop_signals_a_console_that_identifies_itself():
    from applyops.main import stop

    root = _tmp()
    _set_up(root)
    app = create_app(root, headless=True)
    import uvicorn

    class _Server(uvicorn.Server):
        pass

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = _Server(config)
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]

    (root / "ui.pid").write_text(
        json.dumps({"pid": 999999999, "port": port}), encoding="utf-8"
    )
    # It answered like ApplyOps, so `stop` accepts ownership and proceeds to
    # signal; the pid is fake, and the "already gone" path is taken. Either way
    # the run is a success and the record is cleaned up -- which is what proves
    # the ownership check passed rather than the pid being signalled blind.
    assert stop(root) == 0
    assert not (root / "ui.pid").exists()
    server.should_exit = True
    thread.join(timeout=10)


def test_frontend_build_resolution_prefers_a_packaged_console():
    from applyops import main as main_module

    assert hasattr(main_module, "frontend_dist")
    # In a checkout the repo build is found; the packaged path is tried first and
    # simply does not exist here.
    dist = main_module.frontend_dist()
    assert dist is not None and (dist / "index.html").exists()


# ── 10. restart / recovery / migration still hold ────────────────────


def test_restart_keeps_state_and_recovery_lands_safely():
    root = _tmp()
    service = ApplicationService(root, memory=MemoryStore(root / "memory.json"))
    row = service.enqueue(job_url="https://example.test/1", job_id="job-1", route="demo")
    service.ledger.transition(row.id, ApplicationState.PREPARING)
    service.ledger.transition(row.id, ApplicationState.WAITING_FOR_APPROVAL)
    service.ledger.transition(row.id, ApplicationState.SUBMITTING)
    with service.ledger._conn:
        service.ledger._conn.execute(
            "UPDATE applications SET claim_expires_at = 0 WHERE id = ?", (row.id,)
        )

    reopened = ApplicationService(root, memory=MemoryStore(root / "memory.json"))
    recovered = reopened.recover()
    assert [r.id for r in recovered] == [row.id]
    assert reopened.get(row.id).state == ApplicationState.SUBMITTED_UNVERIFIED.value


def test_legacy_migration_still_never_invents_success():
    root = _tmp()
    (root / "memory.json").write_text(
        json.dumps({"application_history": [{"job_url": "https://x.test/1", "job_id": "old-1"}]}),
        encoding="utf-8",
    )
    service = ApplicationService(root, memory=MemoryStore(root / "memory.json"))
    result = service.import_legacy_history()
    assert result["imported"] == 1
    row = service.ledger.find_by_job_key("old-1")
    assert row.state == ApplicationState.LEGACY_IMPORTED.value
    assert Ledger(root / "app.sqlite").attempts(row.id) == []
