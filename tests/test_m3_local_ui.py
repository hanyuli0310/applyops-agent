"""M3 -- Local Web UI.

The rule under test: **the pages show real backend state, and the whole demo
flow works end to end without a single mock** -- upload resume, prepare against
the local demo ATS, approve in the UI, submit, see verified evidence.

Two layers:

1. API tests through the real FastAPI app (TestClient): the contract the UI
   consumes, including the approval boundary (a grant only exists after a
   human-facing approve call).
2. One Playwright walk through the built frontend: the four pages, in order,
   ending in a verified result. This is the acceptance path from `PLAN.md` M3.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from applyops.api.app import create_app
from applyops.demo_ats import write_sample_resume


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-m3-"))


SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}


def _client(headless: bool = True, frontend: Path | None = None):
    """A client whose lifespan RUNS -- otherwise the app's browser is never
    closed, and a few tests in a row can exhaust the machine.

    It also carries the session token, exactly as the served console page does;
    without it every state-changing request is refused, which is the point of
    the token and would make these tests measure the wrong thing.
    """
    root = _tmp()
    app = create_app(root, frontend_dist=frontend, headless=headless)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers.update({"X-ApplyOps-Token": app.state.applyops.session_token})
    return client, root


@pytest.fixture
def client():
    """A console client with a fillable profile, like a returning user's."""
    client, root = _client(frontend=None)
    with client:
        client.post("/api/profile", json=dict(SYNTHETIC_PROFILE))
        client.post(
            "/api/resumes",
            files={
                "file": (
                    "resume.pdf",
                    write_sample_resume(root / "resume.pdf").read_bytes(),
                    "application/pdf",
                )
            },
        )
        yield client, root


def _approve_and_submit(client, app_id: str) -> dict:
    """Walk prepare -> (answer) -> approve -> submit, as the console does."""
    prepared = client.post(f"/api/applications/{app_id}/prepare").json()
    if prepared["state"] == "waiting_for_input":
        client.post(
            f"/api/applications/{app_id}/answer",
            json={"question": prepared["missing"][0], "answer": "Two weeks"},
        )
        prepared = client.post(f"/api/applications/{app_id}/prepare").json()
    assert prepared["state"] == "waiting_for_approval", prepared
    approved = client.post(f"/api/requests/{prepared['request_id']}/approve").json()
    return client.post(
        f"/api/applications/{app_id}/submit", json={"grant_id": approved["grant_id"]}
    ).json()


# ── A. API contract ──────────────────────────────────────────────────


def test_status_reports_setup_honestly():
    """A fresh install: nothing configured, and the gaps are listed."""
    client, _root = _client()
    with client:
        res = client.get("/api/status")
        assert res.status_code == 200
        body = res.json()
        assert body["profile_ready"] is False  # nothing configured yet
        assert body["missing_required"]  # and it says exactly what is missing
        assert body["browser_open"] is False


def test_profile_round_trip(client):
    client, _ = client
    res = client.post(
        "/api/profile",
        json={"name": "Jane Doe", "email": "jane@example.com", "phone": "+1 555 010 4477"},
    )
    assert res.status_code == 200
    reread = client.get("/api/profile").json()
    assert reread["values"]["name"] == "Jane Doe"


def test_profile_rejects_unknown_fields_rather_than_storing_them(client):
    client, _ = client
    res = client.post("/api/profile", json={"definitely_not_a_field": "x"})
    assert res.status_code == 422


def test_resume_upload_becomes_the_single_source_of_truth(client):
    client, root = client
    payload = write_sample_resume(root / "source.pdf").read_bytes()
    res = client.post(
        "/api/resumes",
        files={"file": ("my-resume.pdf", payload, "application/pdf")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["resume"]["filename"] == "my-resume.pdf"

    # The profile now points at the managed copy, and it is content-addressed.
    profile = client.get("/api/profile").json()
    assert profile["values"]["resume_path"].endswith("my-resume.pdf")
    resumes = client.get("/api/resumes").json()
    assert resumes["configured"] is True
    assert len(resumes["resumes"][0]["sha256"]) == 64


def test_resume_upload_rejects_unsupported_types(client):
    client, _ = client
    res = client.post(
        "/api/resumes",
        files={"file": ("photo.png", b"\x89PNG", "image/png")},
    )
    assert res.status_code == 415


def test_demo_flow_to_approval_through_the_api(client):
    """enqueue -> fill -> pending request -> approve -> grant exists.

    This is the boundary in UI form: `POST /requests/{id}/approve` is the one
    place a grant can be born, and it is only reachable by the human clicking.
    """
    client, _root = client

    enqueued = client.post("/api/applications", json={"job_url": ""})
    assert enqueued.status_code == 422  # a URL is required; nothing is guessed

    demo = client.post("/api/demo/start").json()
    app_id = demo["application"]["id"]

    first = client.post(f"/api/applications/{app_id}/prepare").json()
    if first["state"] == "waiting_for_input":
        client.post(
            f"/api/applications/{app_id}/answer",
            json={"question": first["missing"][0], "answer": "Two weeks"},
        )
        first = client.post(f"/api/applications/{app_id}/prepare").json()
    prepared = first
    assert prepared["state"] == "waiting_for_approval", prepared
    assert prepared["request_id"]

    requests = client.get("/api/requests").json()
    assert requests["count"] == 1
    assert "Full name" in requests["requests"][0]["summary"]

    approved = client.post(f"/api/requests/{prepared['request_id']}/approve").json()
    assert approved["approved"] is True
    assert approved["grant_id"]

    # A second approve is refused: the request was decided.
    assert client.post(f"/api/requests/{prepared['request_id']}/approve").status_code == 409

    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["application"]["state"] == "waiting_for_approval"


def test_submit_without_approval_is_refused_by_the_api(client):
    client, _root = client
    demo = client.post("/api/demo/start").json()
    app_id = demo["application"]["id"]

    # Prepare opens the real demo form, fills it and files the request -- but
    # nobody approves. The grant id here is fabricated.
    first = client.post(f"/api/applications/{app_id}/prepare").json()
    if first["state"] == "waiting_for_input":
        client.post(
            f"/api/applications/{app_id}/answer",
            json={"question": first["missing"][0], "answer": "Two weeks"},
        )
        client.post(f"/api/applications/{app_id}/prepare")
    res = client.post(f"/api/applications/{app_id}/submit", json={"grant_id": "made-up"})
    assert res.status_code == 403

    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["application"]["state"] != "submitted_verified"


def test_loopback_only_serve():
    from applyops.api.app import serve

    with pytest.raises(SystemExit):
        serve(_tmp(), host="0.0.0.0")


# ── B. UI end to end (built frontend, real backend) ──────────────────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_four_pages_end_to_end_in_a_real_browser():
    """The M3 acceptance path, on the built frontend:

    upload resume -> demo job -> prepare (which fills) -> answer what is
    missing -> prepare again -> approve -> submit -> verified.

    The step that matters most here is the middle one: the summary a person
    approves must contain the values that were actually filled in, because
    approving an empty form was the bug this milestone had to fix.
    """
    import uvicorn
    from playwright.async_api import async_playwright

    root = _tmp()
    frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if not (frontend_dist / "index.html").exists():
        pytest.skip("frontend not built; run `npm run build` in frontend/")

    app = create_app(root, frontend_dist=frontend_dist, headless=True)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "uvicorn did not start"

    base = f"http://127.0.0.1:{port}"
    try:
        # Seed the profile the way a user's earlier session would have. The API
        # wants the page's session token for state changes; the browser below
        # gets it from the served HTML.
        client = TestClient(app, base_url="http://127.0.0.1")
        client.headers.update({"X-ApplyOps-Token": app.state.applyops.session_token})
        client.post("/api/profile", json=dict(SYNTHETIC_PROFILE))
        payload = write_sample_resume(root / "resume.pdf").read_bytes()
        client.post("/api/resumes", files={"file": ("resume.pdf", payload, "application/pdf")})

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(base)

            # 1. 资料与简历 renders the seeded profile.
            await page.wait_for_selector("text=资料与简历")
            await page.wait_for_selector("text=resume.pdf")

            # 2. 岗位与偏好: one click adds the local demo job.
            await page.click("text=岗位与偏好")
            await page.click("[data-testid=demo-start]")
            await page.wait_for_selector("text=演示岗位已入队")

            # 3. 运行与记录: prepare. The form gets filled; whatever cannot be
            #    filled is named instead of being approved as a blank.
            await page.click("text=运行与记录")
            await page.wait_for_selector("[data-testid^=run-]")
            await page.click("[data-testid^=run-]")
            await page.wait_for_selector("[data-testid=run-message]")
            first = await page.text_content("[data-testid=run-message]")
            assert first and "缺" in first, first  # e.g. the notice period

            # 4. 待我处理: the missing field is listed, answer it, prepare again.
            await page.click("text=待我处理")
            await page.wait_for_selector("[data-testid=needs-input]")
            missing_text = await page.text_content("[data-testid=missing-list]")
            assert "Notice" in missing_text or "notice" in missing_text, missing_text

            app_row = await page.get_attribute("[data-testid=needs-input] input", "data-testid")
            assert app_row and app_row.startswith("answer-")
            application_id = app_row.removeprefix("answer-")
            await page.fill(f"[data-testid=answer-{application_id}]", "Two weeks")
            await page.click(f"[data-testid=save-answer-{application_id}]")
            await page.wait_for_selector("[data-testid=attention-message]")
            await page.click(f"[data-testid=reprepare-{application_id}]")
            await page.wait_for_selector("[data-testid=approval-request]")

            # The approval summary must contain the real values now.
            summary = await page.text_content("[data-testid=approval-request] pre.summary")
            assert "Jane Doe" in summary, summary
            assert "jane@example.com" in summary, summary
            await page.click("[data-testid=approve-button]")

            # Back to runs; submit; expect the verified verdict.
            await page.click("text=运行与记录")
            await page.wait_for_selector("[data-testid^=submit-]")
            await page.click("[data-testid^=submit-]")
            await page.wait_for_selector(
                "[data-testid=run-message], .banner.error", timeout=60000
            )
            message = await page.text_content("[data-testid=run-message]") if (
                await page.locator("[data-testid=run-message]").count()
            ) else None
            error_banner = (
                await page.text_content(".banner.error")
                if (await page.locator(".banner.error").count())
                else None
            )
            assert message and "已确认提交成功" in message, (
                f"message={message!r} error={error_banner!r}"
            )

            await browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
