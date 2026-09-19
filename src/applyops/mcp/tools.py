"""MCP tool definitions.

Design rules that are load-bearing, not stylistic:

1. **No bare actions.** `fill_field` requires ``role`` and ``reason``, and
   records the outcome against the platform's selector memory itself. There is
   no `fill_field(ref, value)`. The old implementation recorded selector
   outcomes in its *caller* (`agent.py`), which meant that the moment a
   different agent loop drove the browser, the flywheel stopped being fed while
   still *looking* alive -- memory.json present, UI present, hit rates frozen at
   their seeded priors. Moving the recording inside the tool makes that
   impossible to bypass, because the bypass would require a tool we do not ship.

2. **`get_answer` returns the answer, not the question.** When memory is
   confident it returns the value directly; the answer never has to enter the
   harness's context. That saves tokens and, more importantly, removes any
   chance of a model paraphrasing a factual answer on its way to a form field.

3. **Guardrails are checked in the tool, not requested of the harness.** A
   prompt that says "do not exceed 20 applications a day" is a suggestion. A
   `preflight` that returns `allowed: false` is a control.

Everything returns a JSON string so any client can parse it without relying on
structured-content support.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import discovery
from ..action_policy import decide_click
from ..browser import BrowserController
from ..evidence import (
    detect_final_action,
    evidence_platform,
    success_patterns_for,
)
from ..memory import extract_job_id
from ..platforms.naming import platform_for_url, resolve_route
from ..prepare import PrepareRefused
from ..prepare import prepare_application as prepare_application_impl
from ..resume import (
    ResumeError,
    ResumeRef,
    resolve_resume,
)
from ..state_machine import ApplicationState, InvalidTransition
from ..submission import (
    FinalAction,
    SubmissionRefused,
)
from .runtime import BrowserBusy, Runtime

# A tool call that blocks this long is a bug, not a rate limit.
MAX_INLINE_WAIT_SECONDS = 300.0


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _busy_payload(runtime: Runtime, exc: BrowserBusy) -> str:
    """The refusal a harness can act on, rather than a stack trace.

    A structured reply rather than an exception, on the two tools that are
    documented entry points: a harness that cannot open a browser should be told
    who has it and what to do, in the same shape as every other tool result.
    Every other browser tool would reach the same refusal if called first, and
    gets the exception instead -- which this MCP stack already renders as a
    readable "<tool> failed: ..." message.
    """
    return _json(
        {
            "ok": False,
            "error": str(exc),
            "browser_busy": True,
            "holder": exc.holder,
            "lock_path": str(runtime.profile_lock.path),
            "hint": (
                "The shared Chrome profile is single-driver by design. Let the "
                "other driver finish, stop the scheduled loop "
                "(`tools/cron_apply.py --stop`), or close this session's browser "
                "with `browser_close` and retry."
            ),
        }
    )


def register(server: MCPServer, runtime: Runtime) -> None:
    """Attach every tool to `server`."""

    # ── liveness ─────────────────────────────────────────────────────

    @server.tool()
    async def ping() -> str:
        """Check that the ApplyOps server is reachable."""
        return _json({"ok": True, "service": "applyops", "tools_ready": True})

    # ── discovery ────────────────────────────────────────────────────

    @server.tool()
    async def discover_jobs(
        keywords: str,
        location: str = "",
        limit: int = 25,
        easy_apply_only: bool = True,
        recent_days: int = 0,
    ) -> str:
        """Search LinkedIn for postings, newest first.

        Use this when the user wants jobs *found* rather than handed a link.
        Defaults to Easy Apply only, because that is the route this server can
        actually finish; "Apply on company site" hands off to an ATS whose form
        we have never seen.

        Returns the postings plus the exact `search_url` used, so a surprising
        result can be traced back to the query that produced it.

        This applies to nothing. It only lists. Take a `job_id` or `url` from
        the result and run the normal `preflight` -> apply flow on it.
        """
        async with runtime.lock:
            try:
                browser = await runtime.get_browser()
            except BrowserBusy as exc:
                return _busy_payload(runtime, exc)
            result = await discovery.search(
                browser,
                keywords=keywords,
                location=location,
                limit=limit,
                easy_apply_only=easy_apply_only,
                recent_days=recent_days,
            )
            return _json(
                {
                    "search_url": result.search_url,
                    "count": len(result.jobs),
                    # `rendered_cards` is what the extractor actually parsed;
                    # `unrendered` is how many slots the virtualised list never
                    # drew. A large `unrendered` means "scroll further", not
                    # "there is nothing else" -- collapsing them into one
                    # number would hide exactly the failure worth seeing.
                    "rendered_cards": result.rendered_cards,
                    "unrendered": result.unrendered,
                    "notes": result.notes,
                    "error": result.error,
                    "jobs": [j.model_dump() for j in result.jobs],
                }
            )

    # ── browser ──────────────────────────────────────────────────────

    @server.tool()
    async def browser_open(url: str) -> str:
        """Open a URL in the automation browser.

        The browser launches on first use with a persistent profile, so a
        LinkedIn session established once is reused on later runs.

        Only one process may drive that profile at a time -- two Chromes on one
        profile delete each other's cookies, and the logged-in session cannot be
        rebuilt from here. If another driver (the scheduled loop, a batch run)
        has it, this returns `browser_busy: true` with the holder's name instead
        of opening a second browser. Do not retry in a loop; the holder is
        usually mid-application.
        """
        async with runtime.lock:
            try:
                browser = await runtime.get_browser()
            except BrowserBusy as exc:
                return _busy_payload(runtime, exc)
            await browser.goto(url, settle=2.0)
            return _json(
                {
                    "url": browser.page.url,
                    "platform": platform_for_url(browser.page.url),
                    "title": await browser.page.title(),
                }
            )

    @server.tool()
    async def browser_state(include_text: bool = False) -> str:
        """List the current page's form fields, buttons and tabs.

        Every field carries a `ref` string. Pass that same `ref` back to
        `fill_field` / `select_option` / `click_target`; do not construct your
        own CSS selector, because the references are already chosen to survive
        the site's generated markup.

        Fields with `required: true` must be filled before submitting.
        `field_type` tells you which action to use:
          - text/email/tel/number/textarea/contenteditable -> fill_field
          - select/combobox/listbox                         -> select_option
          - radio/checkbox                                  -> set_checkbox
          - file                                            -> upload_file
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            state = await browser.get_page_state()
            return _json(
                {
                    "url": state.url,
                    "title": state.title,
                    "platform": platform_for_url(state.url),
                    "error": state.error,
                    "fields": [f.model_dump() for f in state.form_fields],
                    "buttons": [b.model_dump() for b in state.buttons],
                    "tabs": [t.model_dump() for t in state.tabs],
                    **({"text": state.text_content} if include_text else {}),
                }
            )

    @server.tool()
    async def browser_screenshot() -> str:
        """Save a screenshot of the visible viewport and return its path.

        Use this when the field list looks wrong or empty: the DOM can be read
        literally, but only a picture shows whether the page is where you think
        it is.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            target = runtime.data_dir / "screenshots"
            target.mkdir(parents=True, exist_ok=True)
            path = target / "latest.png"
            path.write_bytes(await browser.screenshot())
            return _json({"path": str(path), "url": browser.page.url})

    @server.tool()
    async def browser_tabs() -> str:
        """List open tabs. A click may open a second tab (employer ATS sites)."""
        async with runtime.lock:
            browser = await runtime.get_browser()
            return _json({"tabs": [t.model_dump() for t in await browser.list_tabs()]})

    @server.tool()
    async def browser_switch_tab(index: int) -> str:
        """Make another tab the active one, by index from `browser_tabs`."""
        async with runtime.lock:
            browser = await runtime.get_browser()
            ok = await browser.switch_tab(index)
            return _json(
                {"switched": ok, "url": browser.page.url if ok else "",
                 "platform": platform_for_url(browser.page.url) if ok else ""}
            )

    @server.tool()
    async def browser_scroll(direction: str = "down", amount: float = 1.0) -> str:
        """Scroll the viewport. `direction` is 'down' or 'up'."""
        async with runtime.lock:
            browser = await runtime.get_browser()
            await browser.scroll(direction, amount)
            return _json({"scrolled": direction, "url": browser.page.url})

    # ── form actions (recording happens inside these) ────────────────

    def _record(browser: BrowserController, ref: str, role: str, success: bool) -> None:
        """Feed the flywheel. Called by the tool, never by the caller."""
        platform = runtime.current_platform()
        runtime.memory.record_selector_result(platform, role, ref, success)

    @server.tool()
    async def fill_field(ref: str, value: str, role: str, reason: str) -> str:
        """Type a value into a text-like field.

        `ref` comes from `browser_state`. `role` names what the field is for
        (for example "phone", "first_name") and `reason` explains why this value
        is correct; both are recorded so the same field is faster to fill next
        time. There is no version of this tool without them, on purpose.

        The value is typed character by character and then read back. If the
        read-back disagrees with what was typed, the tool reports a mismatch
        instead of pretending to have succeeded.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
        # Every failure (unverified included) is still an attempt: the request
        # may have gone out, so the slot is spent either way.
        result = await browser.fill_field(ref, value)
        _record(browser, ref, role, result.ok)
        return _json(
            {
                "ok": result.ok,
                "verification": result.verification,
                "readback": result.readback,
                "mismatch": result.mismatch,
                "detail": result.detail,
                "error": result.error,
                "role": role,
                "reason": reason,
            }
        )

    @server.tool()
    async def select_option(ref: str, value: str, role: str, reason: str) -> str:
        """Choose an option in a dropdown.

        Handles both a real `<select>` and a custom control that opens a popup,
        so the same call works on LinkedIn and on Workday-style forms.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
        result = await browser.select_option(ref, value)
        _record(browser, ref, role, result.ok)
        return _json(
            {
                "ok": result.ok,
                "verification": result.verification,
                "selected": result.selected,
                "readback": result.readback,
                "detail": result.detail,
                "strategy": result.strategy,
                "error": result.error,
                "role": role,
                "reason": reason,
            }
        )

    @server.tool()
    async def set_checkbox(ref: str, checked: bool, role: str, reason: str) -> str:
        """Check or uncheck a checkbox or radio button."""
        async with runtime.lock:
            browser = await runtime.get_browser()
        result = await browser.set_checkbox(ref, checked)
        _record(browser, ref, role, result.ok)
        return _json(
            {
                "ok": result.ok,
                "verification": result.verification,
                "checked": result.checked,
                "detail": result.detail,
                "error": result.error,
                "role": role,
                "reason": reason,
            }
        )

    @server.tool()
    async def upload_file(ref: str, file_path: str, role: str, reason: str) -> str:
        """Attach a local file to a file input (typically a resume).

        The reply reports what the input holds *afterwards*, and compares the
        file against the profile's configured resume. Both matter: an input that
        still reports nothing is `unverifiable` and must be treated as not
        attached, and a file that is not the configured resume is either an older
        revision or somebody else's -- either way it must not go out silently.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            result = await browser.upload_file(ref, file_path)

            comparison = _compare_resume(runtime, file_path)
            if comparison["mismatch"]:
                # Still recorded as whatever the page verified: refusing to
                # upload is not useful, but claiming it is the right file is a lie.
                result.detail = (result.detail or "") + " " + comparison["detail"]

            _record(browser, ref, role, result.ok)
            return _json(
                {
                    "ok": result.ok,
                    "verification": result.verification,
                    "detail": result.detail,
                    "attachments": result.attachments,
                    "resume_match": comparison["match"],
                    "resume_detail": comparison["detail"],
                    "path": result.path,
                    "error": result.error,
                    "role": role,
                    "reason": reason,
                }
            )

    @server.tool()
    async def attachment_state(ref: str) -> str:
        """What a file input currently holds, without touching it.

        Use before deciding an upload succeeded. A form frequently arrives with
        a file already chosen -- LinkedIn keeps the last one selected, and ATS
        forms re-populate a previously parsed resume -- and an attachment this
        session did not put there is not evidence that it is the user's choice.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            observed, readable = await browser.read_attachments(ref)
            return _json(
                {
                    "readable": readable,
                    "attachments": [{"name": f.name, "size": f.size} for f in observed],
                    "hint": "" if readable else "could not read this input's file list",
                }
            )

    @server.tool()
    async def click_target(name: str = "", ref: str = "", role: str = "", reason: str = "") -> str:
        """Click a button or link, by visible/accessible `name` or by `ref`.

        If the click opens a new tab, this follows it automatically and reports
        `new_tab: true` -- "Apply on company site" leads to the employer's own
        ATS in a new tab, and continuing to drive the old tab would lose the
        application entirely.

        **This cannot press the final submit.** Gating the button named
        "Submit" is not enough: a `<button>` inside a form submits it whatever it
        says, so the control is checked structurally as well as by name (`Save`,
        `Continue`, any label at all can be the end of the form). Anything that looks like the end of the
        application is refused here and must go through `submit_final`, which is
        the only path that requires a verified grant. Walking a multi-step form
        (Next, Review, Continue) is unaffected.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            facts, inspect_error = await browser.inspect_target(ref=ref, name=name)
            if facts is None:
                return _json({"clicked": False, "error": inspect_error})

            decision = decide_click(facts, authorized=False)
            if not decision.allowed:
                return _json(
                    {
                        "clicked": False,
                        "refused": True,
                        "reason": decision.reason,
                        **decision.to_dict(),
                        "next_step": (
                            "call prepare_submission / request_submission_grant, "
                            "have the human approve it, then submit_final"
                        ),
                    }
                )

            result = await browser.click(ref=ref, name=name)
            if role:
                _record(browser, ref or name, role, result.clicked)
            return _json(
                {
                    "clicked": result.clicked,
                    "new_tab": result.new_tab,
                    "active_url": result.active_url,
                    "platform": platform_for_url(result.active_url),
                    "target_class": decision.target_class.value,
                    "error": result.error,
                    "reason": reason,
                }
            )

    # ── memory: the flywheel ─────────────────────────────────────────

    @server.tool()
    async def get_answer(question: str) -> str:
        """Look up a previously learned answer to an application question.

        Call this for **every** question on a form before deciding to ask the
        user. When it answers, use the value as-is -- it is a stored fact, and
        paraphrasing it risks putting a wrong answer on a real application.

        `status` meanings:
          - "answered"    -- confident match; use `answer` directly
          - "suggestion"  -- plausible but not yet proven; confirm with the user
          - "need_human"  -- nothing usable stored; ask the user, then call
                             `record_answer` so the next run does not have to
        """
        found = runtime.memory.get_confident_answer(question)
        if found is not None:
            found.mark_used()
            runtime.memory.record_question(automated=True)
            runtime.memory._save()
            return _json(
                {
                    "status": "answered",
                    "answer": found.answer,
                    "confidence": found.confidence,
                    "question_id": found.id,
                    "use_answer_verbatim": True,
                }
            )

        suggestion = runtime.memory.get_suggestion(question)
        if suggestion is not None:
            # A suggestion means the caller is about to put this in front of the
            # human. Record that, or `asked_count` stays 0 forever and the stats
            # cannot distinguish "never surfaced" from "surfaced and rejected".
            suggestion.mark_asked()
            runtime.memory._save()
            return _json(
                {
                    "status": "suggestion",
                    "answer": suggestion.answer,
                    "confidence": suggestion.confidence,
                    "question_id": suggestion.id,
                }
            )

        runtime.memory.record_question(automated=False)
        return _json({"status": "need_human", "question": question})

    @server.tool()
    async def record_answer(question: str, answer: str, context: str = "") -> str:
        """Store a user-supplied answer so future applications reuse it.

        Call this immediately after the user answers something, including the
        exact wording of the question as the form asked it.
        """
        entry = runtime.memory.learn(question, answer, context=context, source="user")
        return _json(
            {
                "stored": True,
                "question_id": entry.id,
                "normalized_key": entry.key,
                "confidence": entry.confidence,
                "total_learned": len(runtime.memory.get_all_qa()),
            }
        )

    @server.tool()
    async def get_selector_hints(platform: str) -> str:
        """Known-good element references for a platform, best first.

        These are learned from real runs. Feed them back through `browser_state`
        refs where possible; the raw values are also useful when a form renders
        differently than expected.
        """
        hints = runtime.memory.get_selector_hints(platform)
        return _json({"platform": platform, "hints": hints,
                      "adapter_gaps": runtime.memory.get_adapter_gaps(5)})

    @server.tool()
    async def route_guide(job_url: str) -> str:
        """What kind of application this job is, and what it will take.

        Call this **before** `browser_open`. There is more than one way to
        apply, and the differences are structural: a LinkedIn Easy Apply lives
        entirely in a modal on the posting, while an "external" posting hands
        the browser to the employer's own account system, where a sign-in --
        often an emailed one-time code -- stands *before* the first field.

        Any step carrying `human_required: true` cannot be completed by the
        machine. Ask the user for those up front, in the same message as the
        profile questions, rather than walking into the gate and stopping there.

        `runs` / `success_rate` / `hardest_gate` are what this route has
        actually done here before. A route with `runs: 0` is a shape we know
        about but have never driven -- read its steps as a map, not as evidence.
        """
        platform = platform_for_url(job_url)
        candidates = runtime.memory.routes_for_url(job_url)
        return _json({
            "job_url": job_url,
            "platform": platform,
            "routes": [
                {
                    "route": record.route,
                    "key": record.key,
                    "entry_signature": record.entry_signature,
                    "prerequisites": record.prerequisites,
                    "steps": [step.model_dump() for step in record.steps],
                    "human_gates": record.human_gates,
                    "runs": record.runs,
                    "success_rate": round(record.success_rate, 3),
                    "hardest_gate": record.hardest_gate,
                    "notes": record.notes,
                }
                for record in candidates
            ],
        })

    @server.tool()
    async def setup_status(include_optional: bool = False) -> str:
        """Whether the user's profile is complete, and the questions to ask.

        Call this **before the first application** and any time `get_profile`
        reports missing fields. If `ready` is false, ask the user the questions
        in `questionnaire` -- all of them in a single message, not one at a
        time -- then hand the answers back through `save_profile`.

        Every field carries its current value, so a second run only needs to ask
        about what is still blank. Fields already answered are never re-asked;
        that is the whole point of the profile being a file.

        Prefer `save_profile` over `update_profile` for this: it writes the whole
        set at once and reports validation problems together.
        """
        profile = runtime.memory.profile
        status = profile.status()
        return _json({
            **status,
            "questionnaire": profile.questionnaire(
                include_optional=include_optional, only_missing=True
            ),
            "profile_example": str(_example_profile_path()),
        })

    @server.tool()
    async def save_profile(answers: dict) -> str:
        """Store setup answers in the user's profile file.

        `answers` maps field keys to values. Booleans accept yes/no, numbers are
        read leniently ("150k", "$150,000" and "150000" all mean 150000), paths
        are expanded to absolute. Empty values clear a field.

        Check `warnings` in the reply: anything listed there was rejected and is
        *not* stored, so it must be re-asked rather than assumed. `unknown_keys`
        lists keys that are stored but not part of the spec, which usually means
        a typo in a key name.
        """
        report = runtime.memory.update_profile(answers)
        return _json({
            **report,
            "profile": runtime.memory.get_profile(),
            "profile_path": str(runtime.memory.profile.path),
        })

    @server.tool()
    async def get_profile() -> str:
        """The candidate's stored facts (name, email, phone, resume path, ...).

        Treat these as authoritative for form fields, and use them *verbatim* --
        do not reformat a phone number or round a salary. If a field an
        application needs is not here, call `setup_status` to get the question
        and ask the user; never infer an answer.

        The values come from a markdown file the user may have edited by hand,
        so they are exactly what the user intends, not a normalized copy.
        """
        return _json({
            "profile": runtime.memory.get_profile(),
            "missing_required": runtime.memory.profile.missing_required(),
            "profile_path": str(runtime.memory.profile.path),
        })

    @server.tool()
    async def update_profile(fields: dict) -> str:
        """Change one or a few known profile fields mid-run.

        For example after asking a single follow-up question. For initial setup
        prefer `save_profile`, which reports all validation problems at once.
        """
        report = runtime.memory.update_profile(fields)
        return _json({
            **report,
            "profile": runtime.memory.get_profile(),
            "profile_path": str(runtime.memory.profile.path),
        })

    @server.tool()
    async def flywheel_stats() -> str:
        """Whether the memory is actually compounding.

        Watch `automation_rate` (share of questions answered without asking) and
        `selectors_suggested`. A `selectors_suggested` of 0 after real runs means
        nothing is being recorded, and the memory is not learning.
        """
        return _json(runtime.memory.get_stats())

    @server.tool()
    async def application_history(limit: int = 20) -> str:
        """Past applications, newest first."""
        history = runtime.memory.get_history()
        return _json({"total": len(history), "applications": history[-limit:][::-1]})

    @server.tool()
    async def check_already_applied(job_url: str, job_id: str = "") -> str:
        """Whether this posting was already applied to.

        Matched on the job id, so the same posting reached through a different
        tracking link is still recognised.
        """
        resolved_id = job_id or extract_job_id(job_url)
        record = runtime.memory.find_application(job_url, resolved_id)
        return _json(
            {
                "already_applied": runtime.memory.is_already_applied(job_url, resolved_id),
                "job_id": resolved_id,
                "previous": record.model_dump() if record else None,
            }
        )

    @server.tool()
    async def record_vision_fallback(
        field_label: str, dom_attempts: int = 0, succeeded: bool = True, platform: str = ""
    ) -> str:
        """Record that the DOM locator failed and vision had to resolve a field.

        This is an adapter gap report, not bookkeeping: each entry names a field
        the platform adapter cannot address, which is exactly the rule that is
        missing. See `flywheel_stats().adapter_gaps` for the ranking.
        """
        if not platform:
            platform = runtime.current_platform()
        runtime.memory.record_vision_fallback(platform, field_label, dom_attempts, succeeded)
        return _json({"recorded": True, "platform": platform, "field_label": field_label})

    # ── guardrails ───────────────────────────────────────────────────

    @server.tool()
    async def preflight(job_url: str, job_id: str = "") -> str:
        """Ask permission before starting an application. Call this first.

        Checks the daily cap, the minimum spacing between applications, whether
        this posting was already applied to, and whether a previous run tripped
        the failure breaker.

        If `allowed` is false, do not proceed -- the `reason` says why. If
        `wait_seconds` is greater than zero this call has already waited, so the
        pacing is enforced rather than merely suggested.
        """
        decision = runtime.guardrails.preflight(job_url, job_id)

        # Enforce the spacing here: returning a number for the caller to respect
        # would not actually pace anything.
        if decision.allowed and decision.wait_seconds > 0:
            wait = min(decision.wait_seconds, MAX_INLINE_WAIT_SECONDS)
            await asyncio.sleep(wait)

        return _json(decision.model_dump())

    @server.tool()
    async def guard_status() -> str:
        """Current safety state: remaining quota today, failures, halted or not."""
        return _json(runtime.guardrails.stats())

    @server.tool()
    async def request_submit_confirmation(summary: str, job_url: str, job_id: str = "") -> str:
        """Legacy confirmation row. Retained for compatibility; authorizes nothing.

        The token this returns used to be the entire gate on submitting, but it
        was checked *after* the click had already happened and it trusted a
        boolean the caller supplied itself. Since M1:

        - the final submit can only be performed by `submit_final`, which needs a
          grant a human minted separately;
        - `click_target` refuses any control that ends the application.

        So this is now a record of intent that nothing enforces. Prefer
        `request_submission_grant`, whose summary is generated from values read
        back off the live form rather than written by whoever is asking.
        """
        confirmation = runtime.guardrails.request_submit_confirmation(
            summary, job_url, job_id
        )
        return _json(
            {
                "confirmation_id": confirmation.id,
                "job_key": confirmation.job_key,
                "job_url": confirmation.job_url,
                "summary_to_show": confirmation.summary,
                "expires_in_seconds": 900,
                "authorizes_submission": False,
                "next_step": (
                    "this token no longer gates submission. Use "
                    "request_submission_grant -> human approval -> submit_final."
                ),
            }
        )

    @server.tool()
    async def submit_application(grant_id: str, job_url: str, job_id: str = "") -> str:
        """Read back what the ledger recorded for an approved submission.

        This tool used to *accept* `outcome="verified"` from its caller, which
        meant a model could type the word "verified" and have a success written
        into history. A verdict has to come from evidence, so this is now a
        read-only view of the attempt row that `submit_final` produced: the
        outcome, the grant, the matched confirmation text and the page it was
        seen on.

        It performs no submission and records nothing. If nothing was submitted
        under that grant, it says so -- "approved but never sent" is a real state
        and not a success.
        """
        grant = runtime.authorizer.peek(grant_id)
        if grant is None:
            return _json({"recorded": False, "error": "unknown grant id"})
        if not grant.used:
            return _json(
                {
                    "recorded": False,
                    "error": (
                        "that grant was never spent, so there is no submission to "
                        "report. Ask for the outcome from submit_final instead."
                    ),
                }
            )

        row = (
            runtime.service.get(grant.application_id)
            if grant.application_id
            else runtime.service.ledger.find_by_job_key(grant.job_key)
        )
        if row is None:
            return _json(
                {"recorded": False, "error": "no application on record for this grant"}
            )

        attempt = next(
            (a for a in runtime.service.ledger.attempts(row.id) if a.grant_id == grant_id),
            None,
        )
        if attempt is None:
            return _json(
                {
                    "recorded": False,
                    "error": "the ledger holds no attempt for this grant; nothing to report",
                }
            )

        return _json(
            {
                "recorded": True,
                "application_id": row.id,
                "application_state": row.state,
                "outcome": attempt.outcome,
                "detail": attempt.detail,
                "evidence": attempt.to_dict().get("evidence", {}),
                "grant_id": grant_id,
                "hint": (
                    "the outcome above is what the page said at submit time; no "
                    "caller chose it."
                ),
            }
        )


    @server.tool()
    async def report_failure(
        note: str = "",
        platform: str = "",
        route: str = "",
        blocked_at: str = "",
    ) -> str:
        """Report that the current application attempt failed.

        Consecutive failures trip the breaker and stop the run. That is
        deliberate: a run that keeps failing is usually failing for one systemic
        reason, and continuing multiplies the damage instead of the results.

        If the attempt died at a recognisable step, pass `platform`, `route` and
        `blocked_at` (a short description of that step). The failure is then
        filed against the route instead of merely counted, which is what turns
        "this kind of application is hard" into an adapter worth writing.
        """
        runtime.guardrails.record_outcome(success=False, note=note)
        filed = None
        if platform and route and blocked_at:
            filed = runtime.memory.record_route_blockage(
                platform, route, blocked_at, notes=note
            ).model_dump()
        return _json({**runtime.guardrails.stats(), "route_blockage": filed})

    @server.tool()
    async def reset_halt() -> str:
        """Clear a tripped failure breaker so a run may resume."""
        runtime.guardrails.reset_halt()
        return _json(runtime.guardrails.stats())

    @server.tool()
    async def form_snapshot() -> str:
        """Every answerable field on the current page, with the value it holds.

        Read from the DOM, not from what anything believes it typed. This is what
        a submission grant binds to, so this is what gets approved: fields that
        came back as `<unreadable>` are named rather than skipped, because
        approving a form containing one should be a decision, not a gap.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            snapshot = await browser.field_snapshot()
            return _json(
                {
                    "url": browser.page.url,
                    "platform": evidence_platform(browser.page.url),
                    "fields": snapshot,
                    "unreadable": [k for k, v in snapshot.items() if v == "<unreadable>"],
                }
            )

    @server.tool()
    async def prepare_application(application_id: str) -> str:
        """Fill an application's form, verify it, and file the approval request.

        This is the step between `enqueue_application` and
        `request_submission_grant`, and it used to be missing from MCP
        altogether: an agent could enqueue an application and then be refused at
        submit time because nothing had moved it out of QUEUED.

        It fills from the profile and the scoped answers, verifies every value it
        writes, attaches the configured resume, and **only then** asks for
        approval. Anything unresolved parks the application in
        `waiting_for_input` and names the fields -- show that list to the user and
        call `record_answer` for the missing questions, then call this again.

        The route decides whether this may run at all: a route with no verified
        submission path is parked rather than driven as something it is not.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            try:
                outcome = await prepare_application_impl(
                    runtime.service, browser, application_id
                )
            except KeyError:
                return _json({"error": f"unknown application {application_id}"})
            except PrepareRefused as exc:
                return _json(
                    {
                        "state": "refused",
                        "application_id": application_id,
                        "reason": str(exc),
                    }
                )
            payload = outcome.to_dict()
            payload["application_id"] = application_id
            if outcome.state == ApplicationState.WAITING_FOR_INPUT.value:
                payload["next_step"] = (
                    "show `missing` to the user, record their answers with "
                    "record_answer, then call prepare_application again"
                )
            elif outcome.ready:
                payload["next_step"] = (
                    "call request_submission_grant for this application, show its "
                    "summary to the user, and let a human approve it"
                )
            return _json(payload)

    @server.tool()
    async def request_submission_grant(
        job_url: str,
        job_id: str = "",
        route: str = "easy_apply",
        application_id: str = "",
    ) -> str:
        """Ask a human to authorize one specific submission. Authorizes nothing itself.

        This creates a **pending request** carrying everything the decision
        depends on: the field values as read back from the live form, the resume
        digest, the profile and answer revisions, the route and an expiry. A
        human then approves it out of band -- `applyops approve <request_id>` --
        which is what mints the one-time grant `submit_final` needs.

        The split is the boundary. If the process asking for permission could
        also grant it, then "the user said yes" would be indistinguishable from
        "the caller decided to say the user said yes", and the check would only
        ever fail by accident.

        Show `summary_to_show` to the user verbatim. Do not paraphrase it: these
        are the values about to reach an employer.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            try:
                resume = _current_resume(runtime)
            except ResumeError as exc:
                return _json({"granted": False, "error": f"resume problem: {exc}"})

            snapshot = await browser.field_snapshot()
            row = (
                runtime.service.get(application_id)
                if application_id
                else runtime.service.ledger.find_by_job_key(
                    job_id or extract_job_id(job_url) or job_url
                )
            )
            if row is None:
                return _json(
                    {
                        "granted": False,
                        "error": (
                            "no application on record; call enqueue_application "
                            "then prepare_application before asking for approval"
                        ),
                    }
                )

            profile_revision, answers_revision = runtime.service.revisions()
            request = runtime.authorizer.create_request(
                job_key=row.job_key,
                job_url=row.job_url,
                route=row.route or resolve_route(row.job_url, row.platform),
                platform=row.platform,
                fields=snapshot,
                resume_filename=resume.filename,
                resume_sha256=resume.sha256,
                answers_revision=answers_revision,
                profile_revision=profile_revision,
                application_id=application_id,
                page_url=browser.page.url,
                requested_by="mcp_relayed_to_human",
            )
            return _json(
                {
                    "granted": False,
                    "status": "pending",
                    "request_id": request.request_id,
                    "summary_to_show": request.summary_for_human(),
                    "next_step": (
                        "show summary_to_show to the user. A human approves with "
                        f"`applyops approve {request.request_id}`, which mints the "
                        "grant; then call submit_final with the returned grant id."
                    ),
                }
            )

    @server.tool()
    async def pending_submission_requests() -> str:
        """Requests waiting for a human decision, oldest first."""
        pending = runtime.authorizer.pending_requests()
        return _json(
            {
                "count": len(pending),
                "requests": [
                    {
                        "request_id": r.request_id,
                        "job_key": r.job_key,
                        "job_url": r.job_url,
                        "route": r.route,
                        "created_at": r.created_at,
                        "summary": r.summary_for_human(),
                    }
                    for r in pending
                ],
            }
        )

    @server.tool()
    async def submit_final(
        grant_id: str,
        job_url: str,
        job_id: str = "",
        route: str = "easy_apply",
        application_id: str = "",
        final_ref: str = "",
        final_name: str = "",
        evidence_text: str = "",
    ) -> str:
        """Perform the real final submit -- the only way one can happen.

        Requires a grant minted by a human approving a request. Nothing here
        takes a boolean promise: `submit_application`'s `acknowledged=True` was a
        sentence a model could write without asking anybody, and this replaces it
        with an authorization that was produced by someone else.

        Order enforced inside:

        1. the target is inspected and must really be the final submit;
        2. the form is snapshotted and compared against what was approved -- a
           changed form, a different resume or an updated profile voids the grant;
        3. the grant is spent inside its lock, so nothing else can spend it too;
        4. only then is anything clicked.

        Result is one of `verified`, `unverified` or `failed`. **Nothing retries.**
        An `unverified` submission may already be sitting in an employer's inbox;
        clicking again is how one application becomes two, so use
        `reconcile_submission` to find out what happened instead.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            try:
                resume = _current_resume(runtime)
            except ResumeError as exc:
                return _json({"submitted": False, "error": f"resume problem: {exc}"})

            action, detect_detail = await detect_final_action(
                browser, ref=final_ref, name=final_name
            )
            if action is None:
                return _json({"submitted": False, "error": detect_detail})

            # The ledger row *is* the application. Without it there is nothing to
            # claim, nothing to attach the outcome to, and no way to apply the
            # same rails the UI and the runner use.
            row = (
                runtime.service.get(application_id)
                if application_id
                else runtime.service.ledger.find_by_job_key(
                    job_id or extract_job_id(job_url) or job_url
                )
            )
            if row is None:
                return _json(
                    {
                        "submitted": False,
                        "error": (
                            "no application on record for this job. Call "
                            "enqueue_application first: submissions run through the "
                            "ledger so quota, dedupe and outcomes stay consistent "
                            "across every driver."
                        ),
                    }
                )

            try:
                outcome = await runtime.service.submit(
                    row.id,
                    grant_id=grant_id,
                    controller=browser,
                    resume=resume,
                    action=action,
                    route=route,
                )
            except SubmissionRefused as exc:
                return _json(
                    {
                        "submitted": False,
                        "refused": True,
                        "reason": exc.reason,
                        "manual_required": exc.manual_required,
                    }
                )
            except InvalidTransition as exc:
                return _json(
                    {
                        "submitted": False,
                        "refused": True,
                        "reason": f"application {row.id} is not submittable: {exc}",
                    }
                )

            return _json(
                {
                    "submitted": True,
                    "application_id": row.id,
                    **outcome.to_dict(),
                    "guard": runtime.guardrails.stats(),
                    "hint": (
                        ""
                        if outcome.verified
                        else (
                            "do not submit again. Call reconcile_submission to re-read "
                            "the page, or have the person check their inbox and the "
                            "employer's site directly."
                        )
                    ),
                }
            )

    @server.tool()
    async def reconcile_submission(
        application_id: str = "", evidence_text: str = "", final_ref: str = ""
    ) -> str:
        """Find out what happened to a possibly-submitted application.

        Read-only: it clicks nothing, submits nothing, and can only ever raise
        its own confidence. Reading an unchanged page leaves the result
        `unverified` and says so rather than upgrading a guess into a success.

        Pass `application_id`: reconciliation is only allowed to confirm an
        application when the evidence is provably about *that* application's
        attempt, which means the page on screen has to be the page the attempt
        was made from. Without it there is nothing to bind to, and the answer is
        refused rather than guessed.
        """
        if not application_id:
            return _json(
                {
                    "reconciled": False,
                    "error": (
                        "application_id is required: this page's evidence can only be "
                        "attributed to a specific application and attempt"
                    ),
                }
            )
        async with runtime.lock:
            browser = await runtime.get_browser()
            patterns = (evidence_text,) if evidence_text else success_patterns_for(
                browser.page.url
            )
            try:
                outcome = await runtime.service.reconcile(
                    application_id,
                    controller=browser,
                    action=FinalAction(ref=final_ref, success_patterns=patterns),
                    evidence_timeout=8.0,
                )
            except KeyError:
                return _json({"reconciled": False, "error": "unknown application"})
            payload = outcome.to_dict()
            payload["application_id"] = application_id
            if not outcome.verified:
                payload["next_step"] = (
                    "nothing was resubmitted. If the employer's site or the user's "
                    "inbox confirms it, that is news for the person to record; the "
                    "browser cannot prove it from here."
                )
            return _json(payload)

    @server.tool()
    async def enqueue_application(
        job_url: str,
        job_id: str = "",
        route: str = "",
        platform: str = "",
        title: str = "",
        company: str = "",
    ) -> str:
        """Accept a posting into the durable application queue.

        Idempotent per job id: the same posting reached twice is one application,
        not two. Nothing happens to it until `preflight` -> the M1 flow runs.
        """
        row = runtime.service.enqueue(
            job_url=job_url,
            job_id=job_id or extract_job_id(job_url),
            # `resolve_route` is the only place a route is decided; callers may
            # override it, but a missing route never becomes "demo" by accident.
            route=route or resolve_route(job_url, platform),
            platform=platform or platform_for_url(job_url),
            title=title,
            company=company,
        )
        return _json({"enqueued": True, "application": row.to_dict()})

    @server.tool()
    async def application_status(application_id: str) -> str:
        """One application's state, attempts and full event history."""
        try:
            return _json(runtime.service.status(application_id))
        except KeyError as exc:
            return _json({"error": str(exc)})

    @server.tool()
    async def list_applications(state: str = "") -> str:
        """Queued and past applications, optionally filtered by state.

        States: queued, preparing, waiting_for_input, waiting_for_approval,
        submitting, submitted_verified, submitted_unverified, failed,
        cancelled, skipped, legacy_imported.
        """
        rows = runtime.service.list(state or None)
        return _json({"count": len(rows), "applications": [r.to_dict() for r in rows]})

    @server.tool()
    async def browser_close() -> str:
        """Close the automation browser and release the profile."""
        async with runtime.lock:
            await runtime.shutdown()
            return _json({"closed": True})


def _example_profile_path():
    from ..profile import example_profile_path

    return example_profile_path()


# ── M1 helpers ───────────────────────────────────────────────────────
#
# Kept below `register` so they read as implementation detail rather than as
# surface: nothing here becomes a tool, and a tool must never be able to reach
# around the authorization path these helpers feed. Platform naming and final
# action detection live in `evidence.py`, shared with the service and the UI.


def _current_resume(runtime: Runtime) -> ResumeRef:
    """The one resume. Raises `ResumeError` rather than inventing a default."""
    configured = runtime.memory.profile.value("resume_path")
    return resolve_resume(configured)


def _compare_resume(runtime: Runtime, attached: str) -> dict:
    """Is the file about to be attached the resume actually configured?"""
    try:
        expected = _current_resume(runtime)
    except ResumeError as exc:
        return {"match": False, "mismatch": True, "detail": str(exc)}
    try:
        actual = resolve_resume(attached)
    except ResumeError as exc:
        return {
            "match": False,
            "mismatch": True,
            "detail": f"attached file could not be verified: {exc}",
        }
    if actual.sha256 == expected.sha256:
        return {"match": True, "mismatch": False, "detail": ""}
    return {
        "match": False,
        "mismatch": True,
        "detail": (
            f"attached {actual.filename} is not the configured resume "
            f"{expected.filename}; confirm which one this employer should get"
        ),
    }


def _answers_revision(runtime: Runtime) -> str:
    """A changing marker for stored answers, so edits void stale approvals."""
    qa = runtime.memory.get_all_qa()
    return f"{len(qa)}:{max((getattr(q, 'updated_at', '') or '' for q in qa), default='')}"


def _profile_revision(runtime: Runtime) -> str:
    """A marker for the profile file's own content."""
    try:
        text = runtime.memory.profile.path.read_text(encoding="utf-8")
    except OSError:
        return "unreadable"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _record_application(
    runtime: Runtime,
    job_url: str,
    job_id: str,
    resume: ResumeRef,
    outcome,
    status: str,
) -> None:
    """Write the attempt to history, tagging the outcome honestly."""
    # `outcome` (verified/unverified/failed) is what statistics count. `status`
    # is only the coarse row label; succeeded there no longer implies anything
    # about whether the employer received anything.
    runtime.memory.add_application(
        job_url=job_url,
        job_id=job_id,
        platform=platform_for_url(job_url),
        apply_route=outcome.evidence.get("route", ""),
        status=status,
        outcome=outcome.status,
        grant_id=outcome.grant_id,
        resume_sha256=resume.sha256,
        notes=outcome.detail,
    )
