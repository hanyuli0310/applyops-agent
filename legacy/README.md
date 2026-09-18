# legacy/ — retired v1 harness

These modules are **not part of the package** (the wheel only ships `src/applyops`).
They were moved here on 2026-09-17 as part of M1 work item W8, which retired the
"project owns the brain" architecture in favour of a harness-agnostic MCP server.

| Path | Lines | Why it was retired |
|---|---|---|
| `agent.py` | 584 | The agent loop. Replaced by the *caller* — Codex / WorkBuddy / Claude Code now drive the tools. Without a loop there is no `sleep(3)` retry spin and no unbounded `await event.wait()`. |
| `llm/` | ~605 | Three provider adapters plus model-selection settings. The project no longer calls a model, so the missing-API-key blocker and provider drift both disappear. |
| `web/` | ~370 | FastAPI + WebSocket UI, the old front end. The MCP client is the front end now. |

Nothing in `src/` imports these. They are kept only so the removal is reversible —
this project has no version control, so deleting outright would have been
unrecoverable.

**To restore v1:** move them back to `src/applyops/` and re-add the `fastapi`,
`uvicorn`, `websockets`, `anthropic`, `openai`, `google-genai`, `aiofiles` and
`python-multipart` dependencies that were pruned from `pyproject.toml`.
