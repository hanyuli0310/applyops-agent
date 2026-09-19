"""Blocker 1 -- the MCP flow cannot reach a submission.

Repro, as reported: `enqueue_application` leaves the application in `QUEUED`,
`request_submission_grant` files an approval request without touching the ledger,
and `submit_final` is then refused by the state machine -- because nothing ever
moved the application through `PREPARING` to `WAITING_FOR_APPROVAL`. There was no
public tool that could: the Web console and the runner both had their own
prepare implementations, and MCP had none.

The test walks the flow **only through public tools**:

    enqueue -> prepare (fill + verify) -> request grant -> human approve
            -> submit -> application_status

No direct database writes, no forged grants. The demo ATS must have received the
real field values and the resume.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.platforms.naming import resolve_route
from applyops.state_machine import ApplicationState

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
    "work_authorization": "authorized to work",
    "location": "Austin, TX",
}


class FakeServer:
    """Captures the registered tools so a test can call them directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *_args, **_kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-blocker1-"))


def _runtime(root: Path):
    from applyops.mcp.runtime import Runtime

    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    runtime = Runtime(root)
    runtime.memory = memory
    return runtime


def _tools(runtime):
    from applyops.mcp import tools as tool_module

    server = FakeServer()
    tool_module.register(server, runtime)
    return server.tools


def test_route_resolution_never_falls_back_to_demo():
    """One route vocabulary, and "unknown" stays unknown."""
    assert resolve_route("http://127.0.0.1:5000/form") == "demo"
    assert resolve_route("http://localhost:5000/form") == "demo"
    assert resolve_route("https://www.linkedin.com/jobs/view/123") == "easy_apply"
    # Anything we have no verified submission path for is external -- never demo.
    assert resolve_route("https://boards.greenhouse.io/acme/jobs/1") == "external"
    assert resolve_route("") == "external"
    assert resolve_route("https://example.test/apply") == "external"


@pytest.mark.asyncio
async def test_public_mcp_flow_reaches_a_verified_submission():
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)
    assert "prepare_application" in tools, (
        "MCP needs a public prepare path: enqueue alone leaves the application in "
        "QUEUED and submit_final is then refused by the state machine"
    )

    from applyops.browser import BrowserController

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        runtime._browser = browser  # the harness owns the browser
        try:
            job_url = f"{ats.url}/form"

            # 1. Public tool: enqueue.
            enqueued = json.loads(
                await tools["enqueue_application"](job_url=job_url, job_id="job-mcp")
            )
            application_id = enqueued["application"]["id"]
            assert enqueued["application"]["state"] == ApplicationState.QUEUED.value
            assert enqueued["application"]["route"] == "demo"

            # 2. Public tool: prepare -- fills, verifies, and advances the state.
            prepared = json.loads(
                await tools["prepare_application"](application_id=application_id)
            )
            assert prepared["state"] != ApplicationState.QUEUED.value, prepared
            if prepared["state"] == ApplicationState.WAITING_FOR_INPUT.value:
                # The demo form's sponsorship group and notice period are the two
                # things a bare profile cannot answer; answer them the way a user
                # would (scoped answers), then prepare again.
                for question, answer in (
                    ("Notice period", "Two weeks"),
                    ("Do you now, or will you in the future, require visa sponsorship?", "No"),
                ):
                    runtime.service.answers.set_answer(question, answer)
                    assert application_id in json.dumps(prepared)
                prepared = json.loads(
                    await tools["prepare_application"](application_id=application_id)
                )
            assert prepared["state"] == ApplicationState.WAITING_FOR_APPROVAL.value, prepared
            assert prepared["request_id"]

            # 3. Public tool: request the grant (the request already exists for
            #    this application; asking again must not fork a second ledger row).
            grant_request = json.loads(
                await tools["request_submission_grant"](
                    job_url=job_url, job_id="job-mcp", application_id=application_id
                )
            )
            assert grant_request["status"] == "pending", grant_request
            request_id = grant_request["request_id"]

            # 4. The existing human approval entry (the CLI's own code path).
            grant = runtime.authorizer.approve_request(request_id, source="cli_human")
            assert grant is not None and grant.application_id == application_id

            # 5. Public tool: submit.
            submitted = json.loads(
                await tools["submit_final"](
                    grant_id=grant.grant_id,
                    job_url=job_url,
                    job_id="job-mcp",
                    application_id=application_id,
                    route=prepared["route"],
                )
            )
            assert submitted.get("submitted") is True, submitted
            assert submitted["status"] == "verified", submitted

            # 6. Public tool: read the result back.
            status = json.loads(await tools["application_status"](application_id=application_id))
            assert status["application"]["state"] == ApplicationState.SUBMITTED_VERIFIED.value
            assert [a["outcome"] for a in status["attempts"]] == ["verified"]

            # 7. The ATS received the real values and the resume.
            received = ats.last_submission
            assert received["fields"]["name"] == "Jane Doe"
            assert received["fields"]["email"] == "jane@example.com"
            assert received["files"]["resume"] == "resume.pdf"
        finally:
            runtime._browser = None
            await browser.close()


@pytest.mark.asyncio
async def test_mcp_prepare_refuses_a_route_it_cannot_drive():
    """An unknown route must not be quietly driven as the demo route."""
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)
    if "prepare_application" not in tools:
        pytest.skip("prepare_application arrives with the fix")

    from applyops.browser import BrowserController

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        runtime._browser = browser
        try:
            # A row whose route is external: MCP may not prepare it as demo.
            row = runtime.service.enqueue(
                job_url="https://example.test/apply",
                job_id="job-external",
                route=resolve_route("https://example.test/apply"),
                platform="Unknown",
            )
            assert row.route == "external"
            payload = json.loads(await tools["prepare_application"](application_id=row.id))
            assert payload["state"] == ApplicationState.WAITING_FOR_INPUT.value, payload
            assert "external" in json.dumps(payload) or "route" in json.dumps(payload)
            assert ats.last_submission == {}  # nothing was sent anywhere
        finally:
            runtime._browser = None
            await browser.close()
