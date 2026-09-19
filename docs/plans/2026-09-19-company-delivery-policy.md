# Company Delivery Policy Implementation Plan

**Goal:** Route queued applications by a persistent company policy so ordinary companies can auto-submit, review-listed companies pause for human approval, and never-listed companies are skipped.

**Architecture:** Add a small file-backed `CompanyPolicyStore` with deterministic company normalization and precedence (`never > review > default`). Extend `QueueRunner` to apply that decision after the existing shared prepare flow, issuing an existing authorization grant only for safe AUTO submissions and reusing `ApplicationService.submit`. Expose the policy through two local API endpoints and a compact Jobs-page editor; existing grant/state-machine/submission safety remains the final boundary.

**Tech Stack:** Python dataclasses, JSON/file locks, existing FastAPI service, React/TypeScript frontend, pytest, Vitest.

---

### Task 1: Company policy model and normalization

**Files:**
- Create: `src/applyops/company_policy.py`
- Test: `tests/test_company_policy.py`

Write failing tests for default lists, normalization/aliases, precedence, and persistence. Implement only the data model, deterministic normalization, and locked JSON store.

### Task 2: Runner routing and safe AUTO submissions

**Files:**
- Modify: `src/applyops/runner.py`
- Modify: `src/applyops/service.py`
- Test: `tests/test_company_policy_runner.py`

Write failing Demo ATS tests for ordinary AUTO, REVIEW pause, human-approved REVIEW submit, NEVER skip, removal from review, unknown input parking, alias matching, and three independent AUTO applications. Add a thin `skip` service method, inject the company policy store, issue a grant through `SubmissionAuthorizer.issue_grant` for AUTO, and submit through the existing service path. Preserve the legacy supervised behavior when no company-policy file is configured so existing tests remain valid; the app initializes defaults for normal use.

### Task 3: Local API and pending-work visibility

**Files:**
- Modify: `src/applyops/api/app.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/pages/Jobs.tsx`
- Modify: `frontend/src/pages/Attention.tsx`
- Test: `tests/test_company_policy_api.py`

Add GET/POST company-policy endpoints, include review waiting applications in runner status, and add simple editable review/never list controls. Keep existing approval and submit endpoints; the Attention page shows review requests with company/reason and uses the existing approval grant flow.

### Task 4: Verification

Run focused Python tests, existing runner/UI regression tests, frontend Vitest/build/TypeScript checks, and Ruff on changed Python files. Commit the implementation to `feat/applyops-v02` without touching `AGENTS.md` or merging main.
