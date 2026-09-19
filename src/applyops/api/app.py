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
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..answers import AnswerScope, AnswerStore
from ..authorization import SubmissionAuthorizer
from ..browser import BrowserController
from ..company_policy import CompanyPolicy, CompanyPolicyStore, normalize_company
from ..concurrency import FileLock, browser_lock_path, describe_holder
from ..demo_ats import DemoATS
from ..evidence import detect_final_action, success_patterns_for
from ..guardrails import Guardrails
from ..ledger import Ledger
from ..memory import MemoryStore
from ..platforms.naming import DEMO_ROUTE, resolve_route
from ..preferences import JobPreferences, PreferenceStore
from ..preferences import evaluate as evaluate_job
from ..prepare import PrepareRefused
from ..prepare import prepare_application as prepare_application_impl
from ..presentation import application_view
from ..resume import ResumeError, resolve_resume
from ..runner import AutoPolicy, PassBudgetRequired, QueueRunner
from ..service import ApplicationService
from ..state_machine import ApplicationState, InvalidTransition
from ..submission import FinalAction, SubmissionRefused


def _latest_missing(service, application_id: str) -> list[str]:
    """The fields the last prepare could not fill, read from the event log."""
    for event in reversed(service.ledger.events(application_id)):
        if event["kind"] != "transition":
            continue
        detail = str(event["payload"].get("detail", ""))
        if "needs input before it can be submitted:" in detail:
            return [f.strip() for f in detail.split(":", 1)[1].split(",") if f.strip()]
    return []


class EnqueueBody(BaseModel):
    job_url: str
    job_id: str = ""
    title: str = ""
    company: str = ""


class SubmitBody(BaseModel):
    grant_id: str


class AnswerBody(BaseModel):
    question: str
    answer: str
    scope: str = "global"
    company: str = ""
    application_id: str = ""


class PreferencesBody(BaseModel):
    target_titles: list[str] = []
    locations: list[str] = []
    include_keywords: list[str] = []
    exclude_keywords: list[str] = []
    exclude_companies: list[str] = []


class CompanyPolicyBody(BaseModel):
    default_policy: str = "auto"
    review_companies: list[str] = []
    never_companies: list[str] = []


class PreviewBody(BaseModel):
    title: str
    company: str = ""
    location: str = ""


class PolicyBody(BaseModel):
    enabled: bool = False
    max_applications: int = 0
    allowed_platforms: list[str] = []
    ttl_minutes: int = 60


class PassBody(BaseModel):
    """How many this pass may send.

    Required, and deliberately so: a pass that silently reused the number chosen
    last time would let a decision days old govern tonight's run.
    """

    budget: int = Field(gt=0)


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
        self.answers = AnswerStore(self.data_dir)
        self.preferences = PreferenceStore(self.data_dir)
        self.company_policy = CompanyPolicyStore(self.data_dir)
        self.company_policy.ensure_defaults()
        self.service = ApplicationService(
            self.data_dir,
            memory=self.memory,
            authorizer=self.authorizer,
            ledger=Ledger(self.data_dir / "app.sqlite"),
            answers=self.answers,
            guardrails=self.guardrails,
        )
        self.runner = QueueRunner(
            self.service,
            memory=self.memory,
            answers=self.answers,
            company_policy_store=self.company_policy,
        )
        # Normal console use starts in the requested default AUTO mode.  The
        # existing per-pass budget, guardrails and submission grant checks still
        # bound every run; this only avoids requiring a second global toggle for
        # ordinary companies.
        if not self.runner.policy_store.path.exists():
            self.runner.policy_store.set(
                AutoPolicy(
                    enabled=True,
                    max_applications=20,
                    allowed_platforms=(),
                    expires_at_epoch=0.0,
                    updated_by="default_company_policy",
                )
            )
        self._browser: BrowserController | None = None
        # One page, one writer *within* this process: prepare, submit, runner
        # passes and browser release all take this before touching the page.
        # The profile lock below is the cross-process half of the same rule.
        self.page_lock = asyncio.Lock()

        # The UI is a browser owner like any other, so it takes the same
        # cross-process profile lock the MCP server and the runner take. Two
        # Chromes on one profile rewrite each other's cookies, and the logged-in
        # session is the one thing this project cannot rebuild.
        self.profile_lock = FileLock(
            browser_lock_path(self.data_dir), purpose="local ui browser", block=False
        )
        # A per-process secret that proves a request came from the page we served
        # rather than from another origin. Injected into index.html at serve time.
        self.session_token = secrets.token_urlsafe(32)
        self.demo_ats: DemoATS | None = None
        self.demo_counter = 0

    def free_browser_claim(self) -> None:
        """Hand the profile back, so a runner or an agent can take it."""
        self.profile_lock.release()

    @property
    def profile_holder(self) -> str:
        return describe_holder(browser_lock_path(self.data_dir))

    @property
    def browser(self) -> BrowserController | None:
        return self._browser

    async def get_browser(self) -> BrowserController:
        if self._browser is not None and self._browser.launched:
            return self._browser
        if not self.profile_lock.held and not self.profile_lock.acquire():
                raise HTTPException(
                    409,
                    detail=(
                        "another driver holds the browser profile "
                        f"({self.profile_holder}). Two Chromes on one profile "
                        "rewrite each other's cookies, so this refuses instead."
                    ),
                )
        # The browser profile lives under THIS data dir. Defaulting to the
        # repo's data/ would make a test -- or a second install -- drive the
        # user's real logged-in profile, which is the one thing that must never
        # happen implicitly.
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
        self.profile_lock.release()

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

    def _application_view(row) -> dict:
        return application_view(
            row,
            missing=_latest_missing(state.service, row.id),
            company_policy=state.company_policy.get().decision(row.company).value,
        )

    LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def _host_of(value: str) -> str:
        """Hostname from a Host header or an Origin URL, without the port."""
        raw = (value or "").strip()
        if "//" in raw:
            from urllib.parse import urlparse

            return (urlparse(raw).hostname or "").lower()
        return raw.split(":")[0].lower()

    @app.middleware("http")
    async def protect_local_api(request, call_next):
        """Loopback-only, same-origin, token-bearing state changes.

        A local server is reachable by every process on the machine -- and by any
        web page the user happens to have open, via a form post or a fetch with a
        permissive CORS response. Without these three checks the approval endpoint
        would be a CSRF target: a random page could approve the user's pending
        submission and the only thing that happened "here" is a click they did not
        make.
        """
        host = _host_of(request.headers.get("host", ""))
        if host and host not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "this API only answers on loopback"}, 403)

        origin = request.headers.get("origin")
        if origin and _host_of(origin) not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "cross-origin requests are refused"}, 403)

        if request.method not in SAFE_METHODS:
            token = request.headers.get("x-applyops-token", "")
            if not secrets.compare_digest(token, state.session_token):
                return JSONResponse(
                    {
                        "detail": (
                            "missing or invalid session token; open the console "
                            "through `applyops serve` so the page carries one"
                        )
                    },
                    403,
                )
        return await call_next(request)

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
        return {
            "count": len(rows),
            "applications": [{**r.to_dict(), **_application_view(r)} for r in rows],
        }

    @app.post("/api/applications")
    async def enqueue(body: EnqueueBody) -> dict:
        if not body.job_url.strip():
            raise HTTPException(422, "job_url is required")
        row = state.service.enqueue(
            job_url=body.job_url.strip(),
            job_id=body.job_id.strip(),
            title=body.title,
            company=body.company,
            # One route vocabulary, resolved in one place (see platforms.naming).
            route=resolve_route(body.job_url.strip()),
        )
        return {"enqueued": True, "application": row.to_dict()}

    @app.get("/api/applications/{application_id}")
    async def application_detail(application_id: str) -> dict:
        try:
            detail = state.service.status(application_id)
            row = state.service.get(application_id)
            assert row is not None
            detail["view"] = _application_view(row)
            return detail
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
        """Open the posting, fill it, and file the approval request.

        The work lives in `applyops.prepare` so that MCP, the console and the
        runner cannot drift: this route only supplies the browser and translates
        the outcome.
        """
        if state.service.get(application_id) is None:
            raise HTTPException(404, "unknown application")

        row = state.service.get(application_id)
        if "127.0.0.1" in row.job_url or "localhost" in row.job_url:
            state.start_demo_ats()

        # One page, one writer: the browser lock serialises every driver.
        async with state.page_lock:
            browser = await state.get_browser()
            try:
                outcome = await prepare_application_impl(
                    state.service, browser, application_id
                )
            except PrepareRefused as exc:
                raise HTTPException(409, str(exc)) from exc
        return outcome.to_dict()

    @app.get("/api/requests")
    async def pending_requests() -> dict:
        requests = state.authorizer.pending_requests()

        def request_view(request) -> dict:
            row = state.service.get(request.application_id)
            view = _application_view(row) if row is not None else {}
            return {
                "request_id": request.request_id,
                "job_key": request.job_key,
                "job_url": request.job_url,
                "application_id": request.application_id,
                "page_identity": request.page_identity,
                "created_at": request.created_at,
                "company": view.get("company", ""),
                "reason_code": view.get("reason_code", "approval_required"),
                "reason": view.get("reason_text", "申请已准备好，等待人工确认"),
                "available_actions": view.get("available_actions", ["approve", "skip"]),
                "summary": request.summary_for_human(),
            }

        return {
            "count": len(requests),
            "requests": [request_view(request) for request in requests],
        }

    @app.post("/api/requests/{request_id}/approve")
    async def approve(request_id: str) -> dict:
        """The human channel. Mints the one-time grant for `submit`."""
        grant = state.authorizer.approve_request(request_id, source="local_ui_human")
        if grant is None:
            raise HTTPException(409, "request is not pending (expired, used or already decided)")
        return {
            "approved": True,
            "grant_id": grant.grant_id,
            # The application this grant is *for*. The UI stores the grant under
            # this id, so a grant cannot drift onto another application.
            "application_id": grant.application_id,
            "job_key": grant.job_key,
        }

    @app.post("/api/requests/{request_id}/reject")
    async def reject(request_id: str) -> dict:
        if not state.authorizer.reject_request(request_id):
            raise HTTPException(409, "request is not pending")
        return {"rejected": True}

    @app.post("/api/requests/{request_id}/skip")
    async def skip_request(request_id: str) -> dict:
        request = state.authorizer.get_request(request_id)
        if request is None or not request.live:
            raise HTTPException(409, "request is not pending")
        row = state.service.get(request.application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        if not state.authorizer.reject_request(request_id):
            raise HTTPException(409, "request is not pending")
        try:
            skipped = state.service.skip(
                row.id, reason="skipped by the user from the review queue"
            )
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"skipped": True, "application_id": skipped.id, "state": skipped.state}

    @app.post("/api/requests/{request_id}/allow-company")
    async def allow_company(request_id: str) -> dict:
        request = state.authorizer.get_request(request_id)
        if request is None or not request.live:
            raise HTTPException(409, "request is not pending")
        row = state.service.get(request.application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        policy = state.company_policy.get()
        company_key = normalize_company(row.company)
        policy.review_companies = [
            item for item in policy.review_companies
            if normalize_company(item) != company_key
        ]
        saved = state.company_policy.set(policy)
        return {"saved": True, "company": row.company, "policy": saved.to_dict()}

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
        async with state.page_lock:
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

    # ── scoped answers: answer once, reuse at the right breadth ───────

    @app.get("/api/answers")
    async def list_answers() -> dict:
        entries = state.answers.entries()
        return {
            "count": len(entries),
            "revision": state.answers.revision,
            "answers": [e.to_dict() for e in entries],
        }

    @app.post("/api/answers")
    async def set_answer(body: AnswerBody) -> dict:
        try:
            entry = state.answers.set_answer(
                body.question,
                body.answer,
                scope=AnswerScope(body.scope),
                company=body.company,
                application_id=body.application_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"saved": True, "answer": entry.to_dict(), "revision": state.answers.revision}

    @app.delete("/api/answers/{entry_id}")
    async def withdraw_answer(entry_id: str) -> dict:
        if not state.answers.withdraw(entry_id):
            raise HTTPException(404, "no such active answer")
        return {"withdrawn": True, "revision": state.answers.revision}

    @app.post("/api/applications/{application_id}/answer")
    async def answer_for_application(application_id: str, body: AnswerBody) -> dict:
        """Answer for this application, then try to prepare it again.

        This is the "answer once, resume the application" loop: the answer is
        stored at application scope so it cannot leak to another employer, and
        the next prepare either produces a filled form or names what is still
        missing.
        """
        if state.service.get(application_id) is None:
            raise HTTPException(404, "unknown application")
        try:
            entry = state.answers.set_answer(
                body.question,
                body.answer,
                scope=AnswerScope.APPLICATION,
                application_id=application_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"saved": True, "answer": entry.to_dict()}

    # ── preferences: what is queued, and why ──────────────────────────

    @app.get("/api/preferences")
    async def get_preferences() -> dict:
        return state.preferences.get().to_dict()

    @app.get("/api/company-policy")
    async def get_company_policy() -> dict:
        return state.company_policy.get().to_dict()

    @app.post("/api/company-policy")
    async def save_company_policy(body: CompanyPolicyBody) -> dict:
        policy = CompanyPolicy(
            default_policy=body.default_policy,
            review_companies=body.review_companies,
            never_companies=body.never_companies,
        )
        return state.company_policy.set(policy).to_dict()

    @app.post("/api/preferences")
    async def save_preferences(body: PreferencesBody) -> dict:
        prefs = JobPreferences(
            target_titles=[t for t in body.target_titles if t.strip()],
            locations=[x for x in body.locations if x.strip()],
            include_keywords=[k for k in body.include_keywords if k.strip()],
            exclude_keywords=[k for k in body.exclude_keywords if k.strip()],
            exclude_companies=[c for c in body.exclude_companies if c.strip()],
        )
        return state.preferences.set(prefs).to_dict()

    @app.post("/api/preferences/target-titles/seed")
    async def seed_target_titles() -> dict:
        """Adopt the built-in target-title pool (AGENTS.md §12) as preferences.

        One call, idempotent, and additive: the user's own titles survive. This is
        how the written definition of intent becomes something the queue filter
        actually applies.
        """
        from ..target_titles import all_titles

        saved = state.preferences.seed_target_titles()
        return {
            "seeded": len(saved.target_titles),
            "available": len(all_titles()),
            "target_titles": saved.target_titles,
        }

    @app.post("/api/preferences/preview")
    async def preview_preference(body: PreviewBody) -> dict:
        """Why a posting would be kept or filtered -- the rule, in words."""
        decision = evaluate_job(
            title=body.title,
            company=body.company,
            location=body.location,
            prefs=state.preferences.get(),
        )
        return {"title": body.title, **decision}

    # ── the runner: supervised passes, and the policy that bounds them ──

    @app.get("/api/runner/status")
    async def runner_status() -> dict:
        policy = state.runner.policy_store.get()
        waiting = state.service.list(ApplicationState.WAITING_FOR_INPUT.value)
        company_policy = state.company_policy.get()
        review_waiting = [
            {
                "application_id": row.id,
                "title": row.title,
                "company": row.company,
                "reason": "该公司在人工确认名单中，需要你确认",
            }
            for row in state.service.list(ApplicationState.WAITING_FOR_APPROVAL.value)
            if company_policy.decision(row.company).value == "review"
        ]
        return {
            "paused": state.runner.paused,
            "stopped": state.runner.stopped,
            "policy": policy.to_dict(),
            "policy_usable": policy.usable,
            "company_policy": company_policy.to_dict(),
            "review_waiting": review_waiting,
            "waiting_for_input": [
                {
                    "application_id": row.id,
                    "title": row.title,
                    "missing": _latest_missing(state.service, row.id),
                    **{
                        key: view[key]
                        for key in ("reason_code", "reason_text", "available_actions")
                    },
                }
                for row in waiting
                for view in [_application_view(row)]
            ],
        }

    @app.post("/api/runner/pause")
    async def runner_pause() -> dict:
        state.runner.pause()
        return {"paused": True}

    @app.post("/api/runner/resume")
    async def runner_resume() -> dict:
        state.runner.resume()
        return {"paused": False}

    @app.post("/api/runner/stop")
    async def runner_stop() -> dict:
        """Stop after the current application. Never mid-submission."""
        state.runner.stop()
        return {"stopped": True}

    @app.post("/api/runner/policy")
    async def runner_policy(body: PolicyBody) -> dict:
        policy = AutoPolicy(
            enabled=body.enabled,
            max_applications=body.max_applications,
            allowed_platforms=tuple(body.allowed_platforms),
            expires_at_epoch=time.time() + max(0, body.ttl_minutes) * 60,
            updated_by="local_ui",
        )
        state.runner.policy_store.set(policy)
        return policy.to_dict()

    @app.post("/api/runner/pass")
    async def runner_pass(body: PassBody) -> dict:
        """Run one supervised pass now, bounded by `budget`, and report item by item.

        `budget` is how many submissions *this* pass may spend. It is not stored
        and not reused: every run states its own.
        """
        async with state.page_lock:
            browser = await state.get_browser()
            try:
                report = await state.runner.run_pass(browser, budget=body.budget)
            except PassBudgetRequired as exc:
                raise HTTPException(422, str(exc)) from exc
        return report.to_dict()

    @app.post("/api/browser/release")
    async def browser_release() -> dict:
        """Close the console's browser and hand the profile back.

        Without this the UI would hold the profile for its whole lifetime and
        every agent or scheduled run in the meantime would be refused.
        """
        # Waiting for the page lock is the point: closing the browser out from
        # under a prepare or a submission would abort it mid-flight.
        async with state.page_lock:
            await state.close_browser()
        return {"closed": True}

    @app.post("/api/applications/{application_id}/reconcile")
    async def reconcile(application_id: str) -> dict:
        row = state.service.get(application_id)
        if row is None:
            raise HTTPException(404, "unknown application")
        async with state.page_lock:
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
        # Each click is its own demo posting. Reusing one job id would dedupe
        # them into a single application, which is right for a real posting and
        # wrong for a demo button someone presses three times.
        state.demo_counter += 1
        row = state.service.enqueue(
            job_url=f"{demo_url}/form?demo={state.demo_counter}",
            job_id=f"demo-{demo_url.rsplit(':', 1)[-1]}-{state.demo_counter}",
            route=DEMO_ROUTE,
            platform="DemoATS",
            title="Backend Engineer (demo)",
            company="ApplyOps Demo Co",
        )
        return {"application": row.to_dict(), "demo_url": demo_url}

    # ── frontend ─────────────────────────────────────────────────────

    if frontend_dist and Path(frontend_dist).is_dir():
        dist = Path(frontend_dist)

        @app.get("/")
        async def index() -> HTMLResponse:
            """Serve the console with this process's session token baked in.

            The page is only reachable on loopback, and the token only has to
            prove that a state-changing request came from the page we served --
            not that the user is authenticated, which on a single-user machine
            there is nobody to authenticate against.
            """
            html = (dist / "index.html").read_text(encoding="utf-8")
            # Only the placeholder is replaced. Editing the property name too
            # (which a naive substring replace did) leaves the page with a token
            # nobody can read -- which is how this shipped broken once.
            html = html.replace("__APPLYOPS_SESSION_TOKEN__", state.session_token)
            return HTMLResponse(html)

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
