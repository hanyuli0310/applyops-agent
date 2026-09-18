"""ApplyAgent — core loop orchestrating browser automation, LLMs, and guided memory.

The loop is also the flywheel: every question either gets served from memory
(silent) or goes to the human (noisy), and every run's outcome is credited or
charged back to the memories and selectors that were used. Over time this drives
the automation rate up and the number of interruptions down.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from applyops.browser import BrowserController, PageState
from applyops.llm.base import (
    AgentAction,
    AskUser,
    BaseLLM,
    CheckBox,
    ClickElement,
    Done,
    FillField,
    Navigate,
    ScrollDown,
    SelectOption,
    UploadFile,
    WaitForUser,
)
from applyops.memory import MemoryStore
from applyops.platforms.detector import detect_platform


# ── Events emitted by the agent ──────────────────────────────────────


class StatusEvent(BaseModel):
    message: str


class ScreenshotEvent(BaseModel):
    base64_data: str


class AskEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    context: str = ""
    suggestion: str = ""
    suggestion_confidence: float = 0.0


class WaitEvent(BaseModel):
    """Human must intervene (CAPTCHA, 2FA, login wall) before we can continue."""

    reason: str


class LearnEvent(BaseModel):
    """The flywheel just absorbed something. Surfaces progress to the UI."""

    question: str
    answer: str
    confidence: float
    auto: bool


class DoneEvent(BaseModel):
    status: str  # success, failed, paused
    summary: str = ""


class ErrorEvent(BaseModel):
    message: str


# Map a DOM interaction to a semantic role so selector knowledge is transferable
# between jobs on the same platform.
_ROLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("submit",), "submit_button"),
    (("review",), "review_button"),
    (("next", "continue"), "next_button"),
    (("dismiss", "close"), "dismiss_button"),
    (("easy apply", "jobs-apply", "apply button"), "apply_button"),
]


def infer_role(selector: str = "", reason: str = "") -> str:
    """Best-effort semantic role for a selector, used to build platform knowledge."""
    if 'input[type="file"]' in selector or "file" in selector:
        return "resume_upload"
    blob = f"{selector} {reason}".lower()
    for needles, role in _ROLE_RULES:
        if any(n in blob for n in needles):
            return role
    return ""


class ApplyAgent:
    """Core job application agent.

    Workflow:
    1. Navigate to target job URL
    2. Loop:
       a. Take screenshot & parse DOM
       b. Query LLM with visual + DOM state + profile + memories + platform selectors
       c. Execute actions (fill fields, click next/apply, upload resume)
       d. Questions resolve via: confident memory → semantic match → ask human
       e. If CAPTCHA or 2FA appears -> WaitForUser
    3. Attribute the run outcome back into every memory and selector that was used
    """

    def __init__(
        self,
        llm: BaseLLM,
        browser: BrowserController,
        memory: MemoryStore,
        callback: Optional[Callable[[Any], Any]] = None,
        max_actions: int = 50,
    ):
        self.llm = llm
        self.browser = browser
        self.memory = memory
        self.callback = callback
        self.max_actions = max_actions

        self._running = False
        self._user_answer_event = asyncio.Event()
        self._user_resume_event = asyncio.Event()
        self._pending_question_id: Optional[str] = None
        self._current_user_answer: Optional[str] = None

        # ---- per-run state used for flywheel attribution ----
        self._answers_used: list[str] = []  # QA ids consumed by this run
        self._hints: list[str] = []  # resolved answers awaiting a fill
        self._steps = 0
        self._recorded = False  # whether this run was written to history
        self._platform_name = "Unknown"

    # ── event plumbing ───────────────────────────────────────────────

    def _emit(self, event: Any):
        if self.callback:
            try:
                self.callback(event)
            except Exception as e:
                print(f"[Agent] Failed to emit event {event}: {e}")

    # ── main loop ────────────────────────────────────────────────────

    async def apply(self, job_url: str):
        """Execute the full application workflow for a job URL."""
        self._running = True
        self._platform_name = detect_platform(job_url).value

        if self.memory.is_already_applied(job_url):
            self._emit(ErrorEvent(message=f"Already applied to this job: {job_url}"))
            self._running = False
            return

        self._emit(
            StatusEvent(message=f"Detected platform: {self._platform_name}. Opening browser...")
        )

        try:
            if not self.browser.launched:
                await self.browser.launch()

            self._emit(StatusEvent(message=f"Navigating to {job_url}..."))
            await self.browser.goto(job_url)
            await asyncio.sleep(2)

            screenshot_bytes = await self.browser.screenshot()
            self._emit(
                ScreenshotEvent(base64_data=base64.b64encode(screenshot_bytes).decode("utf-8"))
            )

            action_count = 0
            while self._running and action_count < self.max_actions:
                action_count += 1
                self._steps = action_count

                page_state = await self.browser.get_page_state()
                screenshot_bytes = await self.browser.screenshot()
                self._emit(
                    ScreenshotEvent(base64_data=base64.b64encode(screenshot_bytes).decode("utf-8"))
                )

                memory_context = self._build_memory_context()
                task_context = self._build_task_context(page_state, action_count)

                self._emit(StatusEvent(message=f"Analyzing page (step {action_count})..."))

                try:
                    actions = await self.llm.analyze_page(
                        screenshot=screenshot_bytes,
                        page_state_text=page_state.model_dump_json(),
                        memory_context=memory_context,
                        task_context=task_context,
                    )
                except Exception as llm_err:
                    self._emit(ErrorEvent(message=f"LLM analysis failed: {llm_err}"))
                    await asyncio.sleep(3)
                    continue

                if not actions:
                    self._emit(StatusEvent(message="No actions detected, scrolling down..."))
                    await self.browser.scroll_down()
                    await asyncio.sleep(2)
                    continue

                for action in actions:
                    if not self._running:
                        break
                    await self._execute_action(action, page_state, job_url)

                    if isinstance(
                        action,
                        (
                            FillField,
                            ClickElement,
                            SelectOption,
                            CheckBox,
                            UploadFile,
                            ScrollDown,
                            Navigate,
                        ),
                    ):
                        await asyncio.sleep(1)
                        try:
                            s_bytes = await self.browser.screenshot()
                            self._emit(
                                ScreenshotEvent(
                                    base64_data=base64.b64encode(s_bytes).decode("utf-8")
                                )
                            )
                        except Exception:
                            pass

            # Fell out of the loop without an explicit Done → treat as failure and
            # charge it to everything this run relied on.
            if not self._recorded:
                reason = (
                    f"Stopped after reaching the action limit ({self.max_actions})."
                    if action_count >= self.max_actions
                    else "Run ended without reaching submission."
                )
                self._emit(ErrorEvent(message=reason))
                self._finish(status="failed", summary=reason, job_url=job_url, page_state=None)

        except Exception as e:
            self._emit(ErrorEvent(message=f"Agent error: {str(e)}"))
            if not self._recorded:
                self._finish(status="failed", summary=str(e), job_url=job_url, page_state=None)
        finally:
            self._running = False

    # ── context construction (what the model gets to see) ────────────

    def _build_memory_context(self) -> str:
        """Profile + trusted memories + validated selectors for this platform."""
        sections = [
            "=== Candidate Profile ===\n" + self.memory.get_profile_summary(),
            "=== Learned Q&A (authoritative — use these exact values) ===\n"
            + self.memory.get_qa_summary(),
        ]

        hints = self.memory.get_platform_hints_text(self._platform_name)
        if hints:
            sections.append(
                "=== Known Selectors for this platform (prefer these, they are validated) ===\n"
                + hints
            )

        pk = self.memory.get_platform(self._platform_name)
        if pk.runs:
            sections.append(
                "=== Platform experience ===\n"
                f"- Prior runs: {pk.runs}, success rate: {pk.success_rate:.0%}"
            )

        if self._hints:
            sections.append(
                "=== Answers resolved this run (must be typed verbatim) ===\n"
                + "\n".join(f"- {h}" for h in self._hints[-8:])
            )

        return "\n\n".join(sections)

    def _build_task_context(self, page_state: PageState, action_count: int) -> str:
        return (
            f"Applying for a job on {self._platform_name}. Current URL: {page_state.url}. "
            f"Cycle {action_count}/{self.max_actions}. "
            "Fill every field you can from the profile. For anything unknown, emit ask_user "
            "with the field's selector rather than guessing."
        )

    def _consume_hint(self, value: str):
        """Drop a hint once its value has actually been typed into the page."""
        if not value:
            return
        before = len(self._hints)
        self._hints = [h for h in self._hints if value not in h]
        if len(self._hints) != before:
            self._emit(StatusEvent(message="Confirmed field filled from memory."))

    # ── action execution ─────────────────────────────────────────────

    async def _execute_action(self, action: AgentAction, page_state: PageState, job_url: str):
        if isinstance(action, FillField):
            self._emit(StatusEvent(message=f"Filling {action.selector}"))
            await self._run_recorded(
                action.selector, action.reason, "fill", action.selector, action.value
            )
            self._consume_hint(action.value)

        elif isinstance(action, ClickElement):
            self._emit(StatusEvent(message=f"Clicking {action.selector} ({action.reason})"))
            await self._run_recorded(action.selector, action.reason, "click", action.selector)

        elif isinstance(action, SelectOption):
            self._emit(StatusEvent(message=f"Selecting '{action.value}' in {action.selector}"))
            await self._run_recorded(
                action.selector, action.reason, "select", action.selector, action.value
            )

        elif isinstance(action, UploadFile):
            await self._handle_upload(action)

        elif isinstance(action, CheckBox):
            self._emit(StatusEvent(message=f"Checking {action.selector}"))
            await self._run_recorded(
                action.selector, action.reason, "check", action.selector, action.checked
            )

        elif isinstance(action, ScrollDown):
            self._emit(StatusEvent(message="Scrolling down page..."))
            await self.browser.scroll_down()

        elif isinstance(action, Navigate):
            self._emit(StatusEvent(message=f"Navigating to {action.url}"))
            await self.browser.goto(action.url)

        elif isinstance(action, AskUser):
            await self._handle_question(action)

        elif isinstance(action, WaitForUser):
            self._emit(
                StatusEvent(message=f"Paused: {action.reason} — waiting for you in the browser.")
            )
            self._emit(WaitEvent(reason=action.reason))
            self._user_resume_event.clear()
            await self._user_resume_event.wait()
            self._emit(StatusEvent(message="Resuming application..."))

        elif isinstance(action, Done):
            self._finish(
                status=action.status or "success",
                summary=action.summary,
                job_url=job_url,
                page_state=page_state,
            )

    async def _run_recorded(
        self, selector: str, reason: str, op: str, target: str, value: Any = None
    ) -> bool:
        """Execute a DOM interaction and record whether the selector actually worked.

        This is the evidence stream that trains the platform knowledge base: a
        selector that keeps failing sinks in the ranking and stops being suggested.
        """
        role = infer_role(selector, reason)
        # Was this one of our own memories being put to work?
        suggested = selector in self.memory.get_selector_hints(self._platform_name).get(role, [])
        if suggested:
            self.memory.count_selector_suggestion()

        try:
            if op == "fill":
                await self.browser.fill_field(target, value)
            elif op == "click":
                await self.browser.click(target)
            elif op == "select":
                await self.browser.select_option(target, value)
            elif op == "check":
                await self.browser.check_checkbox(target, True if value is None else bool(value))
            elif op == "upload":
                await self.browser.upload_file(target, value)
        except Exception as e:
            if role:
                self.memory.record_selector_result(self._platform_name, role, selector, False)
            self._emit(ErrorEvent(message=f"Action failed on {selector}: {e}"))
            return False

        if role:
            self.memory.record_selector_result(self._platform_name, role, selector, True)
        return True

    # ── resume upload ────────────────────────────────────────────────

    async def _handle_upload(self, action: UploadFile):
        file_path = action.file_path
        if not file_path or file_path == "resume":
            file_path = self.memory.get_profile().get("resume_path", "")

        if not file_path:
            q_id = str(uuid.uuid4())
            self._emit(
                AskEvent(
                    id=q_id,
                    question="Please provide the local file path to your resume "
                    "(e.g. /Users/you/resume.pdf):",
                    context="Resume upload required",
                )
            )
            answer = await self._wait_for_answer(q_id)
            if not answer:
                return
            file_path = answer
            self.memory.set_profile("resume_path", answer)
            self._emit(
                StatusEvent(message="Learned resume path — uploaded automatically next time.")
            )

        self._emit(StatusEvent(message=f"Uploading resume: {file_path}"))
        await self._run_recorded(
            action.selector, action.reason, "upload", action.selector, file_path
        )

    # ── question resolution: the heart of the flywheel ───────────────

    async def _handle_question(self, action: AskUser):
        """Resolve a question: memory → semantic match → human (with a prefilled guess).

        Every path returns a concrete value, unlike the previous implementation
        which could find an answer in memory and then do nothing with it.
        """
        answer: Optional[str] = None
        qa_id: Optional[str] = None
        auto = False

        # 1. Confident local memory.
        qa = self.memory.get_confident_answer(action.question)
        if qa is not None:
            answer, qa_id, auto = qa.answer, qa.id, True
            qa.mark_used()  # this memory just did real work
            self._emit(
                StatusEvent(message=f"Auto-filled from memory (confidence {qa.confidence:.2f})")
            )

        # 2. Semantic match across everything we've ever learned.
        if answer is None:
            answer, qa_id, auto = await self._semantic_lookup(action.question)

        # 3. Nothing good enough — ask the human, but never with an empty box.
        if answer is None:
            answer, qa_id = await self._ask_human(action)

        if not answer:
            return

        if qa_id:
            self._answers_used.append(qa_id)
            self.memory.record_question(automated=auto)
            entry = self.memory.get_qa(qa_id)
            if entry is not None:
                self._emit(
                    LearnEvent(
                        question=entry.question,
                        answer=answer,
                        confidence=entry.confidence,
                        auto=auto,
                    )
                )

        # Actually apply it: directly if we have a selector, otherwise hand it back
        # to the model verbatim on the next cycle.
        if action.selector:
            self._emit(StatusEvent(message=f"Filling answer into {action.selector}"))
            await self._run_recorded(
                action.selector, action.question, "fill", action.selector, answer
            )
        else:
            self._hints.append(f'The answer to "{action.question}" is: {answer}')

    async def _semantic_lookup(self, question: str):
        """LLM-assisted recall; a hit is cached under this wording so it's cheap next time."""
        entries = self.memory.get_all_qa()
        if not entries:
            return None, None, False
        try:
            matched = await self.llm.match_memory(question, entries)
        except Exception:
            return None, None, False
        if not matched:
            return None, None, False

        entry = self.memory.learn(question, matched, context="semantic match", source="derived")
        entry.mark_used()
        self._emit(StatusEvent(message="Matched by meaning — learned this wording too."))
        return matched, entry.id, True

    async def _ask_human(self, action: AskUser):
        suggestion = self.memory.get_suggestion(action.question)
        q_id = str(uuid.uuid4())
        self._emit(
            AskEvent(
                id=q_id,
                question=action.question,
                context=action.context
                or "The application asks for this. Your answer becomes permanent memory.",
                suggestion=suggestion.answer if suggestion else "",
                suggestion_confidence=suggestion.confidence if suggestion else 0.0,
            )
        )
        answer = await self._wait_for_answer(q_id)
        if not answer:
            return None, None
        entry = self.memory.learn(action.question, answer, context=action.context)
        entry.mark_used()
        self._emit(StatusEvent(message="Learned — will be reused on future applications."))
        return answer, entry.id

    async def _wait_for_answer(self, question_id: str) -> Optional[str]:
        """Block until the user answers this specific question."""
        self._pending_question_id = question_id
        self._current_user_answer = None
        self._user_answer_event.clear()
        await self._user_answer_event.wait()
        self._pending_question_id = None
        return self._current_user_answer

    # ── run closure & attribution ────────────────────────────────────

    def _finish(
        self,
        status: str,
        summary: str,
        job_url: str,
        page_state: Optional[PageState],
    ):
        """Close the flywheel: record the run and credit/charge everything used."""
        if self._recorded:
            return
        self._recorded = True
        self._running = False

        status = status if status in ("success", "failed", "paused", "applied") else "success"
        self.memory.add_application(
            job_url=job_url,
            job_title=page_state.title if page_state else "",
            platform=self._platform_name,
            status=status,
            notes=summary,
            answers_used=list(dict.fromkeys(self._answers_used)),
            steps=self._steps,
        )
        self._emit(DoneEvent(status=status, summary=summary))

    # ── external control ─────────────────────────────────────────────

    async def provide_answer(self, question_id: str, answer: str):
        """Deliver an answer to the question currently blocking the loop."""
        if self._pending_question_id and question_id and question_id != self._pending_question_id:
            # Stale reply for a question we've moved past — drop it.
            print(f"[Agent] Ignored answer for stale question {question_id}")
            return
        self._current_user_answer = answer or None
        self._user_answer_event.set()

    async def answer_question(self, question_id: str, answer: str):
        await self.provide_answer(question_id, answer)

    async def resume(self):
        self._user_resume_event.set()

    async def stop(self):
        self._running = False
        self._user_answer_event.set()
        self._user_resume_event.set()
        self._emit(StatusEvent(message="Agent stopped by user."))
