# AGENTS.md

Operating notes for an agent — or a person — driving this repository.
Read this file first. It is the shortest path from `git clone` to a filled-in
application form.

---

## 1. What this is

ApplyOps is **not** an agent. It has no model, no API key and no loop of its own.

It is an **MCP server** that gives an agent two things:

| | |
|---|---|
| **Hands** | A real browser that fills real forms — through shadow DOM and cross-origin iframes — plus a locator layer built to survive a page redesign |
| **Memory** | A flywheel that records every question ever answered and every selector that ever worked, so application N+1 interrupts the user less than application N |

The brain is yours: Codex, WorkBuddy, Claude Code, anything that speaks MCP.
That split is the whole design. It is what removes the API-key requirement and
what lets one codebase serve every harness.

**32 tools** are exposed. You will use about eight of them for a normal
application; the rest are diagnostics and the unattended runners.

## 2. Requirements

- macOS or Linux, Python **≥ 3.12**
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- **Google Chrome** — installed by Playwright. Not bundled Chromium: importing
  your existing LinkedIn session only works against a real Chrome profile.
- A **LinkedIn account**, if you want the Easy Apply path

## 3. Setup

```bash
git clone git@github.com:hanyuli0310/applyops-agent.git
cd applyops-agent

uv sync                                  # creates .venv/
uv run playwright install chrome

# Build your profile: this asks every question a form will ask.
.venv/bin/applyops-init

# LinkedIn requires a login. Reuse the one Chrome already has:
.venv/bin/python tools/import_chrome_session.py --list      # find the profile holding the session
.venv/bin/python tools/import_chrome_session.py --verify    # confirm it landed
```

Then register the stdio server with your harness:

```bash
.venv/bin/applyops-mcp
```

If you skip `applyops-init`, nothing breaks: the first `setup_status()` call
reports the profile as incomplete and hands you the same questions in
`questionnaire`. Both paths are generated from one field definition, so they
can never disagree.

## 4. Verify the install before trusting it

```bash
.venv/bin/python tools/op.py ping '{}'                 # server reachable
.venv/bin/python tools/op.py setup_status '{}'         # profile complete?
.venv/bin/python tools/op.py guard_status '{}'         # remaining quota today
.venv/bin/python -m pytest tests/ -q                   # 22 tests, ~6s
```

`tools/op.py <tool> '<json>'` builds the **real** server and calls **one** tool
with the arguments you pass. Every production path runs — guardrails, memory,
locator, runtime lock. Only the browser's owner differs, so it is safe to use it
to drive a single step by hand. It exits 1 when the tool reported an error, 3
when the browser is held by another driver.

## 5. Driving one application

The order matters; each step exists because skipping it caused a real failure.

```
preflight(job_url)            # 1. always first — permission, pacing, dedupe
route_guide(job_url)          # 2. before the browser — which route, what gates
browser_open(job_url)         # 3. open it
browser_state()               # 4. read the form: fields, buttons, tabs
get_answer(question)          # 5. for EACH question — memory first, user last
fill_field / select_option /
set_checkbox / upload_file    # 6. fill, passing ref + role + reason
request_submit_confirmation   # 7. one-time token, after showing a full summary
submit_application            # 8. only after the browser actually submitted
report_failure(...)           # 9. if it died, say where
```

**Step 1 — `preflight` is not optional.** It enforces the daily cap, the minimum
gap between applications, and the already-applied check. If `allowed` is false,
stop and relay the reason. It sleeps to honour pacing, so a call may take a
minute. Working around it risks the LinkedIn account, which costs more than
applying to fewer jobs today.

**Step 2 — `route_guide` before you open anything.** Not every application is an
Easy Apply. See §6.

**Step 5 — ask memory before asking the human.** `get_answer` returns one of
three verdicts:

| verdict | what you do |
|---|---|
| `answered` | fill it, verbatim — do not re-ask |
| `suggestion` | confirm with the user, then `record_answer` |
| `need_human` | ask the user, then `record_answer` |

Batch every question you have to ask into **one** message. A Workday form holds
ten of them. Asking the memory something it already knows is the single
behaviour that makes this tool worthless.

**Step 6 — check `ok` on every fill.** `mismatch: true` means the page rejected
the value. It is not filled. Never treat it as filled.

**Step 7 — the confirmation token is the only real guardrail against a wrong
submission.** `submit_application` refuses without it, and that refusal is the
point. Show the user every field and value before asking.

## 6. Routes: Easy Apply is the easy case

`route_guide(job_url)` names the route you are on.

| route | shape |
|---|---|
| `easy_apply` | The form is a modal on the posting. Nothing navigates away, no account gate. |
| `external_ats` | The posting hands the browser to the employer's system. |

`external_ats` is not one shape but several:

- **Greenhouse / Lever** — a single page, no account.
- **Amazon** — `passport.amazon.jobs`, a sign-in *before* the first field,
  usually confirmed by an emailed one-time code.
- **Workday** — often an account creation further down the flow.

A step marked `human_required` is a wall, not a speed bump: a one-time code, a
captcha, an account password. Stop and ask. Batch every such gate you can
foresee into the same message as the profile questions — discovering them one at
a time means abandoning a half-filled form.

When `click_target` reports `new_tab: true`, follow the tab. That is the
`external_ats` route opening somewhere else, not a failure.

## 7. Invariants

These are load-bearing. Breaking one produces a system that looks like it works.

1. **The flywheel records inside the tools, never in the caller.**
   `fill_field` has no three-argument form. If recording lived in the caller, a
   different agent loop would starve the memory while `memory.json` stayed
   plausible-looking.
2. **Submission requires a one-time token.** No token, no submission.
3. **A blank profile field means "not answered" — ask, never guess.** Salary,
   visa status, work authorization, legal declarations, years of experience and
   skill self-ratings are the cases where a confident wrong answer is real harm.
4. **`selectors_suggested` must be non-zero after real runs.** It is the only
   signal separating "never tried" from "tried and always failed" — both show a
   `hit_rate` of 0. A permanently zero value means the memory is dead.
5. **A `human_required` gate is declared up front**, in the route, before the
   form is opened.

## 8. One browser at a time

Several processes share one `data/browser-profile`, because the logged-in
session is the one thing this project cannot rebuild for itself. Two Chromes on
one profile do not merely slow down — they overwrite each other's cookie
database.

So the profile has an **exclusive lock** and there is exactly one driver.

If `browser_open` or `discover_jobs` replies `browser_busy: true`, another
driver holds it and the reply names which one. Say so and stop. Do not retry in
a loop: the holder is normally mid-application.

Everything else is designed to be written concurrently and safely:

| file | mechanism | accuracy |
|---|---|---|
| `guard_state.json` — cap, breaker, tokens | lock + read-modify-write | **exact** |
| `memory.json` — the flywheel | lock + **merge**-on-write | no record lost; counters take the max |
| `application_log.json` — the ledger | same lock, merged by posting | no row lost or duplicated |

Locks are `flock`, not pid files. The kernel releases them however a process
dies, including `SIGKILL`, so there is no stale-lock cleanup anywhere and no
code path that has to ask whether another process is still alive.

## 9. Where state lives

Everything mutable is under `data/`, which is **git-ignored in full**.

| path | contents | safe to delete? |
|---|---|---|
| `data/profile.md` | your facts, as a hand-editable markdown file | no — that is your work |
| `data/memory.json` | the flywheel: learned answers, selector scores, history, route knowledge | no |
| `data/guard_state.json` | today's count, breaker, outstanding confirmation tokens | yes |
| `data/application_log.json` | the ledger the batch runners read | yes, but you lose the record |
| `data/browser-profile/` | the live Chrome profile, **including session cookies** | yes, then re-import |
| `data/.locks/`, `data/.cron_apply.lock` | lock files | yes, when nothing is running |

`data/profile.md` holds personal data and is deliberately a *different* file
from `memory.json`: the profile is yours to hand-edit, the memory is meant to
accumulate and should not be edited by hand. Two files, two lifecycles.

**The repository contains no personal data of any kind.** No default name,
email, phone or salary; `applyops-init` asks for all of it. Keep it that way —
never commit `data/`, and never paste a real value into `profile.example.md` or
into a test fixture.

## 10. Tests and lint

```bash
.venv/bin/python -m pytest tests/ -q        # 22 tests
.venv/bin/ruff check src tools tests
.venv/bin/python tools/cron_apply.py --status
```

`tests/test_concurrency.py` **really** spawns processes: eight writers on one
`memory.json` losing nothing, twelve racing a cap of five and exactly five
winning, four processes spending one confirmation token with one success, and a
holder killed with `SIGKILL` releasing the lock immediately. Do not replace
these with mocks — the bugs they catch only exist across process boundaries.

## 11. Repo layout

```
src/applyops/
├── mcp/                # the entire public surface: 32 tools
│   ├── server.py       # stdio entry point + the operating contract
│   ├── runtime.py      # shared state: memory / guardrails / browser lock
│   └── tools.py        # tool registration, with flywheel recording inline
├── profile.py          # profile field definitions + profile.md read/write
├── cli.py              # the applyops-init wizard
├── locator.py          # element location through shadow DOM and iframes
├── browser.py          # Playwright control: human-like typing, tab following
├── memory.py           # the flywheel: Q&A, selectors, routes
├── guardrails.py       # cap / pacing / dedupe / breaker / submit confirmation
├── concurrency.py      # flock, atomic writes, lock inheritance
└── platforms/detector.py

tools/                  # operator entry points, not shipped in the wheel
├── auto_apply.py       # batch runner: discover | apply | retry
├── cron_apply.py       # unattended loop: --ensure / --status / --stop
├── op.py               # call exactly one MCP tool from the shell
├── attach.py           # attach to a running Chrome over CDP
├── import_chrome_session.py
└── singlewriter.py     # browser-lock facade

legacy/                 # the retired v1 (self-owned agent loop + LLM + Web UI)
```

`legacy/` is dead code, kept as a rollback point and as the record of why the
architecture turned. Nothing in `src/` imports it.

## 12. Target job titles

**Who this is for.** The person this installation serves is a recent graduate
starting their first engineering job — a **new grad / entry-level** candidate.
The pool below defines the *kind of work*; seniority is a separate question and,
for this candidate, it is already settled: **only new grad, entry level, early
career, associate and university graduate titles are in scope.** A posting whose
title says senior, staff, principal, lead or manager is out of scope even when
the work matches one of the titles below. This is a fact about the applicant, not
a preference — it is written here because everything that decides "is this
posting for us" has to read it from the same place.

The pool of titles the agent is for. It answers one question only: **is this
posting the kind of job we are looking for?** Everything else — how a resume is
picked, how a description is scored, work authorization, location, seniority —
is deliberately not defined here.

These are **semantic targets, not exact strings.** A posting matches when its
title means one of these jobs, not when it spells one of them exactly. Common
naming variations, punctuation and separator differences, abbreviations, and
closely equivalent titles all count. `Software Engineer - AI`, `AI Applications
Developer` and `Member of Technical Staff, AI` are all in scope even though none
of them appears on the list verbatim.

The line runs at "is this an engineering or applied-research job in software, AI
or ML". It does not extend sideways into analyst, finance, accounting, product
management, QA, IT support, sales, or other non-technical business roles. When a
title is genuinely ambiguous, treat it as out of scope rather than stretching a
neighbour in the list to cover it.

### Software engineering — general and entry level

- Software Engineer
- Software Development Engineer
- Software Engineer I
- Associate Software Engineer
- Entry Level Software Engineer
- Early Career Software Engineer
- New Grad Software Engineer
- University Graduate Software Engineer

### Backend and full stack

- Backend Software Engineer
- Backend Engineer
- Full Stack Software Engineer
- Full Stack Engineer

### Product and platform

- Product Engineer
- Platform Engineer

### AI and machine learning

- AI Engineer
- AI Software Engineer
- Applied AI Engineer
- AI Application Engineer
- AI/ML Engineer
- Machine Learning Engineer
- ML Engineer
- Machine Learning Software Engineer
- Applied Machine Learning Engineer
- Generative AI Engineer
- LLM Engineer
- AI Agent Engineer
- AI Product Engineer

### Applied research and data

- Forward Deployed Engineer
- Research Engineer
- AI Research Engineer
- Computer Vision Engineer
- Data Scientist

Adding a title is appending one line to the group it belongs in. Nothing else
has to change for the pool to grow.

**This section is the written definition of intent, not the switch that flips
it.** What the unattended runners actually search for today is still
`KEYWORDS` in `tools/auto_apply.py` — a handful of LinkedIn query strings, which
is a different and much blunter thing. Keeping the two in step is a change to
that file, not to this one.

## 13. Unattended runs

Two runners exist for applying without a conversation. Both take the same
browser lock, so neither can collide with the other or with you.

```bash
# One pass in the foreground
.venv/bin/python tools/cron_apply.py --dry-run     # rails + browser only, no applications

# A supervised loop
.venv/bin/python tools/cron_apply.py --ensure      # start it if it is not running
.venv/bin/python tools/cron_apply.py --status      # is it up? how much quota is left?
.venv/bin/python tools/cron_apply.py --stop
```

**Run `--dry-run` first.** And prefer to be watching the first real submission:
the loop drives the same Chrome profile you do, and a mis-filled form is cheaper
to catch than to undo.

---

## 14. Standing applicant defaults

Decisions the applicant made once, so that forms stop asking them one at a
time. Every agent and every code path reads this section before it fills
anything; these are standing answers, not guesses.

1. **Consent and agreement checkboxes: check them.** "By checking this box you
   consent to…" is agreed by default. The applicant has decided that the
   convenience of applying outweighs reading each consent line.
2. **Experience questions: never select "none of the above" — select the options
   that match the applicant's background.** The applicant has decided that the
   cost of leaving an experience question blank (or marking it "none") outweighs
   the stretch of selecting a related option. For a multi-select, pick every
   option that his background plausibly covers; for a single choice, pick the
   closest match. His background: UCSD MSCS, Alibaba Cloud SWE intern (AI
   agents, harness engineering, MCP, backend infrastructure).
3. **"Where are you currently based?" and every location question takes the
   posting's location** (`applications.location`, filled as `job:location`) —
   the applicant decided that a city question is about *which* job, not about
   where he lives. Remote postings included.
4. **A question is answered by what it asks, not by how it is worded.** He
   answers each question once; a form that words it differently inherits that
   answer through its semantic class (sponsorship, work authorization,
   relocation, workplace mode, compensation basis, background check, criminal
   history). "Do you need a Work VISA to work in the country where this job is
   located?" and "Will you now or in the future require sponsorship for
   employment visa status?" are one question with one answer. A question
   outside those classes is still asked, never answered from a neighbour — and
   the accumulated answers only count when the store can actually read them,
   which is why `AnswerStore` reads the flywheel's `learned_qa` through rather
   than keeping a second copy.
5. **Questions that are facts about him and are not covered here** — new ones,
   or ones where the truthful answer would be "none of the above" — still go to
   the applicant. The store of answers is the place to record them once, so the
   next application does not ask again.

These defaults are standing answers to real questions on real applications.
Adding to this list is the applicant's decision; nothing here may be inferred
by an agent on its own.

---

## License

MIT — see `LICENSE`.
