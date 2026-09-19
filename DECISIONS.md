# DECISIONS.md

Architecture decisions made while implementing `PLAN.md`, with the alternatives
considered. Written so a later milestone cannot silently reverse an invariant
because it did not know why the invariant existed.

---

## D1 — Verification is three-valued and fails closed

**Decision.** Every form action reports `verified`, `mismatch` or
`unverifiable`. Only `verified` is success. `unverifiable` is the default when
nothing can be proven.

**Why.** The old line `mismatch = bool(readback) and readback.strip() != value.strip()`
reads as a safety check and was the opposite of one: `bool("") is False`, so the
case where the page reported nothing — a number field rejecting letters, a
component whose value never reaches the DOM, a read that timed out — was scored
as agreement. Not knowing was indistinguishable from knowing.

**Rejected alternative.** Keeping a boolean and adding a `readback_ok` flag. A
fourth "probably fine" state is how this got lost the first time; two-valued
successes invite callers to ignore the new field.

**Consequence carried forward.** Anything that counts — statistics, quota, the
failure breaker, "was this applied?" — must ask `outcome == "verified"`, never
`status in ("applied", "success")`.

## D2 — The requesting process cannot grant itself permission

**Decision.** Two objects: `SubmissionRequest` (created by whoever wants to act)
and `SubmissionGrant` (created only by `SubmissionAuthorizer.approve_request`,
which today means the human-facing CLI `python -m applyops.approve`). A request id
is not accepted where a grant id is expected.

**Why.** `submit_application(acknowledged=True)` let the caller assert the user's
approval. A model can write `True`. That is not a boundary; it is a comment.

**Rejected alternative.** Keep the flag but require a `seen_by_human` token minted
by another tool. Still the same process asking and answering.

**Honest limit.** This constrains what ApplyOps itself will do. It does not
isolate against another process running as the same OS user (`PLAN.md` §5.4).

## D3 — Click classification uses structure first, name second, and errs toward blocking

**Decision.** A target is `FINAL_SUBMIT` if it structurally submits a form
(`<input type=submit>`, or a `<button>` whose type is not `button`/`reset` inside
a form) **or** if its accessible name matches final-submit vocabulary. Advancing
(`Next`, `Continue`, `Review`) is explicitly ordinary.

**Why.** Name-only checks miss `<button>Save</button>`; structure-only checks miss
JS-driven custom controls. Both together catch either. The asymmetry between the
two error directions is deliberate: wrongly requiring a grant costs a prompt,
wrongly allowing a click sends an unauthorized application to an employer.

## D4 — A spent grant whose click never settles is `unverified`, never `failed`

**Decision.** `no_wait_after=True` on the final click, and a timeout there is
reported as `unverified` with `reconciliation_required`.

**Why.** Playwright's default click waits for the navigation to finish. Against a
slow employer that wait times out — *after* the HTTP request already left the
machine. Marking that `failed` would tell the operator nothing was sent when it
very likely was, which invites a duplicate submission. Verified empirically
against the demo ATS `scenario=slow`.

## D5 — Snapshot drift is a "nothing was sent" failure, not a refusal

**Decision.** If the form no longer matches the approved digest at execution
time, the path returns `failed` with `sent: False` and does not consume the grant;
every other authorization problem raises `SubmissionRefused`.

**Why.** They are different events with different remediations. "Your approval
expired or is for another job" means re-authorize. "The form changed under you"
means re-read and re-review — and nothing has happened to anybody's employer yet,
which is the one thing worth saying loudly and separately.

## D6 — Grant binding includes fact revisions, not just fields

**Decision.** The digest covers fields + resume sha256 + answers revision +
profile revision + route.

**Why.** Binding only the visible fields would let the stored answers or profile
change after approval and still authorize the submission of values nobody
reviewed. The revisions are cheap to compute (count + newest timestamp; profile
file hash) and make "something you were not shown changed" detectable.

## D7 — `prepare/request` reads the snapshot from the DOM, never from a caller summary

**Decision.** The summary shown to a human is generated from
`BrowserController.field_snapshot()`.

**Why.** A model writing "all fields look correct" is not evidence about the
page. Approving a narrative rather than the values is how an unread field ends up
submitted. Unreadable fields are listed as `<unreadable>` rather than omitted, so
approving one is a decision rather than a gap.

## D8 — Resume is resolved, never defaulted

**Decision.** `resume.resolve_resume()` is the only way to obtain a resume; it
raises for missing/relative/unsupported/empty files instead of falling back.
`tools/auto_apply.py`'s hard-coded `data/resume.pdf` was deleted.

**Why.** Two independent sources of truth guaranteed divergence between what was
attached and what the log claimed. Failure before attaching is cheap; a wrong
file in front of a recruiter is not recoverable.

## D9 — No migration was written for M1

**Decision.** New fields default (`outcome="unverified"`) and new state files are
additive. No existing row was rewritten.

**Why.** Upgrading historic rows derived from unconfirmed submissions would be
inventing evidence — precisely what `PLAN.md` §4.2 forbids. Old applications stay
counted as attempts and stop counting as successes. M2 owns the real migration
with backup/rollback.

## D10 — Demo ATS ships in the package, not in `tests/`

**Decision.** `src/applyops/demo_ats.py`, stdlib only, deterministic, every page
labelled as the demo.

**Why.** It is both the test target and the thing `applyops demo` walks a new user
through. Keeping them identical means safety behaviour cannot drift apart from
what users are shown.
