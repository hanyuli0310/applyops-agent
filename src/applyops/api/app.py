"""The local ApplyOps API.

One backend over the unified core, for the local UI. Nothing here re-implements
application semantics: every route is a thin adapter over `ApplicationService`,
`MemoryStore`, `Guardrails` and the M1 submission path. If a route needs logic
that does not exist in the core, the answer is to add it to the core -- not to
invent a second, weaker version here and let the UI drift.

Two deliberate choices:

- **The API process owns the browser.** One owner per machine is the whole
  concurrency model; the UI is a client of this process, never a second driver.
- **The approval endpoint lives here, on loopback.** This is the human channel
  `PLAN.md` §5.3 asks for: the requesting process (agent, runner) cannot mint a
  grant, but a person clicking Approve in their own browser can. Binding to
  `127.0.0.1` is what makes that boundary meaningful; `serve()` refuses any
  other host.
"""

from __future__ import annotations

import asyncio
import platform as _platform
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class EnqueueBody(BaseModel):
    job_url: str
    job_id: str = ""
    title: str = ""
    company: str = ""


class SubmitBody(BaseModel):
    grant_id: str
from ..authorization import SubmissionAuthorizer
from ..browser import BrowserController
from ..demo_ats import DemoATS
from ..evidence import detect_final_action, success_patterns_for
from ..guardrails import Guardrails
from ..ledger import Ledger
from ..memory import MemoryStore
from ..resume import ResumeError, resolve_resume
from ..service import ApplicationService
from ..state_machine import ApplicationState, InvalidTransition
from ..submission import FinalAction, SubmissionRefused

APP_VERSION = "0.2.0-m3"


class AppState:
    """Everything the routes need, owned by this one process.

    `headless=False` for real use: a person watches their own applications.
    Tests pass True so a run never opens a window on the developer's machine.
    """

    def __init__(self, data_dir: str | Path, headless: bool = False):
        self.headless = headless
        self.data_dir = Path(data_dir)
        self.memory = MemoryStore(self.data_dir / "memory.json")
        self.guardrails = Guardrails(self.data_dir / "guard_state.json", memory=self.memory)
        self.authorizer = SubmissionAuthorizer(self.data_dir)
        self.service = ApplicationService(
            self.data_dir,
            memory=self.memory,
            authorizer=self.authorizer,
            ledger=Ledger(self.data_dir / "app.sqlite"),
        )
        self._browser: BrowserController | None = None
        self._browser_lock = asyncio.Lock()
        self._profile_lock = threading.Lock()
        self.demo_ats: DemoATS | None = None

    @property
    def browser(self) -> BrowserController | None:
        return self._browser

    async def get_browser(self) -> BrowserController:
        if self._browser is None or not self._browser.launched:
            # The browser profile lives under THIS data dir. Defaulting to the
            # repo's data/ would make a test -- or a second install -- drive the
            # user's real logged-in profile, which is the one thing that must
            # never happen implicitly.
            self._browser = BrowserController(
                headless=self.headless,
                user_data_dir=self.data_dir / "browser-profile",
            )
            await self._browser.launch()
        return self._browser

    async def close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001, S110 - a dead browser is already dead
                pass
            self._browser = None

    def start_demo_ats(self) -> str:
        if self.demo_ats is None:
            self.demo_ats = DemoATS()
            self.demo_ats.start()
        return self.demo_ats.url


def create_app(
    data_dir: str | Path,
    frontend_dist: str | Path | None = None,
    *,
    headless: bool = False,
) -> FastAPI:
    state = AppState(data_dir, headless=headless)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await state.close_browser()
        if state.demo_ats is not None:
            state.demo_ats.stop()

    app = FastAPI(title="ApplyOps", version=APP_VERSION, lifespan=lifespan)
    app.state.applyops = state  # reachable for the CLI's demo bootstrap

    # ── status / diagnostics ─────────────────────────────────────────

    @app.get("/api/status")
    async def status() -> dict:
        profile = state.memory.profile
        counts: dict[str, int] = {}
        for row in state.service.list():
            counts[row.state] = counts.get(row.state, 0) + 1
        return {
            "version": APP_VERSION,
            "python": sys.version.split()[0],
            "os": f"{_platform.system()} {_platform.release()}",
            "profile_ready": profile.is_ready(),
            "missing_required": profile.missing_required(),
            "browser_open": state.browser is not None and state.browser.launched,
            "applications_by_state": counts,
            "pending_approvals": len(state.authorizer.pending_requests()),
            "guardrails": state.guardrails.stats(),
        }

    # ── profile & resumes ────────────────────────────────────────────

    @app.get("/api/profile")
    async def get_profile() -> dict:
        return {
            "values": state.memory.get_profile(),
            "missing_required": state.memory.profile.missing_required(),
            "ready": state.memory.profile.is_ready(),
            "path": str(state.memory.profile.path),
        }

    @app.post("/api/profile")
    async def save_profile(fields: dict[str, str]) -> dict:
        report = state.memory.update_profile(fields)
        problems = report.get("warnings") or report.get("unknown") or report.get("unknown_keys")
        if problems:
            # Rejected input is surfaced, never silently dropped or stored.
            raise HTTPException(422, detail=report)
        return {"saved": True, "missing_required": state.memory.profile.missing_required()}

    @app.get("/api/resumes")
    async def list_resumes() -> dict:
        configured = state.memory.profile.value("resume_path")
        try:
            ref = resolve_resume(configured)
            return {"resumes": [ref.to_dict()], "configured": True}
        except ResumeError as exc:
            return {"resumes": [], "configured": False, "detail": str(exc)}

    @app.post("/api/resumes")
    async def upload_resume(file: UploadFile = File(...)) -> dict:  # noqa: B008 - FastAPI idiom
        """Store a resume under `data/resumes/` and make it THE configured one.

        There is one resume on purpose (M1, D8): uploading replaces the single
        source of truth rather than accumulating candidates, so "which file went
        out" is always answerable.
        """
        target_dir = state.data_dir / "resumes"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".doc", ".docx"}:
            raise HTTPException(415, detail=f"unsupported type {suffix!r}; use pdf/doc/docx")
        target = target_dir / Path(file.filename or "resume").name
        target.write_bytes(await file.read())
        report = state.memory.update_profile({"resume_path": str(target)})
        if report.get("warnings"):
            raise HTTPException(422, detail=report)
        ref = resolve_resume(str(target))
        return {"saved": True, "resume": ref.to_dict(), "describe": ref.describe()}

    # ── jobs / queue ─────────────────────────────────────────────────

    @app.get("/api/applications")
    async def list_applications(state_filter: str = "") -> dict:
        rows = state.service.list(state_filter or None)
        return {"count": len(rows), "applications": [r.to_dict() for r in rows]}

    @app.post("/api/applications")
    async def enqueue(body: EnqueueBody) -> dict:
        if not body.job_url.strip():
            raise HTTPException(422, "job_url is required")
        row = state.service.enqueue(
            job_url=body.job_url.strip(),
            job_id=body.job_id.strip(),
            title=body.title,
            company=body.company,
            route="demo" if "127.0.0.1" in body.job_url or "localhost" in body.job_url else "",
        )
        return {"enqueued": True, "application": row.to_dict()}

    @app.get("/api/applications/{application_id}")
    async def application_detail(application_id: str) -> dict:
        try:
            return state.service.status(application_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/applications/{application_id}/cancel")
    async def cancel(application_id: str) -> dict:
        try:
            row = state.service.cancel(application_id, reason="cancelled from UI")
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"cancelled": True, "state": row.state}

    # ── the run flow: prepare -> approve -> submit ───────────────────

    @app.post("/api/applications/{application_id}/prepare")
    async def prepare(application_id: str) -> dict:
        """Open the posting, read the form, and file the approval request.

        This is the step that turns a queue row into something a human can
        decide on: the summary they will see is read off the live page, not
        written by whatever component is asking.
        """
        row = state.service.get(application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        if row.state == ApplicationState.SUBMITTED_UNVERIFIED.value:
            raise HTTPException(409, "this application was possibly submitted; reconcile instead")

        demo_url = state.start_demo_ats()
        url = row.job_url or f"{demo_url}/form"
        browser = await state.get_browser()
        await browser.goto(url, settle=1.0)

        snapshot = await browser.field_snapshot()
        try:
            resume = resolve_resume(state.memory.profile.value("resume_path"))
        except ResumeError as exc:
            row = state.service.prepare(application_id, ready=False, detail=str(exc))
            return {"state": row.state, "blocked": "resume", "detail": str(exc)}

        missing = [k for k, v in snapshot.items() if v in {"<unreadable>", "<unresolvable>"}]
        request = state.authorizer.create_request(
            job_key=row.job_key,
            job_url=row.job_url or url,
            route=row.route or "demo",
            platform=row.platform or "DemoATS",
            fields=snapshot,
            resume_filename=resume.filename,
            resume_sha256=resume.sha256,
            requested_by="local_ui",
        )
        state.service.prepare(
            application_id,
            ready=True,
            detail=f"request {request.request_id} filed for approval",
        )
        return {
            "state": "waiting_for_approval",
            "request_id": request.request_id,
            "unreadable_fields": missing,
        }

    @app.get("/api/requests")
    async def pending_requests() -> dict:
        requests = state.authorizer.pending_requests()
        return {
            "count": len(requests),
            "requests": [
                {
                    "request_id": r.request_id,
                    "job_key": r.job_key,
                    "job_url": r.job_url,
                    "created_at": r.created_at,
                    "summary": r.summary_for_human(),
                }
                for r in requests
            ],
        }

    @app.post("/api/requests/{request_id}/approve")
    async def approve(request_id: str) -> dict:
        """The human channel. Mints the one-time grant for `submit`."""
        grant = state.authorizer.approve_request(request_id, source="local_ui_human")
        if grant is None:
            raise HTTPException(409, "request is not pending (expired, used or already decided)")
        return {"approved": True, "grant_id": grant.grant_id}

    @app.post("/api/requests/{request_id}/reject")
    async def reject(request_id: str) -> dict:
        if not state.authorizer.reject_request(request_id):
            raise HTTPException(409, "request is not pending")
        return {"rejected": True}

    @app.post("/api/applications/{application_id}/submit")
    async def submit(application_id: str, body: SubmitBody) -> dict:
        """The final submit. Same path, same boundary, as every other driver."""
        row = state.service.get(application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        try:
            resume = resolve_resume(state.memory.profile.value("resume_path"))
        except ResumeError as exc:
            raise HTTPException(409, f"resume problem: {exc}") from exc
        browser = await state.get_browser()
        action, detail = await detect_final_action(browser)
        if action is None:
            raise HTTPException(409, detail)
        try:
            outcome = await state.service.submit(
                application_id,
                grant_id=body.grant_id,
                controller=browser,
                resume=resume,
                action=action,
            )
        except SubmissionRefused as exc:
            raise HTTPException(403, exc.reason) from exc
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return outcome.to_dict()

    @app.post("/api/applications/{application_id}/reconcile")
    async def reconcile(application_id: str) -> dict:
        row = state.service.get(application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        browser = await state.get_browser()
        outcome = await state.service.reconcile(
            application_id,
            controller=browser,
            action=FinalAction(success_patterns=success_patterns_for(browser.page.url)),
        )
        return outcome.to_dict()

    # ── demo ─────────────────────────────────────────────────────────

    @app.post("/api/demo/start")
    async def start_demo() -> dict:
        """One click: a local demo job in the queue. Nothing leaves the machine."""
        demo_url = state.start_demo_ats()
        row = state.service.enqueue(
            job_url=f"{demo_url}/form",
            job_id=f"demo-{demo_url.rsplit(':', 1)[-1]}",
            route="demo",
            platform="DemoATS",
            title="Backend Engineer (demo)",
            company="ApplyOps Demo Co",
        )
        return {"application": row.to_dict(), "demo_url": demo_url}

    # ── frontend ─────────────────────────────────────────────────────

    if frontend_dist and Path(frontend_dist).is_dir():
        dist = Path(frontend_dist)

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    return app


def serve(
    data_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8620,
    frontend_dist: str | Path | None = None,
) -> None:
    """Run the local UI. Loopback only -- see the module docstring for why."""
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit(
            "refusing to bind outside loopback: the approval endpoint is only "
            "meaningful on a machine a single person controls."
        )
    import uvicorn

    uvicorn.run(
        create_app(data_dir, frontend_dist),
        host=host,
        port=port,
        log_level="info",
    )
