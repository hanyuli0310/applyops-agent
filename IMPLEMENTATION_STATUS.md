# IMPLEMENTATION_STATUS.md

Execution record for `PLAN.md` (ApplyOps v0.2). Updated after each milestone, not
before it. Nothing here is aspirational: every line either names something that
was verified, or says explicitly that it was not.

Baseline for the whole run: `hanyuli0310/applyops-agent`, `main`, commit
`7f4d505c72b7dc463a43d189e36640924e03fedd`.

Working tree note: at the start of M1, `AGENTS.md` already carried an
uncommitted change (a target-titles section added by another session). It is
**deliberately left uncommitted and untouched**; it is not part of this work.

---

## M1 — Trusted Execution

**Status: COMPLETE. All M1 tests pass. Nothing was submitted to a real employer.**

- Starting commit: `7f4d505c72b7dc463a43d189e36640924e03fedd`
- Ending commit: see below ("M1 commit")
- Branch: `feat/applyops-v02` (local only; not pushed)

### What changed, and why

| change | file(s) | why the old behaviour was unsafe |
|---|---|---|
| Three-valued verification for every form action | new `src/applyops/verification.py`; `browser.py` | `bool(readback) and ...` treated "could not read it" as agreement, so a control that rejected the value scored `ok=True` |
| `fill_field` / `select_option` / `set_checkbox` / `upload_file` now read back and report `verified` \| `mismatch` \| `unverifiable` | `browser.py` | writing to a page is not evidence the page accepted the write |
| New page reads: `field_snapshot()`, `read_attachments()`, `inspect_target()`, `page_indicates()` | `browser.py` | nothing could previously state what the form actually held at any instant |
| Click classification by structure **and** name | new `src/applyops/action_policy.py` | gating only the word "Submit" misses `<button>` (default type submits its form) and any JS-driven final action |
| Generic `click_target` refuses any final-submit target | `mcp/tools.py` | the previous gate was the post-hoc token; the real external action was ungated |
| Request / grant split | new `src/applyops/authorization.py`, new `src/applyops/approve.py` | the caller that wanted permission could grant itself permission (`acknowledged=True`) |
| Single submission path | new `src/applyops/submission.py` | there were two: `click_target` doing the real thing, and `submit_application` doing the bookkeeping |
| Grant bound to job + snapshot + resume digest + fact revisions + expiry, single-use, locked | `authorization.py` | the old token bound only a job key and a free-text summary |
| Resume has one source of truth | new `src/applyops/resume.py`; `tools/auto_apply.py` | `auto_apply.py` hard-coded `data/resume.pdf` independently of `profile.md` |
| `verified` / `unverified` / `failed` recorded separately; only `verified` counts | `memory.py`, `guardrails.py`, `mcp/tools.py` | anything not explicitly failed counted as success, both in history rows and in platform/route statistics |
| Local demo ATS | new `src/applyops/demo_ats.py` | safety claims could not be exercised at all without a target |

### Schema changes

- `ApplicationRecord.outcome` (default `"unverified"`), `.grant_id`, `.resume_sha256`.
  Old rows load fine and are treated as `unverified` — **they are not upgraded to
  success**, which is exactly what `PLAN.md` §4.2 forbids.
- New state files under the data root: `submission_requests.json`,
  `submission_grants.json`. Both are additive; deleting them loses nothing else.
- No migration existed to write and none was needed: nothing was moved or renamed.

### Tests executed (exact commands)

```bash
.venv/bin/python -m pytest tests/ -q                      # 62 passed in 92.8s
.venv/bin/python -m pytest tests/test_m1_trusted_execution.py -q   # 40 passed in 72.6s
.venv/bin/ruff check src tools tests                      # 139 errors, all pre-existing or fewer
```

Baseline was **22 passed** and **140 lint errors**; the diff repaired two lint
errors in `browser.py` and added none.

The suite was additionally **proven to fail** against the old semantics: with
`fill_field` temporarily reverted to `mismatch = bool(readback) and ...`,
`test_fill_verifies_against_the_page` went red and was restored to green when the
fix was put back. The screenshot repro that started this investigation
(`/tmp/applyops_readback_repro.py`) reproduced it independently in a real browser.

### Outcome of each M1 requirement

| # | requirement | result |
|---|---|---|
| 1 | explicit verification semantics for form actions | ✅ three verdicts, fail-closed, unit-tested |
| 2 | expected non-empty + empty/unreadable readback is not success | ✅ regression test, proven red under old code |
| 3 | final external submit behind the authorization boundary | ✅ only `submission.execute_authorized_submission` clicks a final target |
| 4 | approval binds job + snapshot + resume + answers + expiry | ✅ digest over all five; each tested separately |
| 5 | no generic-click bypass | ✅ structural + name classification; `Save` inside a form is caught |
| 6 | verified / unverified / failed strictly separated | ✅ new `outcome` field; statistics count only `verified` |
| 7 | `SUBMITTED_UNVERIFIED` is never retried | ✅ no retry exists anywhere in the path; reconciliation is read-only |
| 8 | crash-after-submit enters reconciliation | ✅ a click that never settles returns `unverified` with `reconciliation_required` |
| 9 | resume has a single source of truth | ✅ `resume.py` only; hard-coded path deleted from `auto_apply.py` |
| 10 | stale/preselected resume not trusted as correct | ✅ `attachment_state` + `verify_upload`; demo scenario `stale` covered |

### Known limitations (deliberate, to be closed later)

1. **Same-OS-user is not a strong boundary.** Anyone who can write to the data
   directory can mint a grant. This protects against the *accidental* and
   *model-generated* authorization, which is what actually happens in practice;
   it is not protection against a malicious process running as this user.
   `PLAN.md` §5.4 requires this to be stated rather than glossed over.
2. **Over MCP, "the user said yes" still arrives as text.** The boundary is that
   the *grant* cannot be created by that same text — it must come from
   `applyops approve`. A harness that fabricates the whole story, including a
   human sitting at the terminal, is out of scope of any local system. M3 moves
   approval into the local UI.
3. **Route-specific success vocabulary is tiny.** Only `DemoATS` and a few
   phrases for LinkedIn/Greenhouse/Lever/Workday. An unknown route has empty
   patterns, so its result can only ever be `unverified` — deliberately.
4. **`tools/auto_apply.py` now waits for a human** and times out politely
   (`GRANT_WAIT_SECONDS = 900`). Unattended automation is deliberately not
   authorized yet; that is M4 (limited auto mode).
5. Demo ATS does not parse multipart uploads — it proves what the *browser input*
   reports, which is the layer under test, not server-side persistence.

### Assumptions

- `APPLYOPS_DATA_DIR` may point elsewhere; default remains `<repo>/data`.
- Real employers were never contacted. All browser tests bind `127.0.0.1`.
- The user's real `data/` was **not read, modified, or committed**.

### Next milestone

**M2 — Unified Core**: one `ApplicationService`, SQLite ledger, explicit
application state machine (`QUEUED … SUBMITTED_VERIFIED / SUBMITTED_UNVERIFIED /
FAILED / CANCELLED`), invalid-transition rejection, crash recovery tests, and
migration with backup/rollback.

---

## M2 — Unified Core

**Status: COMPLETE. All tests pass. No real submissions.**

- Starting commit: `ce0a5da` (M1)
- Branch: `feat/applyops-v02` (local only; not pushed)

### What changed, and why

| change | file(s) | why |
|---|---|---|
| Explicit application state machine | new `src/applyops/state_machine.py` | states were free-form strings; nothing refused an impossible move |
| Durable SQLite ledger: applications, attempts, append-only events | new `src/applyops/ledger.py` (WAL, `PRAGMA user_version`) | two entry points could not share state; JSON merge cannot do compare-and-set |
| Claims with expiry | `ledger.py` | two entry points must not execute one application; a crashed claim expires instead of wedging the row |
| Crash recovery | `ledger.recover_expired()` | a process dying inside `SUBMITTING` lands in `SUBMITTED_UNVERIFIED` — never back at "ready to submit again" |
| Unified `ApplicationService` | new `src/applyops/service.py` | one lifecycle for MCP, batch runner and (later) the UI, instead of three |
| Attempt rows | `ledger.py` | a retry appends; it never overwrites the failed attempt |
| Legacy migration with backup | `service.import_legacy_history()` | old `memory.json` history -> `LEGACY_IMPORTED` (terminal), idempotent, source backed up first |
| Schema downgrade refused | `ledger.migrate()` | newer data must not be silently mangled by older code |
| MCP queue surface | `runtime.py` (lazy `service`), `tools.py` (`enqueue_application`, `application_status`, `list_applications`) | M3/M4 talk to the core instead of reimplementing semantics |
| Platform naming extracted | new `src/applyops/platforms/naming.py`, shared `src/applyops/evidence.py` | final-action detection + success vocabulary must be identical for every driver |

### State machine (enforced, not documented)

`QUEUED -> PREPARING -> WAITING_FOR_INPUT | WAITING_FOR_APPROVAL -> SUBMITTING ->
SUBMITTED_VERIFIED | SUBMITTED_UNVERIFIED | FAILED`; `FAILED -> PREPARING` (retry
= new attempt); `SUBMITTED_UNVERIFIED -> SUBMITTED_VERIFIED` (reconciliation
evidence only); `SUBMITTED_UNVERIFIED -/-> SUBMITTING` (no resubmission, by
construction); `LEGACY_IMPORTED` terminal.

### Schema

`applications` (state, claim_owner, claim_expires_at, …, UNIQUE job_key),
`attempts` (UNIQUE (application_id, ordinal)), `events` (append-only),
`PRAGMA user_version = 1`. New file `app.sqlite` in the data root; created lazily
on first use, nothing existing is migrated or rewritten.

### Tests executed

```bash
.venv/bin/python -m pytest tests/ -q        # 85 passed in 94.9s (62 pre-M2 + 23 M2)
.venv/bin/ruff check src tools tests        # no new errors; new modules lint-clean
```

Covered: illegal transitions (incl. `SUBMITTING -> WAITING_FOR_APPROVAL` and
`SUBMITTED_UNVERIFIED -> SUBMITTING`), restart durability, idempotent enqueue,
stale-writer rejection, 2-thread claim race (exactly one winner), attempt
accumulation, unknown outcome refused, expired-claim recovery, schema-downgrade
refusal, legacy import (never verified / idempotent / backup / failure keeps
source untouched), service E2E on the demo ATS (prepare -> input -> approval ->
submit -> verified), wrong-state submit writes no attempt, flywheel mirror counts
only verified.

### Known limitations

1. Cross-process double-execution is prevented by claims + optimistic
   transitions; a hostile process that bypasses the ledger entirely is out of
   scope (same-OS-user boundary, as in M1).
2. `service.prepare(ready=...)` trusts its caller for readiness; the UI/runner
   wiring in M3/M4 must derive it from actual snapshot/profile checks.
3. Migration imports only rows with a usable `job_id`/`job_url`; rows without
   either are counted as `skipped` and left in the source file.
4. The scheduled runner (`cron_apply.py`) still drives the old flow; it moves to
   the service in M4.

### Next milestone

**M3 — Local Web UI**: FastAPI service backend over `ApplicationService`,
React/TypeScript/Vite frontend, four pages, real API only (no mock data).

---

## M3 — Local Web UI

**Status: COMPLETE. All tests pass, including a real-browser walk of all four
pages against the built frontend. No mock data anywhere.**

- Starting commit: `0663b10` (M2)
- Branch: `feat/applyops-v02` (local only; not pushed)

### What was built

| piece | file(s) |
|---|---|
| FastAPI backend over the unified core (no duplicated semantics) | `src/applyops/api/app.py` |
| React + TypeScript + Vite frontend, four pages | `frontend/` (`Profile` `Jobs` `Attention` `Applications`) |
| Frontend production build served by the backend | `frontend/dist`, mounted at `/` |

Pages map 1:1 to `PLAN.md` §3.2: 资料与简历 (profile editor, single-resume upload
with sha256 shown), 岗位与偏好 (paste URL / one-click local demo job, queue
preview with states), 待我处理 (approval requests with the full value summary,
read-only reconcile for unknown results), 运行与记录 (all states, attempts,
event history, JSON export).

### The boundary in UI form

`POST /api/requests/{id}/approve` is the only place a grant is born, and the API
process binds loopback only (`serve()` refuses any other host). The requesting
side -- the prepare route -- can only file a request. This is the M3 answer to
`PLAN.md` §5.3's "approval entry is a local user UI".

### Tests executed

```bash
.venv/bin/python -m pytest tests/ -q        # 94 passed in 111.7s (85 + 9 M3)
npm run build                               # tsc + vite, clean
```

API tests: honest status, profile round-trip, unknown-field rejection (422),
resume upload becomes the one configured resume (content-addressed), unsupported
type 415, demo flow through approval (second approve 409), submit with a
fabricated grant 403, loopback-only serve.

Browser E2E (`test_four_pages_end_to_end_in_a_real_browser`): drives the built
frontend with Playwright — profile page renders the seeded resume, one-click
demo enqueue, prepare, read the approval summary, approve, submit, and sees
「已确认提交成功」. The same flow was verified at the API level to end
`SUBMITTED_VERIFIED` with `matched_text: "Application received"`.

### Known limitations

1. Approval is any local program; no login/session on the loopback API yet (M5
   adds Host/Origin hardening; the process is not reachable off-machine).
2. Jobs & Preferences page covers enqueue + queue states; preference rules
   (titles/locations/exclusions) arrive with the supervised runner in M4, where
   the matcher exists.
3. The UI polls every 5 s rather than SSE; acceptable for one local user.
4. Lint caught `FinalAction` missing from the reconcile route (would 500 on
   first use) — fixed and covered by the route now importing from the core.

---

## M4 — Supervised Automation

**Status: COMPLETE. All tests pass. The runner still cannot approve anything by
itself.**

- Starting commit: `5ea60df` (M3)
- Branch: `feat/applyops-v02` (local only; not pushed)

### What was built

| piece | file(s) | why |
|---|---|---|
| Scoped answers (GLOBAL / COMPANY / APPLICATION) | new `src/applyops/answers.py` | the flywheel answered everything globally; sponsorship and salary questions must not cross employers |
| Resolution is most-specific-first, no fuzzy matching | `answers.resolve()` | similar question text is not the same semantics (`PLAN.md` §6.3) |
| Withdrawal bumps a revision that is part of every grant digest | `answers` + `authorization` | withdrawing an answer must void pending approvals immediately |
| Queue runner | new `src/applyops/runner.py` | supervised passes: recover -> reconcile -> prepare -> submit only pre-approved work |
| `AutoPolicy` + `PolicyStore` | `runner.py`, `data/auto_policy.json` | limited auto mode is explicit opt-in with max applications, platform allowlist, expiry; **disabled by default** |
| Pause / resume / stop | `runner` | checked between applications; never interrupts an in-flight submission |

### What limited auto mode means here (and what it does not)

A pass with the policy enabled may prepare applications and spend **already
approved** grants, up to `max_applications`, only on `allowed_platforms`, only
before `expires_at_epoch`. It still cannot mint a grant: every submission was
individually approved by a human via CLI or UI. The default policy is disabled,
and a pass with the default policy submits nothing.

### Tests executed

Run per-module (see note below):

```bash
.venv/bin/python -m pytest tests/test_core.py tests/test_concurrency.py -q   # 22 passed
.venv/bin/python -m pytest tests/test_m1_trusted_execution.py -q             # 40 passed
.venv/bin/python -m pytest tests/test_m2_unified_core.py -q                  # 23 passed
.venv/bin/python -m pytest tests/test_m3_local_ui.py -q                      # 9 passed
.venv/bin/python -m pytest tests/test_m4_supervised_automation.py -q         # 13 passed
```

107 total, 0 failures. M4 covers: scope precedence, application-scope isolation,
no fuzzy inheritance, scope context required, withdrawal revision + voided
grant, policy defaults/expiry, disabled policy prepares-but-never-submits,
missing resume parks with reason, policy budget of 1 spends exactly 1 of 2
pre-approved grants, platform allowlist refuses (and does not even prepare),
pause/stop honoured, reconciliation pass never submits.

**Environment note:** running the whole suite in one pytest process is killed by
the sandbox (exit 137, memory pressure from many headless Chromes). Per-module
runs are green and stable; a CI matrix should shard the browser tests.

### Known limitations

1. The runner fills nothing itself beyond what M1's flow already does; field
   filling for real employers remains the harness's job (this is the "hands +
   brain" split, unchanged).
2. `pending_questions` batching across applications is served by the scoped
   answer store; a dedicated "answer once, resume affected" sweep lands with the
   UI's needs-attention page, which already shows waiting_for_input rows.
3. `cron_apply.py` still runs the old flow. It keeps working through the same
   guardrails, but moving it onto `QueueRunner` is deferred (its unattended
   cadence is exactly what limited auto mode gates).

---

## M5 — Productization

**Status: COMPLETE. All tests pass.**

- Starting commit: `dc8db93` (M4)
- Ending commit: this one

### What was built

| piece | file(s) |
|---|---|
| Unified CLI: `applyops doctor / serve / demo / stop / version` | new `src/applyops/main.py`, registered as the `applyops` console script |
| Doctor with actionable fixes | `main.py` — OS, Python, Chrome, data dir writability, browser lock, profile, resume, frontend build |
| Stop is pid-file based and only stops its own console | `main.py` |
| Version 0.2.0 | `pyproject.toml`, `main.APP_VERSION` |
| Final report | `FINAL_REPORT.md` |

### Tests executed

```bash
.venv/bin/python -m pytest tests/test_m5_productization.py -q   # 9 passed
# full suite re-verified per module: 116 passed, 0 failed
ruff check src tools tests   # 135 (baseline 140; new code lint-clean)
```

M5 covers: doctor on a fresh machine (exit 1, gaps + fixes named), doctor green
after setup, doctor never writes, CLI version/help, loopback refusal, stop
no-op + stale pid cleanup, console script registered, and the clean-machine
onboarding walk: doctor(1) → complete profile + resume via API → doctor(0) →
demo → prepare → approve → submit → verified → history.

### Known limitations

1. The local API has no session auth yet (loopback only); Host/Origin
   hardening is the first beta item.
2. `uv sync` must be re-run with `--extra dev` for dev tools (documented).

### Status: ALL FIVE MILESTONES COMPLETE

Final verification, known limitations, unsupported routes and beta checklist
are consolidated in `FINAL_REPORT.md`.

---

## Acceptance review fixes (post-M5)

**Status: COMPLETE. All suites pass. Not committed, not pushed -- left in the
working tree for review.**

Nine reported problems, each fixed and each covered by a regression test in
`tests/test_acceptance_fixes.py` (25 tests). Every one of these was a real
defect in shipped code, confirmed by reading the failure, not by inspection.

| # | problem | fix | tests |
|---|---|---|---|
| 1 | `request_submission_grant` called `create_request(source=...)`; the parameter is `requested_by` -- the MCP flow raised TypeError and could never run | parameter corrected; `application_id` + `page_url` now passed; `applyops approve <request_id>` added as a real subcommand of the console script | `test_mcp_request_grant_is_callable_and_the_approve_channel_exists`, `test_approve_refuses_to_decide_non_interactively` |
| 2 | Web `prepare` read an *empty* form and asked approval for it; the demo ATS accepted anything | new `filling.py`: fill from profile -> scoped answers -> verify each read-back -> attach resume -> verify attachment; unresolved/unreadable/mismatched fields park the application in `waiting_for_input`; demo ATS parses the multipart body and validates required fields + resume | `test_prepare_fills_the_form_and_the_ats_receives_jane_doe`, `test_demo_ats_rejects_an_empty_or_partial_application` |
| 3 | The UI took "the first grant it found in localStorage" | `frontend/src/grants.ts` keys grants by application id; API returns `application_id` on approve; three consecutive applications each use their own grant | `test_three_applications_each_submit_with_their_own_grant`, `test_wrong_grant_for_the_wrong_application_is_refused`, `test_expired_and_used_grants_are_both_refused`, frontend `grants.test.ts` (7) |
| 4 | Submitting compared field snapshots only; two postings on one ATS render identical forms | `page_identity_of()` (host+path+non-tracking query, tracking params dropped) recorded at approval; checked again at submit; `application_id` non-transferable | `test_grant_refuses_when_the_browser_sits_on_another_posting`, `test_submit_refuses_when_the_browser_moved_to_another_application`, `test_page_identity_ignores_noise_and_keeps_the_posting` |
| 5 | Web/MCP/runner had three submission paths; `submit_application(outcome="verified")` let a caller type a success | MCP `submit_final` goes through `ApplicationService`; `submit_application` is now a read-only view of the ledger attempt; `service.submit` runs preflight, claims, and records rails + history | `test_submit_application_reports_evidence_and_cannot_invent_a_verdict`, `test_service_applies_the_rails_for_every_driver` |
| 6 | The FastAPI browser owner ignored the cross-process profile lock | `AppState` takes `FileLock(browser_lock_path(...))`, refuses with the holder's name, releases on close (`/api/browser/release`) | `test_the_console_profile_lock_is_held_across_processes` (child process) |
| 7 | `profile_revision` / `answers_revision` were empty strings from the UI and runner, so "the facts changed" was recorded as "nothing changed" | one definition in `ApplicationService.revisions()` (profile hash + scoped answers revision + flywheel), used by MCP, UI and runner | `test_revisions_are_real_values_and_profile_changes_void_a_grant`, `test_a_new_scoped_answer_voids_a_pending_grant` |
| 8 | M4 capabilities existed but were not reachable from the UI | `waiting_for_input` names the missing fields, answers can be given per application and prepare re-run; runner pause/resume/stop/policy/pass endpoints + UI; Jobs & Preferences sets titles/locations/include/exclude and previews why a posting is kept or filtered | `test_waiting_for_input_names_what_is_missing_and_can_be_resumed`, `test_runner_controls_and_policy_reach_the_real_runner`, `test_preferences_explain_why_a_posting_is_kept_or_filtered` |
| 9 | Users had to `npm install && npm run build`; `stop` trusted a pid file on macOS; the local API had no session/origin protection | wheel ships `applyops/web` (verified in the built artifact); `stop` proves ownership by asking the recorded port to identify itself, and never signals a recycled pid; Host/Origin/session-token middleware, token injected into the served page | `test_frontend_build_resolution_prefers_a_packaged_console`, `test_stop_does_not_signal_a_pid_that_is_not_our_console`, `test_stop_signals_a_console_that_identifies_itself`, `test_local_api_requires_the_session_token_for_state_changes`, `test_local_api_refuses_a_foreign_host_or_origin` |

### Bugs found *while* fixing the reported ones (not in the list)

1. **`label=No` resolved to the "Notice period" field.** `resolve_ref` used
   substring label matching, so the sponsorship radio and the notice select
   collided. Now exact-first, substring as fallback.
2. **The sponsorship radio was silently unselected.** The filler passed the
   option's *text* ("No") where a boolean was expected, and falsy meant
   "uncheck" -- so the form shipped with its most consequential question blank.
3. **The page token was unreadable.** The server replaced `__APPLYOPS_TOKEN__`
   including the property *name*, so `window.__APPLYOPS_TOKEN__` was `None` and
   every state change from the UI was refused (403).
4. **`demo_ats.py` used `json` without importing it** (the `/-/last-submission`
   endpoint would have crashed on first use).
5. **Repeated demo clicks deduped into one application** (same job id), so a
   three-application test could not exist.
6. **The runner submitted from whichever page the previous iteration left
   loaded** -- now it returns to each application's own page and restores the
   form, which is what its grant is bound to.

### Test results (each run separately; see the note about sandbox memory)

| suite | result |
|---|---|
| `tests/test_core.py` | 10 passed |
| `tests/test_concurrency.py` | 12 passed |
| `tests/test_m1_trusted_execution.py` | 40 passed |
| `tests/test_m2_unified_core.py` | 23 passed |
| `tests/test_m3_local_ui.py` | 9 passed (incl. the fill -> answer -> approve -> submit browser walk) |
| `tests/test_m4_supervised_automation.py` | 13 passed |
| `tests/test_m5_productization.py` | 9 passed |
| `tests/test_acceptance_fixes.py` | 25 passed |
| **total** | **141 passed, 0 failed** |
| frontend `npx vitest run` | 7 passed |
| frontend `npm run build` / `tsc --noEmit` | clean |
| `uv build --wheel` | console shipped at `applyops/web` |
| `ruff check src tools tests` | 138 findings, all pre-existing debt (pre-review baseline 143) |

Three behaviours the earlier milestones *asserted* had to change with the
product, and the tests were updated to assert the new truth rather than
weakened: the demo ATS now rejects incomplete applications (M1/M2/M4 tests
submit complete forms), submitting requires being on the approved page (the
three-application test interleaves prepare/approve/submit), and `stop` cleans up
stale pid files instead of assuming ownership.

---

## Blocking fixes (round 2)

Baseline `40c91b2`. Four reported blockers, each with a failing test written
first, then a minimal fix, then a small local commit. Nothing was pushed or
merged; `AGENTS.md`'s uncommitted third-party change was left alone.

| blocker | commit | fix | tests |
|---|---|---|---|
| 1. MCP flow could not reach a submission; routes disagreed | `505f778` | new public tool `prepare_application`; the prepare work moved into one implementation (`applyops.prepare`) used by MCP, the console and the runner; `platforms.naming.resolve_route` is the single route vocabulary and an unknown posting is `external`, never `demo`; `request_submission_grant` reads job/route/platform from the ledger row | `tests/test_blocker_mcp_flow.py` (3) |
| 2. Yes/No options answered each other | `defcb37` | the page-wide "contains sponsor" heuristic is gone; the locator exposes each control's *question* (fieldset legend / ARIA group) and its form `name`; radio refs are `[name][value]` and the dedupe key includes the group, so three Yes/No groups on one page are three controls; filling resolves per question and parks an unanswered one by name | `tests/test_blocker_choice_groups.py` (3) |
| 3. reconcile confirmed applications with another posting's evidence; failures guessed | `8ed024d` | reconcile takes the expected page identity and only counts evidence on the page the attempt was made from; MCP reconcile goes through the service and requires an application id; evidence-read failure after the click is unverified, an exception after the grant was spent is unverified, only a pre-consume exception is FAILED, and a bookkeeping failure keeps the real outcome | `tests/test_blocker_reconcile_binding.py` (6) |
| 4. drivers shared one page; the interval was ignored outside MCP | `32b767f` | every page-touching console route takes `AppState.page_lock` (asserted structurally for both the console and MCP); `service.submit` waits `preflight.wait_seconds` (bounded) and **re-checks the rails afterwards**, refusing before the claim so nothing is sent and no approval is burned | `tests/test_blocker_concurrency.py` (7) |

### Found while fixing these

1. `label=No` resolved to the "Notice period" field, and the three radio groups
   of the new screening page collapsed into one at collection time — the
   reported cross-answering started *before* the filler ran.
2. The demo ATS never counted submissions, so "the request was actually sent"
   could not be asserted; the first version of the unknown-result test passed for
   the wrong reason (a browser-blocked form that was never posted).
3. The acceptance test titled "three applications, three grants" was asserting
   three immediate submissions — i.e. exactly the missing interval this round
   was about. It now sets the gap to zero for its own purpose, and the interval
   has its own tests.

### Behaviour changes to be aware of

- A submission that arrives too early now **waits** out the rails' interval
  (bounded by `MAX_INLINE_WAIT_SECONDS`, 60s) and re-checks; if more time is
  still required it is refused *before* the claim. Previously the console
  ignored the interval entirely.
- `resolve_route` labels anything that is not the local demo ATS or LinkedIn as
  `external`; those applications can be read and parked but not driven.

### Test results

| suite | result | exit |
|---|---|---|
| `tests/test_core.py` | 10 passed | 0 |
| `tests/test_concurrency.py` | 12 passed | 0 |
| `tests/test_m1_trusted_execution.py` | 40 passed | 0 |
| `tests/test_m2_unified_core.py` | 23 passed | 0 |
| `tests/test_m3_local_ui.py` | 9 passed | 0 |
| `tests/test_m4_supervised_automation.py` | 13 passed | 0 |
| `tests/test_m5_productization.py` | 9 passed | 0 |
| `tests/test_acceptance_fixes.py` | 25 passed | 0 |
| `tests/test_blocker_mcp_flow.py` | 3 passed | 0 |
| `tests/test_blocker_choice_groups.py` | 3 passed | 0 |
| `tests/test_blocker_reconcile_binding.py` | 6 passed | 0 |
| `tests/test_blocker_concurrency.py` | 7 passed (run in batches: 2 structural + 5, and 1 + 1 for the interval pair) | 0 per batch |
| frontend `npx vitest run` | 7 passed | 0 |
| frontend `npm run build` / `tsc --noEmit` | clean | 0 |
| `ruff check src tools tests` | 134 findings, all pre-existing debt (baseline 138) | 1 |

Not run as a single process: `tests/test_blocker_concurrency.py` in one go, and
the whole suite in one go. Both are killed by this sandbox's memory cap (exit
137) once enough headless Chromes accumulate. Every suite above was run to
completion in its own process, and the two batches cover the file's 7 tests.

### Remaining blockers

1. There is still no user authentication on the loopback API (Host/Origin/token
   only). Single-user machine, so nobody to authenticate against — first beta item.
2. `cron_apply.py` / `auto_apply.py` remain on the legacy flow; the runner has
   not replaced them.
3. Only the local demo ATS and LinkedIn Easy Apply have a submission path.
   Everything else is `external`: read, prepare, park.
4. The field label map is still fixed plus the sponsorship rule; an ATS with
   unusual labels parks for a human (intended) but needs per-route knowledge to
   be pleasant.
5. The console still polls every 5 seconds (no SSE).

---

## Final blocker fixes (round 3)

Baseline `8c2ee5b`. Two blockers, tests written first, minimal fixes, one local
commit. `AGENTS.md`'s uncommitted change was left untouched.

### Fix 1 — the MCP answer path now writes what the filler reads

`record_answer` wrote only to the legacy learning flywheel (`runtime.memory`)
while `fill_application_form` resolves from the unified `AnswerStore`. The public
tool therefore reported `stored: true` for an answer the filler could not see,
and `prepare_application` reported the same question as missing forever.

- `record_answer(question, answer, context, scope, company, application_id)` now
  writes to `service.answers` — the store the filler reads — and *also* copies to
  the flywheel for learning/statistics. A flywheel failure cannot turn a
  successful store write into a report of failure, and a store failure is never
  reported as success.
- Scope defaults to the narrowest thing the caller named: `application_id` ->
  `application`, `company` -> `company`, otherwise `global`. An application-scoped
  answer without an id, or a company-scoped one without a company, is refused with
  an explanation instead of being silently downgraded.
- `get_answer` reads the unified store first and reports `source: answers:<scope>`,
  so "saved" and "the filler will use it" cannot disagree.
- `prepare_application`'s `next_step` now names the tool, the scope and the
  application id to pass.
- **Also fixed, found by the new E2E:** `submit_final(route=...)` defaulted to
  `easy_apply`, which overrode the ledger row's own route (`demo`) and made the
  approval digest mismatch — every non-LinkedIn MCP submission failed with "the
  form no longer matches what was approved". The default is now the recorded
  route.

### Fix 2 — an exception after the final click is UNVERIFIED, not FAILED

`_click_final` ran resolve, click, wait and tab adoption in one `try`, then
decided whether the click had happened by searching the exception message for
"Timeout" or "navigat". Every other post-click failure — a closed page, a tab
that could not be adopted, a browser that went away — came back as
`clicked=False, sent=False`, which lands as FAILED: the one state a retry may
start from.

- Three explicit phases, decided by control flow: `pre_click` (control never
  found — nothing sent), `click_attempted` (`click()` raised; it may still have
  dispatched), `post_click` (the click returned; observing the result failed).
- From `click_attempted` onward the result is always "the click was attempted"
  and the outcome is UNVERIFIED with `sent_possible`, `reconciliation_required`
  and no automatic retry. Only `pre_click` can produce FAILED / `sent: false`.

### New tests

| file | tests | covers |
|---|---|---|
| `tests/test_blocker_mcp_answers.py` | 4 | the reported MCP answer loop end to end through public tools only (enqueue → prepare → answer → prepare → approve → submit → status → ATS received the fields and the resume); `get_answer` reports what the filler reads; application-scoped answers do not leak; global answers are visible everywhere and a company-scoped one without a company is refused |
| `tests/test_blocker_click_phases.py` | 6 | pre-click failure → no click and FAILED/unsent; a raising `click()` still counts as attempted; post-click failure is unknown; a page that dies after the click lands SUBMITTED_UNVERIFIED and cannot be resubmitted (ATS POST count stays 1); the verified happy path still works |

### Test results

| suite | result | exit |
|---|---|---|
| `tests/test_blocker_mcp_answers.py` | 4 passed | 0 |
| `tests/test_blocker_click_phases.py` | 6 passed | 0 |
| `tests/test_blocker_mcp_flow.py` | 3 passed | 0 |
| `tests/test_blocker_choice_groups.py` | 3 passed | 0 |
| `tests/test_blocker_reconcile_binding.py` | 6 passed | 0 |
| `tests/test_blocker_concurrency.py` | 7 passed (batches: 5 + 1 + 1) | 0 |
| `tests/test_m1_trusted_execution.py` | 40 passed | 0 |
| `tests/test_m2_unified_core.py` | 23 passed | 0 |
| `tests/test_m3_local_ui.py` | 9 passed | 0 |
| `tests/test_m4_supervised_automation.py` | 13 passed | 0 |
| `tests/test_m5_productization.py` | 9 passed | 0 |
| `tests/test_core.py` + `tests/test_concurrency.py` | 22 passed | 0 |
| `tests/test_acceptance_fixes.py` | 25 passed | 0 |
| frontend `npx vitest run` / `npm run build` / `tsc --noEmit` | 7 passed / clean / clean | 0 |
| `ruff check src tools tests` | 134 findings, all pre-existing debt (same as baseline) | 1 |

Not run as one process: `tests/test_blocker_concurrency.py` (three batches) and
the whole suite at once — the sandbox's memory cap kills both (exit 137) once
enough headless Chromes accumulate.

### Blockers that would stop basic local / MCP demo use

None known. The MCP demo flow now runs end to end through public tools only
(enqueue → prepare → answer → prepare → approve → submit → status), and the two
stores that used to disagree are one.

---

## Wiring the documented intent, and a per-pass budget (round 4)

Two additions on top of `8ca434b`, both asked for directly. `AGENTS.md`'s
uncommitted change was read (never modified) and its definition brought into the
system.

### The target-title pool is now part of the system

`AGENTS.md` §12 defines which job titles this project is for — 32 titles in five
groups, with the line drawn at engineering and applied research in software, AI
and ML. Nothing read it: the unattended runners searched six hard-coded query
strings and the console's filter knew only what the user had typed.

- `applyops/target_titles.py` ships the pool (same groups, same order, same
  wording) plus the search keywords the runners use.
- **Drift is a test failure, not a surprise.** `from_agents_md` parses the
  document solely so a test can assert the shipped list still equals it; nothing
  at runtime reads markdown, because a policy that depends on a prose file
  sitting next to the installed package breaks the moment the package is
  installed on its own.
- Search keywords are a **declared subset, not a generated list**. The pool is
  semantic; a LinkedIn query is a blunt string. Turning 32 titles into 32 queries
  would be inventing policy, so the six queries are listed explicitly and a test
  requires each one to be backed by a title in the pool.
- `tools/auto_apply.py` now derives `KEYWORDS` from the pool (it used to carry its
  own copy), and `cron_apply.py` keeps rotating through it.
- Adoption is one call: `PreferenceStore.seed_target_titles()` and
  `POST /api/preferences/target-titles/seed`. Additive and idempotent — the
  user's own titles survive, nothing is duplicated. Once adopted, the pool is
  what the filter and the preview apply.

### Every pass states how many it will send

A pass used to take its budget from the stored auto-policy, which is set once and
then silently reused — a number chosen days ago governing tonight's run, and a
pass with no policy quietly becoming zero rather than a question.

- `run_pass(browser, budget=<n>)` is required. Missing, zero, negative or
  non-integer raises `PassBudgetRequired`; the API answers 422; the console's
  button stays disabled until a positive number is entered.
- The count is **not remembered**: every pass states its own, and the next pass
  without one is refused.
- The stored policy remains the **outer gate**. Asking for more than the policy
  allows is refused rather than silently clamped, and auto mode off (or expired)
  still means nothing is sent even when a number was typed — the switch is not
  bypassable by typing.
- `PassReport` reports `budget` and `budget_remaining`, and the console shows
  "本轮额度 N · 已投 M · 剩余 …".

### Tests

| file | tests | covers |
|---|---|---|
| `tests/test_target_titles.py` | 7 | shipped pool equals `AGENTS.md` §12; the scope the document draws (landmarks in, analyst/finance/PM/QA out); every search keyword is backed by a pool title; the runner derives its keywords; seeding makes the pool filter for real; seeding twice is idempotent and keeps the user's own titles; the console endpoint needs the token and then the preview reflects the pool |
| `tests/test_pass_budget.py` | 5 | no budget → refused and nothing sent; zero/negative → refused; budget bounds one pass and does not carry over; asking above the policy → refused; the console asks for the number every time |

Existing suites that had to be told the new rule: `tests/test_m4_supervised_automation.py`
(every pass now states a budget; the policy-bounds test now asserts that asking
for more than the policy is refused) and one acceptance test (the runner endpoint
now requires `budget`).

### Results

`test_target_titles.py` 7 passed (exit 0) · `test_pass_budget.py` 5 passed (exit 0) ·
`test_m4_supervised_automation.py` 13 passed (exit 0) · `test_acceptance_fixes.py`
25 passed (exit 0) · `test_m1_trusted_execution.py` 40 passed (exit 0) ·
`test_m2_unified_core.py` 23 passed · `test_m3_local_ui.py` 9 passed ·
`test_m5_productization.py` 9 passed · `test_core.py` 10 passed ·
`test_blocker_mcp_flow.py` 3 passed · `test_blocker_mcp_answers.py` 4 passed ·
`test_blocker_choice_groups.py` 3 passed · `test_blocker_click_phases.py` 6 passed ·
`test_blocker_reconcile_binding.py` 6 passed · `test_blocker_concurrency.py`
7 passed (3 batches) · frontend vitest 7 passed, build and tsc clean ·
`ruff` 134 findings, same as the baseline.

As before, `test_blocker_concurrency.py` and the suite as a whole were not run in
a single process — the sandbox's memory cap (exit 137) kills them once enough
headless Chromes accumulate.
