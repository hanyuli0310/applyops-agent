# Application Dashboard Implementation Plan

**Goal:** Make the ApplyOps frontend open on a real-time application dashboard that shows queue progress, human actions, evidence, and application history without changing the backend data model.

**Architecture:** Add a dashboard page as the default tab, backed by the existing `/api/status`, `/api/applications`, `/api/requests`, `/api/runner/status`, and application-detail endpoints. Keep the existing profile, jobs, attention, and history pages available as secondary views; reuse the current action endpoints and grant storage.

**Tech Stack:** React 18, TypeScript, Vite, existing ApplyOps FastAPI API, CSS.

---

### Task 1: Extend frontend API types and shared display helpers

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/dashboard.ts`

**Step 1: Add detail/view types**

Represent the optional application presentation fields returned by the API (`display_state`, `reason_text`, `available_actions`, `reason_code`) and the detail response's `view` object without changing request functions.

**Step 2: Add pure dashboard helpers**

Create helpers for grouping applications by state, calculating an application progress percentage, choosing the next action, and formatting timestamps. Keep these functions free of React and browser state so the dashboard can use them consistently.

**Step 3: Verify type safety**

Run `npm run build` from `frontend/` and confirm TypeScript accepts the new types and helpers.

### Task 2: Add the dashboard page and live refresh model

**Files:**
- Create: `frontend/src/pages/Dashboard.tsx`
- Modify: `frontend/src/App.tsx`

**Step 1: Make dashboard the default tab**

Add a `dashboard` tab before the existing pages. Preserve the existing pages and route labels; only change the initial tab.

**Step 2: Load dashboard data together**

Fetch status, all applications, pending requests, and runner status on initial load and every five seconds. Track `lastSyncedAt`, loading, stale, and error states. A failed refresh must preserve the last successful snapshot.

**Step 3: Render overview sections**

Implement metric cards, the active application spotlight, the application queue, and the fixed attention panel. Each card uses the shared state labels and progress helpers.

**Step 4: Add filtering and selection**

Support state/company/platform filters in the queue. Selecting a row opens the detail drawer without leaving the dashboard.

### Task 3: Add application detail drawer and safe actions

**Files:**
- Create: `frontend/src/components/ApplicationDetailDrawer.tsx`
- Create: `frontend/src/components/StatusBadge.tsx`
- Create: `frontend/src/components/MetricCards.tsx`
- Create: `frontend/src/components/AttentionPanel.tsx`

**Step 1: Display detail and timeline**

Load `/api/applications/{id}` when a row is selected. Show job identity, route/platform, reason, field/fill evidence when available, attempts, and chronological events.

**Step 2: Reuse existing action boundaries**

Wire prepare, answer, approve, submit, reconcile, cancel, and export actions to existing API functions. Store grants by application ID using `frontend/src/grants.ts`. Never show an automatic retry-submit action for `submitted_unverified`.

**Step 3: Surface safe failure states**

Display backend rejection messages in the drawer and keep the user on the detail view. After any mutation, refresh the selected application and dashboard snapshot.

### Task 4: Restyle the console for dashboard density and responsive use

**Files:**
- Modify: `frontend/src/styles.css`
- Modify: `frontend/index.html`

**Step 1: Add dashboard layout primitives**

Add the sidebar/header, metric grid, two-column dashboard area, queue table/card layout, detail drawer, timeline, state colors, and stale/error banners.

**Step 2: Add narrow-screen behavior**

Collapse the sidebar and stack cards/panels below a tablet breakpoint. Keep buttons and status labels readable without horizontal scrolling.

**Step 3: Verify visual build output**

Run `npm run build` and inspect the generated page through the local server. Confirm no old AAAI-style or placeholder UI text remains relevant to this console.

### Task 5: Verification and integration

**Files:**
- No new backend files expected.

**Step 1: Run frontend verification**

Run `npm run build` from `frontend/`.

**Step 2: Run backend regression tests**

Run `.venv/bin/python -m pytest tests/ -q` from the project root if the worktree has the environment available; otherwise run the repository's documented test command from the canonical checkout.

**Step 3: Run a dashboard smoke check**

Start the local ApplyOps console with the existing Demo ATS path, open the dashboard, enqueue or prepare a demo application, and verify that the dashboard transitions through queue, approval, submission, and result states without a page reload.

**Step 4: Commit the implementation**

Run `git diff --check`, review `git diff --stat`, then commit the dashboard changes with a focused message such as `feat: add application dashboard`.
