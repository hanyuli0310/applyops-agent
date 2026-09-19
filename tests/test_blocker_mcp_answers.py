"""Blocker: the MCP answer path did not reach the filler's answer store.

Repro, as reported:

    prepare_application -> waiting_for_input ("Notice period")
    -> the user answers
    -> record_answer("Notice period", "Two weeks")     # public MCP tool
    -> prepare_application -> *still* waiting_for_input

`record_answer` wrote to the legacy flywheel (`runtime.memory`), while
`fill_application_form` resolves questions from the unified `AnswerStore`. Two
stores, one of them invisible to the thing that fills forms, and the public tool
wrote to the invisible one.

The E2E below drives **only public MCP tools** -- enqueue, prepare, the answer
tool, prepare again, submit -- with no direct store writes, no database
manipulation and no forged grant. The human approval goes through the same
authorizer the CLI uses.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from applyops.answers import AnswerScope
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.memory import MemoryStore
from applyops.state_machine import ApplicationState

QUESTION = "Notice period"
ANSWER = "Two weeks"

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}


class FakeServer:
    """Captures the registered tools so the test can call them directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *_args, **_kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-mcp-answers-"))


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


@pytest.mark.asyncio
async def test_the_mcp_answer_tool_feeds_the_filler_end_to_end():
    """The reported loop, closed: answer once, prepare again, submit."""
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)

    from applyops.browser import BrowserController

    with DemoATS() as ats:
        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        runtime._browser = browser  # the harness owns the browser
        try:
            job_url = f"{ats.url}/form"

            enqueued = json.loads(
                await tools["enqueue_application"](job_url=job_url, job_id="job-answers")
            )
            application_id = enqueued["application"]["id"]

            # 1. Prepare: the demo form asks for a notice period, which nothing
            #    knows yet, so the application parks and names the question.
            first = json.loads(await tools["prepare_application"](application_id=application_id))
            assert first["state"] == ApplicationState.WAITING_FOR_INPUT.value, first
            assert any(QUESTION.lower() in m.lower() for m in first["missing"]), first

            # 2. The user answers, through the public tool, for this application.
            stored = json.loads(
                await tools["record_answer"](
                    question=QUESTION,
                    answer=ANSWER,
                    context="demo form",
                    scope="application",
                    application_id=application_id,
                )
            )
            assert stored["stored"] is True, stored
            assert stored["scope"] == AnswerScope.APPLICATION.value, stored

            # 3. Prepare again: the answer must now be visible to the filler.
            second = json.loads(
                await tools["prepare_application"](application_id=application_id)
            )
            assert second["state"] == ApplicationState.WAITING_FOR_APPROVAL.value, second
            assert second["filled"] >= 5, second

            # 4. The human approves (the CLI's own code path), then submit.
            grant = runtime.authorizer.approve_request(second["request_id"], source="cli_human")
            assert grant is not None

            submitted = json.loads(
                await tools["submit_final"](
                    grant_id=grant.grant_id,
                    job_url=job_url,
                    job_id="job-answers",
                    application_id=application_id,
                )
            )
            assert submitted.get("submitted") is True, submitted
            assert submitted["status"] == "verified", submitted

            status = json.loads(
                await tools["application_status"](application_id=application_id)
            )
            assert status["application"]["state"] == ApplicationState.SUBMITTED_VERIFIED.value

            # 5. The employer received the real values, answered question included.
            received = ats.last_submission
            assert received["fields"]["name"] == "Jane Doe"
            assert received["fields"]["notice_period"] == "two_weeks"
            assert received["files"]["resume"] == "resume.pdf"
        finally:
            runtime._browser = None
            await browser.close()


@pytest.mark.asyncio
async def test_get_answer_reports_what_the_filler_will_read():
    """An agent that records an answer must be able to verify it took effect."""
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)

    stored = json.loads(
        await tools["record_answer"](
            question=QUESTION, answer=ANSWER, context="check", scope="global"
        )
    )
    assert stored["stored"] is True
    # The same store the filler reads, so "saved" and "usable" cannot disagree.
    assert runtime.service.answers.resolve(QUESTION) is not None

    looked_up = json.loads(await tools["get_answer"](question=QUESTION))
    assert looked_up["status"] == "answered", looked_up
    assert looked_up["answer"] == ANSWER, looked_up
    assert looked_up.get("source", "").startswith("answers"), looked_up


@pytest.mark.asyncio
async def test_application_scoped_answers_do_not_leak_to_other_applications():
    """The reason application scope is the recommended default."""
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)

    first = json.loads(
        await tools["enqueue_application"](
            job_url="https://example.test/a", job_id="job-a"
        )
    )["application"]["id"]
    second = json.loads(
        await tools["enqueue_application"](
            job_url="https://example.test/b", job_id="job-b"
        )
    )["application"]["id"]

    await tools["record_answer"](
        question="Why do you want to work here?",
        answer="Because of the compiler team.",
        scope="application",
        application_id=first,
    )

    # The answer belongs to the first application, and to it alone.
    assert runtime.service.answers.resolve("Why do you want to work here?", application_id=first)
    assert (
        runtime.service.answers.resolve("Why do you want to work here?", application_id=second)
        is None
    )


@pytest.mark.asyncio
async def test_a_global_answer_is_visible_everywhere_and_company_scope_needs_a_company():
    root = _tmp()
    runtime = _runtime(root)
    tools = _tools(runtime)

    await tools["record_answer"](question="Are you 18 or older?", answer="Yes")

    await tools["enqueue_application"](job_url="https://example.test/c", job_id="job-c")
    row = runtime.service.ledger.find_by_job_key("job-c")
    assert runtime.service.answers.resolve("Are you 18 or older?", application_id=row.id)

    # A company-scoped answer without a company is refused, not silently downgraded.
    refused = json.loads(
        await tools["record_answer"](
            question="Notice period", answer="One month", scope="company"
        )
    )
    assert refused["stored"] is False, refused
    assert "company" in refused["error"]
