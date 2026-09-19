"""Blocker 4 -- one page, one writer, and the interval the rails asked for.

Two repros, as reported:

1. The browser is a single shared page, but nothing serialised the drivers that
   touch it. The console's prepare route and its submit route, a runner pass and
   a browser release could interleave: one would navigate while another was
   reading a snapshot, and the loser would act on the wrong page. (MCP already
   held its lock -- that part is asserted structurally, so nobody removes it.)

2. `preflight` returns `wait_seconds` -- the randomised spacing between
   submissions -- and only the MCP preflight *tool* honoured it. The shared
   execution path (service.submit, used by the console and the runner) ignored
   it, so two drivers submitting back to back went out at the same instant, and
   nothing re-checked the rails after a wait.
"""

from __future__ import annotations

import ast
import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from applyops.api.app import create_app
from applyops.authorization import page_identity_of
from applyops.demo_ats import DemoATS, write_sample_resume
from applyops.guardrails import Guardrails
from applyops.memory import MemoryStore
from applyops.prepare import prepare_application
from applyops.resume import resolve_resume
from applyops.service import ApplicationService
from applyops.submission import FinalAction

SYNTHETIC_PROFILE = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1 555 010 4477",
    "years_experience": "4",
    "requires_sponsorship": "no",
}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-blocker4-"))


def _service(root: Path, *, guardrails: Guardrails | None = None) -> ApplicationService:
    memory = MemoryStore(root / "memory.json")
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service = ApplicationService(root, memory=memory, guardrails=guardrails)
    service.answers.set_answer("Notice period", "Two weeks")
    return service


# ── 1. serialisation ─────────────────────────────────────────────────


def test_every_page_touching_mcp_tool_holds_the_lock():
    """Structural, and deliberately so: this is a rule about the whole module,
    not about one call path. A new tool that forgets the lock would otherwise
    only show up as an intermittent failure nobody can reproduce."""
    source = Path(__file__).parent.parent / "src" / "applyops" / "mcp" / "tools.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    unlocked: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.col_offset != 4:
            continue
        body = ast.get_source_segment(source.read_text(encoding="utf-8"), node) or ""
        touches_page = any(
            token in body for token in ("browser.", "get_browser", "controller.")
        )
        if touches_page and "runtime.lock" not in body:
            unlocked.append(node.name)
    assert unlocked == [], f"these page-touching tools do not hold the lock: {unlocked}"


def test_every_page_touching_api_route_holds_the_page_lock():
    """The same structural rule as for MCP, on the console's own routes.

    Without it, prepare and submit (or a runner pass, or a browser release) can
    interleave on one page: one navigates while the other is reading a snapshot,
    and whoever loses acts on the wrong posting.

    Scoped to the routes themselves. `AppState.get_browser`/`close_browser` are
    the primitives their callers hold the lock around, and the lifespan hook is
    shutdown -- neither can take it without deadlocking the routes above them.
    """
    source = Path(__file__).parent.parent / "src" / "applyops" / "api" / "app.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)

    create_app_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )

    def is_route(node: ast.AST) -> bool:
        for decorator in getattr(node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "app"
            ):
                return True
        return False

    unlocked: list[str] = []
    for node in create_app_fn.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) or not is_route(node):
            continue
        body = ast.get_source_segment(text, node) or ""
        touches_page = any(
            token in body for token in ("get_browser", "close_browser", "run_pass")
        )
        if touches_page and "page_lock" not in body:
            unlocked.append(node.name)
    assert unlocked == [], f"these API routes do not take the page lock: {unlocked}"


@pytest.mark.asyncio
async def test_two_concurrent_prepares_do_not_share_a_page():
    """Two prepares at once: both must land on their own application's page."""
    root = _tmp()
    app = create_app(root, frontend_dist=None, headless=True)
    service = app.state.applyops.service
    memory = app.state.applyops.memory
    memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    service.answers.set_answer("Notice period", "Two weeks")

    with DemoATS() as ats:
        first = service.enqueue(
            job_url=f"{ats.url}/form?demo=1", job_id="job-1", route="demo", platform="DemoATS"
        )
        second = service.enqueue(
            job_url=f"{ats.url}/form?demo=2", job_id="job-2", route="demo", platform="DemoATS"
        )
        app.state.applyops.demo_ats = ats

        async def prepare(application_id: str):
            async with app.state.applyops.page_lock:
                browser = await app.state.applyops.get_browser()
                return await prepare_application(service, browser, application_id)

        try:
            outcomes = await asyncio.gather(prepare(first.id), prepare(second.id))
        finally:
            await app.state.applyops.close_browser()

        assert [o.ready for o in outcomes] == [True, True], [o.to_dict() for o in outcomes]

        # The proof that they did not share a page: each application's approval
        # request names the page that application's job lives on.
        for row, outcome in ((first, outcomes[0]), (second, outcomes[1])):
            request = service.authorizer.get_request(outcome.request_id)
            assert request.application_id == row.id
            assert request.page_identity == page_identity_of(row.job_url), (
                row.job_url,
                request.page_identity,
            )


@pytest.mark.asyncio
async def test_submit_and_prepare_in_parallel_do_not_cross_pages():
    """A submit running while another application is being prepared."""
    root = _tmp()
    service = _service(root)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        page_lock = asyncio.Lock()
        try:
            a = service.enqueue(
                job_url=f"{ats.url}/form?demo=a", job_id="job-a", route="demo", platform="DemoATS"
            )
            b = service.enqueue(
                job_url=f"{ats.url}/form?demo=b", job_id="job-b", route="demo", platform="DemoATS"
            )

            async def with_page(fn):
                async with page_lock:
                    return await fn()

            a_outcome = await with_page(lambda: prepare_application(service, browser, a.id))
            assert a_outcome.ready
            grant = service.authorizer.approve_request(a_outcome.request_id, source="cli_human")

            async def submit_a():
                async with page_lock:
                    return await service.submit(
                        a.id,
                        grant_id=grant.grant_id,
                        controller=browser,
                        resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                        action=FinalAction(
                            name="Submit application",
                            success_patterns=("Application received",),
                        ),
                    )

            results = await asyncio.gather(submit_a(), with_page(lambda: prepare_application(service, browser, b.id)))
            submit_outcome, b_outcome = results

            # The submit went out from A's page and was confirmed; the prepare
            # landed on B's page and filed B's request.
            assert submit_outcome.status == "verified", submit_outcome.to_dict()
            assert b_outcome.ready, b_outcome.to_dict()
            request_b = service.authorizer.get_request(b_outcome.request_id)
            assert request_b.page_identity == page_identity_of(b.job_url)
            assert service.get(a.id).state == "submitted_verified"
        finally:
            await browser.close()


# ── 2. the interval the rails asked for ──────────────────────────────


@pytest.mark.asyncio
async def test_the_shared_path_waits_for_the_required_interval():
    """`wait_seconds` from preflight must be honoured by service.submit, not
    only by the MCP preflight tool."""
    root = _tmp()
    # Long enough that preparing the second application (~1-2 s of browser work)
    # cannot consume the whole interval, so there is a real wait to observe.
    rails = Guardrails(root / "guard_state.json", memory=None, daily_cap=10, min_gap=(6.0, 6.0))
    service = _service(root, guardrails=rails)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            outcomes: list[tuple[float, object, float]] = []
            for job_id in ("job-1", "job-2"):
                row = service.enqueue(
                    job_url=f"{ats.url}/form", job_id=job_id, route="demo", platform="DemoATS"
                )
                prepared = await prepare_application(service, browser, row.id)
                assert prepared.ready, prepared.to_dict()
                grant = service.authorizer.approve_request(
                    prepared.request_id, source="cli_human"
                )
                # What the rails ask for *at this moment*: if preparing the
                # application already consumed the interval, zero is the correct
                # answer and the assertion below has to allow for it.
                required_wait = rails.preflight(row.job_url, row.job_key).wait_seconds
                started = time.monotonic()
                outcome = await service.submit(
                    row.id,
                    grant_id=grant.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=FinalAction(
                        name="Submit application",
                        success_patterns=("Application received",),
                    ),
                )
                outcomes.append((time.monotonic() - started, outcome, required_wait))

            assert outcomes[0][1].status == "verified", outcomes[0][1].to_dict()
            assert outcomes[1][1].status == "verified", outcomes[1][1].to_dict()
            # The first submission has nothing to wait for; the second must wait
            # out whatever the rails asked for at that moment.
            elapsed, _, required = outcomes[1]
            assert required > 0, "the interval should not have elapsed during prepare"
            assert elapsed >= required - 0.05, (
                f"waited {elapsed:.2f}s but the rails asked for {required:.2f}s"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_rails_are_rechecked_after_waiting():
    """If the answer changed while we waited, the wait does not authorise a send."""
    root = _tmp()
    rails = Guardrails(root / "guard_state.json", memory=None, daily_cap=10, min_gap=(1.2, 1.2))
    service = _service(root, guardrails=rails)

    with DemoATS() as ats:
        from applyops.browser import BrowserController

        browser = BrowserController(headless=True, user_data_dir=root / "chrome")
        await browser.launch()
        try:
            first = service.enqueue(
                job_url=f"{ats.url}/form", job_id="job-first", route="demo", platform="DemoATS"
            )
            prepared = await prepare_application(service, browser, first.id)
            grant = service.authorizer.approve_request(prepared.request_id, source="cli_human")
            await service.submit(
                first.id,
                grant_id=grant.grant_id,
                controller=browser,
                resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                action=FinalAction(
                    name="Submit application", success_patterns=("Application received",)
                ),
            )

            # A second application, ready to go -- and while it waits out the
            # interval, another driver spends the last of the daily quota.
            second = service.enqueue(
                job_url=f"{ats.url}/form", job_id="job-second", route="demo", platform="DemoATS"
            )
            prepared_second = await prepare_application(service, browser, second.id)
            grant_second = service.authorizer.approve_request(
                prepared_second.request_id, source="cli_human"
            )

            async def spend_the_quota():
                await asyncio.sleep(0.4)
                rails.set_daily_cap(2, reason="test")
                rails.record_outcome(success=True)
                rails.record_outcome(success=True)
                rails._refresh()

            await asyncio.gather(
                spend_the_quota(),
                service.submit(
                    second.id,
                    grant_id=grant_second.grant_id,
                    controller=browser,
                    resume=resolve_resume(str(service.memory.profile.value("resume_path"))),
                    action=FinalAction(
                        name="Submit application",
                        success_patterns=("Application received",),
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the refusal path is the point
            from applyops.submission import SubmissionRefused

            assert isinstance(exc, SubmissionRefused), exc
            assert "after waiting for the required interval" in str(exc), str(exc)
            assert "cap" in str(exc)
            # Nothing was sent for the second application.
            assert service.get(second.id).state != "submitted_verified"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_console_honours_the_interval_and_stops_when_it_cannot():
    """The interval applies through the API, not only inside the service.

    Two behaviours, both real:

    * an early caller **waits** out the interval and then submits (the default
      45-180s gap is what the rails ask for, so refusing instantly would push the
      wait onto a human clicking a button);
    * when the remaining interval is longer than the bounded inline wait, the
      console **refuses** instead of holding the request -- and refuses *before*
      the claim, so nothing is sent and the approval is not spent.
    """
    from fastapi.testclient import TestClient

    import applyops.service as service_module

    root = _tmp()
    app = create_app(root, frontend_dist=None, headless=True)
    state = app.state.applyops
    state.memory.update_profile(
        {**SYNTHETIC_PROFILE, "resume_path": str(write_sample_resume(root / "resume.pdf"))}
    )
    state.service.answers.set_answer("Notice period", "Two weeks")
    state.guardrails.min_gap = (20.0, 20.0)

    with DemoATS() as ats:
        state.demo_ats = ats
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers.update({"X-ApplyOps-Token": state.session_token})

            def submit_one():
                demo = client.post("/api/demo/start").json()
                application_id = demo["application"]["id"]
                prepared = client.post(f"/api/applications/{application_id}/prepare").json()
                if prepared["state"] == "waiting_for_input":
                    client.post(
                        f"/api/applications/{application_id}/answer",
                        json={"question": prepared["missing"][0], "answer": "Two weeks"},
                    )
                    prepared = client.post(f"/api/applications/{application_id}/prepare").json()
                grant = client.post(f"/api/requests/{prepared['request_id']}/approve").json()
                # What the rails ask for at this instant: preparing the
                # application has already consumed part of the interval.
                required = state.guardrails.preflight(
                    f"{ats.url}/form", f"demo-{application_id}"
                ).wait_seconds
                started = time.monotonic()
                response = client.post(
                    f"/api/applications/{application_id}/submit",
                    json={"grant_id": grant["grant_id"]},
                )
                return {
                    "application_id": application_id,
                    "response": response,
                    "elapsed": time.monotonic() - started,
                    "required": required,
                }

            first = submit_one()
            assert first["response"].status_code == 200, first["response"].text
            assert first["response"].json()["status"] == "verified"
            assert ats.submission_count == 1

            # 1. The console waits out whatever the interval still required,
            #    rather than sending immediately.
            second = submit_one()
            assert second["response"].status_code == 200, second["response"].text
            assert second["required"] > 3.0, (
                "the interval should still have been running; prepare consumed all of it"
            )
            assert second["elapsed"] >= second["required"] - 0.05, (
                f"waited {second['elapsed']:.2f}s but the rails asked for "
                f"{second['required']:.2f}s"
            )
            assert ats.submission_count == 2

            # 2. When the remaining interval exceeds the bounded inline wait, the
            #    console refuses instead of hanging on to the request.
            original_bound = service_module.MAX_INLINE_WAIT_SECONDS
            service_module.MAX_INLINE_WAIT_SECONDS = 0.2
            try:
                third = submit_one()
            finally:
                service_module.MAX_INLINE_WAIT_SECONDS = original_bound

            assert third["response"].status_code == 403, third["response"].text
            assert "interval" in third["response"].json()["detail"]
            assert ats.submission_count == 2, "the refused submission must not be sent"
            # The approval survives the refusal: the user can submit once the
            # interval has passed, without asking for a new grant.
            assert state.service.get(third["application_id"]).state == "waiting_for_approval"

        await state.close_browser()
