#!/usr/bin/env python3
"""Attach to a persistent, visible Chrome that Playwright does not own.

Personal / local development helper. **Not part of the shipped MCP surface.**

Why this exists
---------------
The MCP server keeps its browser in a module-level `Runtime`, so the browser
dies the moment that process exits. That is correct for a server, and wrong for
what a first real application actually needs: read one page, stop, think, ask
the human a question, then come back to the *same* tab. A browser that cannot
outlive a single script cannot do that.

So: launch Chrome once with a debugging port and attach over CDP. The window
stays up across scripts, the profile is the same `data/browser-profile` the
server uses (so the LinkedIn session is shared, not duplicated), and each step
is an ordinary short-lived script with no IPC to maintain.

Chrome 136+ refuses to open a debugging port on the *default* user-data-dir,
which is why this points at the project profile rather than the user's own.

`--no-sandbox` is not a shortcut, it is a requirement here and it mirrors what
the server already does: Playwright defaults `chromiumSandbox` to False, so
every browser the MCP server launches is already running without the Chrome
sandbox. Launching by hand *with* the sandbox fails outright in a restricted
execution environment -- child processes log "sandbox initialization failed:
Operation not permitted", the GPU and network services crash in a loop, and
Chrome exits with "GPU process isn't usable. Goodbye." before the debugging
port stays up for a single connection. Matching Playwright's flags is what
makes the attached browser behave identically to the server's.

`--password-store=basic` and `--use-mock-keychain` are NOT optional. They are
how Playwright launches Chrome, and therefore how the cookies in this profile
are encrypted: the mock keychain uses a fixed, non-Keychain key. Launching by
hand *without* them hands Chrome the real macOS Keychain key instead, which
cannot decrypt a single existing cookie -- and Chrome does not merely fail to
read them, it **discards them**. That is not a theory: doing exactly this
emptied `data/browser-profile/Default/Cookies` of all 32 linkedin.com cookies,
`li_at` included, and logged the session out. Any new cookie written after that
point has the same problem in reverse.

So the rule for this file: never launch this profile with a hand-picked flag
set. Whatever is added here has to be a superset of what Playwright passes,
or the two launchers will keep destroying each other's cookies.

`--disable-gpu` is belt-and-braces for the same reason as `--no-sandbox`: the
fatal above comes from the GPU process, which headless runs never start.

Usage
-----
    python tools/attach.py                    # launch if needed, verify, print url
    python tools/attach.py --goto <url>       # ...and navigate

    from tools.attach import session
    async with session() as bc:
        print(await bc.get_current_url())
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from applyops.browser import BrowserController  # noqa: E402

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = PROJECT_ROOT / "data" / "browser-profile"
DEFAULT_PORT = 9222


def _version_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json/version"


def is_up(port: int = DEFAULT_PORT) -> bool:
    """True when something is already answering DevTools on this port."""
    try:
        with urllib.request.urlopen(_version_url(port), timeout=1.0) as resp:
            return json.loads(resp.read()).get("Browser", "").startswith("Chrome")
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch(port: int = DEFAULT_PORT, url: str = "about:blank", wait: float = 30.0) -> None:
    """Start a detached Chrome on the project profile, then wait for the port.

    Detached on purpose: this process is expected to exit while Chrome keeps
    running, which is the entire point of the helper.
    """
    if is_up(port):
        return
    if not Path(CHROME_BIN).exists():
        raise RuntimeError(f"Chrome not found at {CHROME_BIN}")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            CHROME_BIN,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1400,950",
            # Mirrors Playwright's own defaults -- see the module docstring.
            # The keychain pair is load-bearing: without it Chrome cannot
            # decrypt this profile's cookies and deletes them.
            "--no-sandbox",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + wait
    while time.time() < deadline:
        if is_up(port):
            return
        time.sleep(0.4)
    raise RuntimeError(f"Chrome did not open a debugging port on {port} within {wait}s")


@asynccontextmanager
async def session(port: int = DEFAULT_PORT, auto_launch: bool = True):
    """Yield a `BrowserController` wired to the live browser.

    The browser is borrowed, never owned: nothing here closes it, so the tab
    you were looking at is still there for the next script.
    """
    if auto_launch:
        launch(port)

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    controller = BrowserController(headless=False)
    controller._playwright = pw  # noqa: SLF001
    controller._context = context  # noqa: SLF001
    controller._page = page  # noqa: SLF001
    try:
        yield controller
    finally:
        # Deliberately no close(): dropping the CDP connection only detaches.
        # Closing here would kill the window the user is watching.
        controller._page = None  # noqa: SLF001
        controller._context = None  # noqa: SLF001
        controller._playwright = None  # noqa: SLF001
        try:
            await pw.stop()
        except Exception:
            pass


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--goto", default="")
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()

    if not args.no_launch:
        launch(args.port, args.goto or "about:blank")
    elif not is_up(args.port):
        raise SystemExit(f"nothing listening on {args.port} and --no-launch was given")

    async with session(args.port, auto_launch=False) as bc:
        if args.goto and not args.no_launch:
            pass  # already navigated by the launch argument
        elif args.goto:
            await bc.goto(args.goto, settle=2.0)
        print("attached:", await bc.get_current_url())
        print("title   :", await bc.page.title())
        tabs = await bc.list_tabs()
        for t in tabs:
            print(f"  tab[{t.index}] active={t.active} {t.url[:90]}")


if __name__ == "__main__":
    asyncio.run(_main())
