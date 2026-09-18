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
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import discovery
from ..browser import BrowserController
from ..memory import extract_job_id
from .runtime import BrowserBusy, Runtime, platform_for_url

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
            result = await browser.fill_field(ref, value)
            _record(browser, ref, role, result.ok)
            return _json(
                {
                    "ok": result.ok,
                    "readback": result.readback,
                    "mismatch": result.mismatch,
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
                    "strategy": result.strategy,
                    "selected": result.selected,
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
            ok = await browser.set_checkbox(ref, checked)
            _record(browser, ref, role, ok)
            return _json({"ok": ok, "checked": checked, "role": role, "reason": reason})

    @server.tool()
    async def upload_file(ref: str, file_path: str, role: str, reason: str) -> str:
        """Attach a local file to a file input (typically a resume)."""
        async with runtime.lock:
            browser = await runtime.get_browser()
            ok = await browser.upload_file(ref, file_path)
            _record(browser, ref, role, ok)
            return _json({"ok": ok, "path": file_path, "role": role, "reason": reason})

    @server.tool()
    async def click_target(name: str = "", ref: str = "", role: str = "", reason: str = "") -> str:
        """Click a button or link, by visible/accessible `name` or by `ref`.

        If the click opens a new tab, this follows it automatically and reports
        `new_tab: true` -- "Apply on company site" leads to the employer's own
        ATS in a new tab, and continuing to drive the old tab would lose the
        application entirely.
        """
        async with runtime.lock:
            browser = await runtime.get_browser()
            result = await browser.click(ref=ref, name=name)
            if role:
                _record(browser, ref or name, role, result.clicked)
            return _json(
                {
                    "clicked": result.clicked,
                    "new_tab": result.new_tab,
                    "active_url": result.active_url,
                    "platform": platform_for_url(result.active_url),
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
        """Request approval before submitting, and get a one-time token.

        `summary` must list the fields and the exact values that will be sent,
        and call out anything the tool filled by itself. If the user has not
        seen a value, they are approving something they cannot see.

        Show the returned `summary_to_show` to the user and get an explicit yes.
        Then call `submit_application` with `confirmation_id` and
        `acknowledged: true`. The token is single-use and expires, so a stale
        approval cannot be replayed onto a different job.
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
                "next_step": (
                    "show summary_to_show to the user; if they agree, call "
                    "submit_application with this confirmation_id and "
                    "acknowledged=true"
                ),
            }
        )

    @server.tool()
    async def submit_application(
        confirmation_id: str,
        job_url: str,
        job_id: str = "",
        job_title: str = "",
        company: str = "",
        platform: str = "",
        ats: str = "",
        apply_route: str = "easy_apply",
        answers_used: list[str] | None = None,
        steps: int = 0,
        vision_fallbacks: int = 0,
        acknowledged: bool = False,
    ) -> str:
        """Record a submitted application. Call this only after clicking Submit.

        Requires a `confirmation_id` from `request_submit_confirmation`. Without
        a valid, unused token this refuses -- the check is what makes "confirm
        before submitting" real rather than advisory.

        When the client could not prompt the user directly, pass
        `acknowledged: true` only after you have shown the user the summary and
        they explicitly approved.
        """
        # Check acknowledgement BEFORE spending the token. Consuming first and
        # then rejecting would burn the approval, forcing the caller to ask the
        # user all over again for a mistake that cost them nothing.
        if not acknowledged:
            pending = runtime.guardrails.peek_confirmation(confirmation_id)
            return _json(
                {
                    "recorded": False,
                    "error": (
                        "not acknowledged. Show the user the summary and call again "
                        "with acknowledged=true only after they agree."
                    ),
                    "summary_to_show": pending.summary if pending else "",
                    "token_still_valid": pending is not None and not pending.used,
                }
            )

        ok, message = runtime.guardrails.consume_confirmation(confirmation_id)
        if not ok:
            return _json({"recorded": False, "error": message})

        record = runtime.memory.add_application(
            job_url=job_url,
            job_id=job_id,
            job_title=job_title,
            company=company,
            platform=platform or platform_for_url(job_url),
            ats=ats,
            apply_route=apply_route,
            answers_used=answers_used or [],
            steps=steps,
            vision_fallbacks=vision_fallbacks,
        )
        runtime.guardrails.record_outcome(success=True)
        return _json({"recorded": True, "application": record.model_dump(),
                      "guard": runtime.guardrails.stats()})

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
    async def browser_close() -> str:
        """Close the automation browser and release the profile."""
        async with runtime.lock:
            await runtime.shutdown()
            return _json({"closed": True})


def _example_profile_path():
    from ..profile import example_profile_path

    return example_profile_path()
