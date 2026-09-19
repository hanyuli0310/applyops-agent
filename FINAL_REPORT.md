# FINAL_REPORT.md — ApplyOps v0.2

Implementation of `PLAN.md` (M1–M5), executed on branch `feat/applyops-v02`,
local commits only, nothing pushed, nothing submitted to any real employer.

Baseline: `main` @ `7f4d505`. Final commit of this run: see `git log` —
M1 `ce0a5da`, M2 `0663b10`, M3 `5ea60df`, M4 `dc8db93`, M5 (this commit).

---

## 1. Architecture

```
本地浏览器 UI ────────────────┐
外部 AI 助手 → stdio MCP 适配 ├→ ApplicationService → 单一提交路径 → BrowserController
CLI（applyops / op.py）──────┘         │
                          SQLite 账本（状态机 / 授权 / 事件 / 尝试）
                                       │
              profile.md + memory.json + app.sqlite + scoped_answers.json
                                       + submission_requests/grants.json
```

- **Core unchanged**: no model, no API key, no agent loop. The harness
  (Codex / WorkBuddy / Claude Code) remains the brain; ApplyOps is hands +
  memory + guardrails + execution infrastructure. `legacy/` stays retired.
- **One browser owner**: the API/UI process or an MCP session — never both.
  Claims and the profile lock enforce it.
- **One submission path**: `submission.execute_authorized_submission`. It
  verifies a grant against a live DOM snapshot, spends the grant, clicks once,
  and reports `verified` / `unverified` / `failed`. Nothing else in the
  codebase can perform a final external submit.

## 2. Completed milestones

| milestone | commit | essence |
|---|---|---|
| M1 Trusted Execution | `ce0a5da` | three-valued verification; final submit behind a real authorization boundary (request→human→grant); no generic-click bypass; verified/unverified/failed separated; single resume source |
| M2 Unified Core | `0663b10` | SQLite ledger + explicit state machine; claims so two entry points cannot double-execute; crash recovery lands in `SUBMITTED_UNVERIFIED`; legacy migration with backup, never upgraded to success |
| M3 Local Web UI | `5ea60df` | FastAPI backend over the core + React/TS/Vite frontend, four pages; approval lives in the local UI on loopback; real-browser E2E of the whole demo flow |
| M4 Supervised Automation | `dc8db93` | scoped answers (global/company/application), withdrawal voids grants; supervised queue runner; limited auto mode as an explicit, bounded, off-by-default policy |
| M5 Productization | this commit | `applyops doctor / serve / demo / stop`; onboarding path verified; version 0.2.0 |

## 3. Test results

Per-module runs (single-process runs of the whole suite are killed by this
sandbox's memory cap — many headless Chromes; a CI matrix should shard them):

```bash
pytest tests/test_core.py tests/test_concurrency.py        # 22 passed
pytest tests/test_m1_trusted_execution.py                  # 40 passed
pytest tests/test_m2_unified_core.py                       # 23 passed
pytest tests/test_m3_local_ui.py                           #  9 passed
pytest tests/test_m4_supervised_automation.py              # 13 passed
pytest tests/test_m5_productization.py                     #  9 passed
# total: 116 passed, 0 failed
ruff check src tools tests   # 135 findings, all pre-existing debt (baseline was 140)
npm run build                # tsc + vite, clean
```

The M1 regression test was proven to fail under the old semantics before the fix
was accepted (temporarily reverted, test went red, restored, green).

## 4. Known limitations

1. **Same-OS-user boundary.** Local files/locks constrain what ApplyOps does,
   not what another process running as the same user could do.
2. **Over MCP, "the user said yes" arrives as text.** It cannot mint a grant —
   only `applyops approve` or the loopback UI endpoint can — but a harness could
   still misreport a conversation. The UI (M3) removes that channel for local
   use.
3. **Route coverage.** Only LinkedIn Easy Apply and the demo ATS have
   submission paths; other platforms remain read/discover + manual-finish.
4. **`cron_apply.py` unchanged** — still the legacy flow with the same
   guardrails; migrating it onto `QueueRunner` is the natural next step but is
   gated on limited-auto-mode decisions.
5. **No SSE** — the UI polls every 5 s.
6. Frontend `dist/` must be built once (`cd frontend && npm install && npm run
   build`); `applyops serve` tells the user exactly that when it is missing.

## 5. Unsupported routes

- Amazon / Workday / Greenhouse / Lever / external ATS: `route_guide` advises,
  filling works, but the final action requires a human (`manual_required`) —
  there is no verified success signal for them yet.
- CAPTCHA, OTP, account creation: always `human_required`; never automated
  (`PLAN.md` §1.2).

## 6. Security / privacy considerations

- The repository contains no personal data; `data/` is fully git-ignored and
  was never read, modified, or committed during this work (all tests use
  temp data roots).
- Grants bind job key + live field snapshot + resume sha256 + answer/profile
  revisions + expiry; single-use under a file lock.
- The API binds loopback only; `serve()` refuses other hosts. Host/Origin
  hardening and a session token are the next hardening steps before beta.
- No secrets, cookies, resume contents or raw sensitive logs enter logs or
  exports; the UI export is the user's own local history.

## 7. Migration behavior

- `memory.json` history imports into `app.sqlite` as terminal
  `LEGACY_IMPORTED` rows: idempotent, backed up first, source untouched on
  failure, **never counted as verified successes**.
- Old `ApplicationRecord` rows load as `outcome="unverified"`.
- New state files (`app.sqlite`, `scoped_answers.json`,
  `submission_requests.json`, `submission_grants.json`, `auto_policy.json`) are
  additive; deleting them loses nothing else.
- Schema downgrades are refused (`PRAGMA user_version`).

## 8. Clean install instructions

```bash
git clone https://github.com/hanyuli0310/applyops-agent.git
cd applyops-agent
uv sync --extra dev                     # Python 3.12+
uv run playwright install chrome        # real Chrome
cd frontend && npm install && npm run build && cd ..
uv sync --extra dev                     # pick up the console script
.venv/bin/applyops doctor               # explains anything still missing
.venv/bin/applyops demo                 # console at http://127.0.0.1:8620
```

## 9. Demo instructions

1. `applyops demo` → open http://127.0.0.1:8620.
2. 资料与简历: upload a PDF; fill the flagged required fields.
3. 岗位与偏好: the demo job is already queued (or add its URL again).
4. 运行与记录: 准备申请 → the runner reads the local demo form.
5. 待我处理: read the summary (values read back from the form), 批准.
6. 运行与记录: 提交 → 「已确认提交成功」 with matched evidence text.
7. 详情 shows the attempt, outcome and full event history; 导出 JSON for the
   record. Everything ran on `127.0.0.1`; nothing left the machine.

## 10. Remaining work before public beta

1. Host/Origin checks + session auth on the local API; CSRF for the UI.
2. Move `cron_apply.py`/`auto_apply.py` onto `QueueRunner` and delete their
   duplicated flow code.
3. "Answer once → resume affected applications" sweep in the UI (the scoped
   store and revisions already carry the semantics).
4. Real-route verification on platforms that permit it, each with its own
   success-evidence vocabulary; until then those routes stay manual-finish.
5. Sharded CI (unit / concurrency / browser) with the frontend build cached.
6. 3–5 external testers on the demo flow; measure time-to-first-approval and
   interruptions per application (`PLAN.md` §7 M5 acceptance metrics).
