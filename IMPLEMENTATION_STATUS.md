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
