"""stdio entry point for the ApplyOps MCP server.

Run with ``applyops-mcp`` (or ``python -m applyops.mcp.server``).

The `instructions` string below is the operating contract. MCP clients surface
it to the model automatically, which makes it the one place we can describe how
these tools are meant to be used without depending on any particular harness --
no SKILL.md, no per-client prompt file.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .runtime import RUNTIME
from .tools import register

INSTRUCTIONS = """\
ApplyOps drives a real browser to fill and submit job applications, and learns
from each one so the next is faster.

SETUP (do this first, once per person)

1. Call `setup_status()`. If `ready` is true, go straight to the workflow below.
2. If it is false, ask the user every question in `questionnaire` -- in ONE
   message, grouped as given, not one question per turn. Relay each question as
   written; they are phrased for a human, and some carry a `why` that matters
   (visa sponsorship, legal declarations).
3. Pass the answers to `save_profile`. Read `warnings` in the reply: anything
   listed there was rejected and must be re-asked, not assumed. Then call
   `setup_status()` again to confirm `ready`.
4. Fields the user does not want to answer stay blank. Do not fill them in
   yourself. The optional groups (`legal`, `demographics`) are skippable
   wholesale, and the demographic questions are voluntary by law -- "Prefer not
   to say" is a complete answer.

The profile is a markdown file the user owns and may edit by hand. If a value
looks stale, say so and ask; never silently correct it.

WORKFLOW FOR ONE APPLICATION

1. `preflight(job_url)` — always first. If `allowed` is false, stop; the reason
   explains why. This also enforces pacing, so a call may take a minute.
2. `route_guide(job_url)` — before the browser. Not every application is an
   Easy Apply; see ROUTES below.
3. `browser_open(job_url)`, then `browser_state()` to read the form.
4. For every question, call `get_answer(question)`:
     - "answered"   -> fill it, using the value verbatim
     - "suggestion" -> confirm with the user, then `record_answer`
     - "need_human" -> ask the user, then `record_answer`
   Batch the questions you have to ask into one message rather than one at a
   time; a Workday form can hold ten of them.
5. Fill fields with `fill_field` / `select_option` / `set_checkbox` /
   `upload_file`, passing the `ref` from `browser_state` plus a `role` and a
   `reason`. Check `ok` on every result — `mismatch: true` means the page did
   not accept the value and it must not be treated as filled.
6. Before submitting, call `request_submit_confirmation` with a summary listing
   every field and value. Show that summary to the user and get an explicit yes.
7. Submit in the browser, then call `submit_application` with the
   `confirmation_id`.
8. If the attempt fails, call `report_failure`. When it died at a recognisable
   step, pass `platform`, `route` and `blocked_at` so the failure is filed
   against the route instead of merely counted.

ROUTES

There is more than one way to apply, and `route_guide(job_url)` names the one
you are on:

- **easy_apply** — the form is a modal on the posting itself. Nothing navigates
  away and no account gate stands in front of it.
- **external_ats** — the posting hands the browser to the employer's own system
  (Amazon's passport, Workday, Greenhouse, Lever, …). The shape varies: Greenhouse
  and Lever are a single page with no account, while Amazon and Workday put a
  sign-in in front of the form, usually verified by an emailed one-time code.

A `human_required` step is not a speed bump, it is a wall. Stop at it and ask the
user — a one-time code, a captcha, an account password. Batch every such gate you
can foresee into the same message as the profile questions, rather than
discovering them one at a time with the form half-filled.

Read `route_guide` even when you expect an Easy Apply: it costs one call and
tells you how many screens the form has before you are in the middle of it.

REQUIRED

- Ask `get_answer` before asking the user anything. Asking a question the memory
  already knows is the one behaviour that makes this tool worthless.
- Pass `role` and `reason` on every form action. They are how the memory learns
  which reference works for which field.
- Follow the tab when `click_target` reports `new_tab: true`. "Apply on company
  site" opens the real application elsewhere — that is the `external_ats` route,
  not a failure.
- Re-read `browser_state()` after any action that changes the page.
- Report failures honestly with `report_failure`; do not silently retry.

FORBIDDEN

- Do not submit without a confirmation token. `submit_application` will refuse,
  and working around that refusal defeats the whole guardrail.
- Do not invent answers for salary, visa status, work authorization, years of
  experience, or skill self-ratings. Ask. A wrong answer here is worse than a
  slightly slower application.
- Do not bypass `preflight`. It is a rate limit, and the account being
  restricted is a much worse outcome than applying to fewer jobs today.

MEMORY

`flywheel_stats()` shows whether the memory is compounding. If
`selectors_suggested` stays at 0 after real runs, nothing is being recorded and
the memory is not learning. The same reply carries `routes`, reported
separately: `runs`, `success_rate`, and `hardest_gate` — the step that route most
often dies on, which is where the next adapter goes.

ONE BROWSER AT A TIME

Several processes share one `data/browser-profile`, because the logged-in session
is the one thing this project cannot rebuild for itself. Two Chromes on one
profile do not slow each other down, they delete each other's cookies. So the
profile has an exclusive lock and there is exactly one driver at a time.

If `browser_open` or `discover_jobs` replies with `browser_busy: true`, another
driver holds it — the scheduled loop, or a batch run — and the reply names it.
Say so and stop; do not retry in a loop, because the holder is normally in the
middle of an application. The daily cap, the application history and the learned
answers are all safe to write concurrently; the browser is not.

Everything else is crash-safe by construction: every state file is written
atomically and merged under a lock, so a killed run loses nothing and no process
has to ask whether another one is still alive.
"""


def build_server() -> MCPServer:
    server = MCPServer(
        name="applyops",
        title="ApplyOps Auto-Apply",
        version="0.2.0",
        instructions=INSTRUCTIONS,
    )
    register(server, RUNTIME)
    return server


def main() -> None:
    """Serve over stdio. This is the entry point harnesses launch."""
    build_server().run("stdio")


if __name__ == "__main__":
    main()
