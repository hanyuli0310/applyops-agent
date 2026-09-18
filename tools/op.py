#!/usr/bin/env python3
"""Invoke ONE ApplyOps MCP tool against a browser that outlives the script.

Personal / local development helper. **Not part of the shipped MCP surface.**

Why this exists
---------------
A real application cannot be driven by a single script. Filling a form means
reading a page, deciding, asking the human a question, waiting for the answer,
then coming back to the same tab. The MCP server cannot hold that state across
separate invocations because its browser dies with its process -- so during
development the browser is held open separately (`tools/attach.py`) and each
step here is one ordinary short-lived script.

What matters is that this does NOT reimplement any of the tool logic. It builds
the real server with the real `build_server()`, then hands `RUNTIME` a
`BrowserController` attached over CDP instead of letting it launch its own. So
the guardrails, the memory, the locator and the runtime lock are all the
production code paths -- only the browser's owner differs.

It does hold the driver's browser lock (`tools/singlewriter.py`), because the tab
it drives is the same tab the scheduled pass and the batch runner drive. A step
from here landing in the middle of an application a pass is filling in would
produce two half-applications, and neither process could tell. Exit code 3 means
somebody else has the browser, with their name printed.

Usage
-----
    python tools/op.py --list
    python tools/op.py preflight '{"job_url": "https://...", "job_id": "123"}'
    python tools/op.py browser_state '{}'
    python tools/op.py browser_state '{"include_text": true}'

Exit code is 1 when the tool reported an error, so a caller can branch on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from applyops.mcp.server import RUNTIME, build_server  # noqa: E402
from tools import singlewriter  # noqa: E402
from tools.attach import session  # noqa: E402


def _blocks(result) -> str:
    """Flatten a CallToolResult into text."""
    chunks = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
    return "\n".join(chunks) if chunks else repr(result)


# Tools with no relationship to an in-flight application: pure local reads and
# writes, so they are safe (and useful) while another driver owns the browser --
# asking `route_guide` or `get_answer` what a form will need is exactly what you
# want to do *before* you get the browser.
#
# An allowlist rather than a list of browser tools, and it defaults the other
# way on purpose: a tool nobody has classified yet takes the lock, which is
# over-restrictive rather than unsafe.
_MEMORY_ONLY = frozenset(
    {
        "application_history",
        "check_already_applied",
        "flywheel_stats",
        "get_answer",
        "get_profile",
        "get_selector_hints",
        "guard_status",
        "ping",
        "preflight",
        "record_answer",
        "route_guide",
        "save_profile",
        "setup_status",
        "update_profile",
    }
)


def _pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        return raw


async def _call(tool: str, args: dict, port: int) -> tuple[str, bool]:
    server = build_server()
    async with session(port) as controller:
        RUNTIME._browser = controller  # noqa: SLF001
        try:
            result = await server.call_tool(tool, args)
        finally:
            RUNTIME._browser = None  # noqa: SLF001
    text = _blocks(result)
    failed = bool(getattr(result, "isError", False))
    if not failed:
        try:
            payload = json.loads(text)
            failed = isinstance(payload, dict) and bool(payload.get("error"))
        except (ValueError, TypeError):
            pass
    return text, failed


async def _list_tools() -> None:
    server = build_server()
    for tool in sorted(await server.list_tools(), key=lambda t: t.name):
        summary = (tool.description or "").strip().splitlines()
        print(f"{tool.name:<28} {summary[0] if summary else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", nargs="?", help="tool name")
    parser.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    parser.add_argument("--list", action="store_true", help="list available tools")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--raw", action="store_true", help="do not pretty-print")
    opts = parser.parse_args()

    if opts.list:
        asyncio.run(_list_tools())
        return 0

    if not opts.tool:
        parser.error("a tool name is required (or use --list)")

    try:
        arguments = json.loads(opts.args)
    except ValueError as exc:
        print(f"arguments must be a JSON object: {exc}", file=sys.stderr)
        return 2

    # This drives the same browser as the scheduled pass and the batch runner,
    # which is the whole point -- the tab is shared. So it is a driver, and it
    # has to hold the driver's lock: without it, a step here can land in the
    # middle of an application a pass is filling in, and the two interleaved
    # halves look like neither's work. Memory-only tools skip it; see
    # `_MEMORY_ONLY`.
    lock = None
    if opts.tool not in _MEMORY_ONLY:
        lock = singlewriter.acquire(purpose=f"op.py {opts.tool}")
        if lock is None:
            print(
                f"browser busy: {singlewriter.describe()}. Another driver is "
                "mid-application; wait for it, or stop the loop with "
                "`.venv/bin/python tools/cron_apply.py --stop`.",
                file=sys.stderr,
            )
            return 3
    try:
        text, failed = asyncio.run(_call(opts.tool, arguments, opts.port))
    finally:
        singlewriter.release(lock)
    print(text if opts.raw else _pretty(text))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
