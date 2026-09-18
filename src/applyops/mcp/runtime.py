"""Shared runtime state for the MCP tools.

A single MCP server process serves one person on one machine, so a module-level
runtime is appropriate here -- the multi-tenant problem that made global state
dangerous in the old web server does not exist in this shape. The browser is
launched lazily on first use, so a memory-only call does not pay for a browser.

"That one process" is only half the picture, and the half that was wrong. On one
machine there are at least three drivers pointing at the same
`data/browser-profile`: this server, the scheduled pass (`tools/cron_apply.py`
and the `tools/auto_apply.py` children it spawns), and whatever the human runs by
hand. An `asyncio.Lock` orders calls *within* this process and does nothing at
all against another process -- and two Chromes on one profile do not merely race,
they rewrite each other's cookie database. The logged-in session is the one
thing this project cannot rebuild for itself, so that is a destructive failure,
not a degradation.

So the profile is also protected by the cross-process lock in
`applyops.concurrency`: taken when this process launches a browser, held until
that browser is closed or the server shuts down, and refused -- with the
holder's name -- when somebody else has it.

Why at *launch* rather than around every call: a controller can be injected from
outside. `tools/auto_apply.py` attaches over CDP and assigns `RUNTIME._browser`
before driving the tools, and in that case the profile was handed to this process
by whoever launched the shared Chrome -- which is also whoever holds the lock.
Re-acquiring there would make a supervisor deadlock against itself, which is
precisely the failure that used to make every phase of every scheduled pass exit
with "another runner holds the browser". The lock answers "may I open a Chrome on
this profile", and that question is only ever asked in the launch branch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from .. import concurrency
from ..browser import BrowserController
from ..guardrails import Guardrails
from ..memory import MemoryStore

# Domain -> platform label. Kept here rather than in `platforms/detector.py`
# because this is about *naming the flywheel bucket*, not about deciding how to
# drive a site; the richer detector stays available for the M3 adapters.
#
# The labels must agree with `Platform`'s values for any platform that has an
# apply route on record: a route is keyed `<platform>/<route>`, so naming the
# same site "Amazon" in one layer and "Unknown" in the other would file the same
# lesson under two keys and make both look unlearned.
_PLATFORM_BY_DOMAIN = (
    ("linkedin.com", "LinkedIn"),
    ("indeed.com", "Indeed"),
    ("amazon.jobs", "Amazon"),
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("myworkdayjobs.com", "Workday"),
    ("workday.com", "Workday"),
    ("ashbyhq.com", "Ashby"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("icims.com", "iCIMS"),
    ("workable.com", "Workable"),
    ("bamboohr.com", "BambooHR"),
)


def platform_for_url(url: str) -> str:
    """Best-effort platform label for a URL, used to bucket flywheel knowledge."""
    lowered = (url or "").lower()
    for domain, label in _PLATFORM_BY_DOMAIN:
        if domain in lowered:
            return label
    return "Unknown"


class BrowserBusy(RuntimeError):
    """Another process is driving the shared Chrome profile, so this refuses.

    Deliberately an error rather than a wait. The other driver holds the profile
    for as long as *its* browser is open, which may be hours -- the scheduled
    pass keeps one up between applications, and an interactive session keeps one
    up between questions. "Wait a moment" would be a lie, so the caller gets the
    holder's name and the ways out instead.
    """

    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(
            f"another process is driving the shared browser profile ({holder}). "
            "Two Chromes on one profile rewrite each other's cookies, so this "
            "refuses rather than launching a second one. Options: let it finish, "
            "stop it (`.venv/bin/python tools/cron_apply.py --stop`), or close "
            "this session's browser first with `browser_close`."
        )


class Runtime:
    """Holds the long-lived objects the tools operate on."""

    def __init__(self, data_dir: Optional[Path] = None):
        root = data_dir or (Path(__file__).parent.parent.parent.parent / "data")
        self.data_dir = Path(root)
        self.memory = MemoryStore(self.data_dir / "memory.json")
        self.guardrails = Guardrails(self.data_dir / "guard_state.json", memory=self.memory)
        self._browser: Optional[BrowserController] = None
        self._lock = asyncio.Lock()
        # Guards `data/browser-profile` against every other process on this
        # machine, not just against other calls in this one. Non-blocking: see
        # `BrowserBusy` for why waiting is not the honest answer.
        self._profile_lock = concurrency.FileLock(
            concurrency.browser_lock_path(self.data_dir),
            purpose="mcp browser",
            block=False,
        )

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def browser(self) -> Optional[BrowserController]:
        return self._browser

    @property
    def profile_lock(self) -> concurrency.FileLock:
        return self._profile_lock

    def profile_holder(self) -> str:
        """Who owns the browser profile right now, as one line."""
        return concurrency.describe_holder(self._profile_lock.path)

    def _claim_profile(self) -> None:
        """Take the profile lock, or explain who has it instead.

        The `held` short-circuit matters for a second launch after a close: the
        lock is ours for the server's lifetime, and asking the kernel again
        would be refused by our own descriptor.
        """
        if self._profile_lock.held:
            return
        if not self._profile_lock.acquire():
            raise BrowserBusy(self.profile_holder())

    async def get_browser(self) -> BrowserController:
        """Launch the browser on first use, then reuse it.

        The profile lock is claimed in the launch branch only -- see the module
        docstring for why an injected controller must not trigger it.
        """
        if self._browser is None or not self._browser.launched:
            self._claim_profile()
            try:
                controller = BrowserController(headless=False)
                await controller.launch()
            except BaseException:
                # A launch that never produced a browser must not leave the
                # profile claimed by a process with nothing to show for it.
                self._profile_lock.release()
                raise
            self._browser = controller
        return self._browser

    async def shutdown(self):
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        # Hand the profile back. Skipping this would leave the server holding
        # the lock for the rest of its life, and every scheduled pass in the
        # meantime would be refused -- a real cost, not a theoretical one. A
        # lock inherited from a supervisor releases as a no-op, which is right:
        # the supervisor is still driving.
        self._profile_lock.release()

    def current_platform(self) -> str:
        if self._browser is None or not self._browser.launched:
            return "Unknown"
        return platform_for_url(self._browser.page.url)


# One runtime per server process.
RUNTIME = Runtime()
