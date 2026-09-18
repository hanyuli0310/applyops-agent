"""Playwright browser controller.

Launches the *real* Chrome binary (``channel="chrome"``) against a dedicated
profile rather than Playwright's bundled build. Two reasons:

* the user agent and fingerprint match a browser that actually exists, and
* cookies are encrypted with Chrome's own Keychain key, which is what makes the
  session-import helper (``tools/import_chrome_session.py``) work.

Every DOM interaction here also fixes a correctness bug, not just a detection
one. See the notes on ``fill_field`` and ``select_option``.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Locator, Page, async_playwright
from pydantic import BaseModel, Field

from . import locator
from .locator import FieldControl

# Chrome is the only browser we support: the session-import helper depends on
# its cookie encryption, and it is what most users actually browse in.
CHROME_CHANNEL = "chrome"

# Human-ish typing. Real key events are slower than `fill()`, and that is the
# point -- see `fill_field`.
TYPING_MIN_DELAY = 0.02
TYPING_MAX_DELAY = 0.09

# Pause after an action that may trigger navigation or a modal.
SETTLE_DELAY = 0.6


# ── Result models ────────────────────────────────────────────────────


class ClickResult(BaseModel):
    """Outcome of a click, including whether it opened a new tab."""

    clicked: bool = False
    new_tab: bool = False
    active_url: str = ""
    error: str = ""


class FillResult(BaseModel):
    ok: bool = False
    readback: str = ""  # what the page reports the value to be
    mismatch: bool = False
    error: str = ""


class SelectResult(BaseModel):
    ok: bool = False
    strategy: str = ""  # native_select | custom_control | option_text
    selected: str = ""
    error: str = ""


class TabInfo(BaseModel):
    index: int
    url: str
    title: str = ""
    active: bool = False


class PageButton(BaseModel):
    name: str = ""
    tag: str = "button"
    aria: str = ""


class PageState(BaseModel):
    """Structured representation of the current page."""

    url: str = ""
    title: str = ""
    text_content: str = ""
    form_fields: list[FieldControl] = Field(default_factory=list)
    buttons: list[PageButton] = Field(default_factory=list)
    tabs: list[TabInfo] = Field(default_factory=list)
    error: str = ""


# ── Controller ───────────────────────────────────────────────────────


class BrowserController:
    """Drives a headful Chrome with a persistent profile."""

    def __init__(
        self,
        headless: bool = False,
        user_data_dir: str | Path | None = None,
        channel: str = CHROME_CHANNEL,
    ):
        self.headless = headless
        self.channel = channel
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._user_data_dir = Path(
            user_data_dir or Path(__file__).parent.parent.parent / "data" / "browser-profile"
        )

    # ── lifecycle ────────────────────────────────────────────────────

    async def launch(self):
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._user_data_dir),
            channel=self.channel,
            headless=self.headless,
            viewport={"width": 1400, "height": 950},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def close(self):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._playwright = None

    @property
    def launched(self) -> bool:
        return self._page is not None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._context

    # ── navigation ───────────────────────────────────────────────────

    async def goto(self, url: str, settle: float = 1.0):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(settle)

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png", full_page=False)

    async def get_current_url(self) -> str:
        return self.page.url

    # ── tabs ─────────────────────────────────────────────────────────

    async def list_tabs(self) -> list[TabInfo]:
        tabs = []
        for index, page in enumerate(self.context.pages):
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append(
                TabInfo(index=index, url=page.url, title=title, active=page is self._page)
            )
        return tabs

    async def switch_tab(self, index: int) -> bool:
        pages = self.context.pages
        if index < 0 or index >= len(pages):
            return False
        self._page = pages[index]
        try:
            await self._page.bring_to_front()
        except Exception:
            pass
        return True

    async def _adopt_new_tabs(self, before: list[Page]) -> bool:
        """Switch to a tab that appeared while we were acting.

        LinkedIn's "Apply on company site" opens the employer's ATS in a new
        tab, so an agent that keeps staring at the original page silently loses
        the actual application. Following the new tab is required for
        correctness, not a nicety.
        """
        fresh = [page for page in self.context.pages if page not in before]
        if not fresh:
            return False
        self._page = fresh[-1]
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        return True

    # ── page state ───────────────────────────────────────────────────

    async def get_page_state(self) -> PageState:
        """Everything the caller needs to decide the next action.

        Field discovery goes through `locator`, not an injected DOM query,
        because the sites we care about render inside shadow roots.
        """
        try:
            fields = await locator.resolve_fields(self.page)
            buttons = await self._visible_buttons()
            try:
                text = await self.page.locator("body").inner_text(timeout=8000)
            except Exception:
                text = ""
            return PageState(
                url=self.page.url,
                title=await self.page.title(),
                text_content=text[:5000],
                form_fields=fields,
                buttons=buttons,
                tabs=await self.list_tabs(),
            )
        except Exception as exc:  # noqa: BLE001 - surface, do not crash the run
            return PageState(url=self.page.url, error=f"{type(exc).__name__}: {exc}")

    async def _visible_buttons(self) -> list[PageButton]:
        """Buttons and button-like links, addressed by accessible name.

        Uses Playwright locators rather than an injected DOM query, for the same
        reason `locator` does: LinkedIn's apply modal lives in a shadow root, and
        `document.querySelectorAll` cannot see into it. Reporting the page's
        buttons but not the modal's would hide "Submit application" from the
        caller entirely.

        CSS classes are useless here now (LinkedIn emits hashed names like
        `b486132d _50ad7bd2`), so the accessible name is the only durable handle.

        LinkedIn renders its primary CTAs as bare `<a>` elements that
        `a[role="button"]` does not match. Two distinct shapes have been
        measured on live pages, and each needs its own clause:

        * "Continue applying" (job-search safety reminder) -- an `<a>` with no
          `role` and no `aria-label` at all, distinguishable only by living
          inside the dialog. Hence `[role="dialog"] a`.
        * "Easy Apply to this job" (the posting's own apply CTA) -- an `<a>`
          with no `role` but *with* an `aria-label`, sitting in the page body
          rather than a dialog. `[role="dialog"] a` misses it, which is how the
          whole apply flow became unreachable. Hence `a[aria-label]`.

        An aria-label on an anchor is a deliberate accessibility name, so it
        marks an interactive control rather than ordinary navigation -- and the
        `header, nav` filter below removes the icon links that also carry one.
        `a[href*="openSDUIApplyFlow"]` is the narrow belt-and-braces clause for
        LinkedIn's apply entry point specifically. The ~48 role-less, label-less
        anchors on the same page stay excluded, which was the point.
        """
        out: list[PageButton] = []
        seen: set[str] = set()

        for frame in [self.page.main_frame, *[f for f in self.page.frames if f is not self.page.main_frame]]:
            try:
                candidates = frame.locator(
                    'button, a[role="button"], input[type="submit"], input[type="button"], '
                    '[role="dialog"] a, dialog a, a[aria-label], '
                    'a[href*="openSDUIApplyFlow"]'
                )
                count = await candidates.count()
            except Exception:
                continue

            for index in range(min(count, 200)):
                handle = candidates.nth(index)
                try:
                    if not await handle.is_visible():
                        continue
                    info = await handle.evaluate(
                        """
                        (el) => {
                          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                          // NOTE: `footer` is intentionally NOT filtered here.
                          // The apply modal keeps Next / Review / Submit in its
                          // own <footer>; filtering it would hide the very button
                          // the caller needs. Fields use a stricter filter.
                          if (el.closest && el.closest('header, nav')) return null;
                          const aria = norm(el.getAttribute('aria-label'));
                          const name = aria || norm(el.innerText) || norm(el.value);
                          if (!name) return null;
                          return { name: name.slice(0, 100), tag: el.tagName.toLowerCase(), aria };
                        }
                        """
                    )
                except Exception:
                    continue
                if not info:
                    continue
                name = info["name"]
                if name in seen:
                    continue
                seen.add(name)
                out.append(PageButton(name=name, tag=info["tag"], aria=info["aria"]))
        return out

    # ── actions ──────────────────────────────────────────────────────

    async def _resolve(self, ref: str) -> Locator:
        resolved = await locator.resolve_ref(self.page, ref)
        if resolved is None:
            raise ValueError(f"could not resolve field reference: {ref!r}")
        return resolved

    async def fill_field(self, ref: str, value: str) -> FillResult:
        """Type into a field one character at a time, then read the value back.

        `locator.fill()` assigns the value and fires a single synthetic input
        event. Two things go wrong with that:

        1. React's controlled inputs (and the shadow-DOM web components LinkedIn
           now uses) can ignore a value that arrived without real key events, so
           the page's own state keeps the old value -- the DOM string changes
           while the app thinks the field is empty, and the form submits stale
           data.
        2. A value that appears instantly, with no keystrokes, is an obvious
           automation tell.

        Real keystrokes fix both. The read-back is what makes the difference
        detectable rather than silent.
        """
        try:
            element = await self._resolve(ref)
            await element.click(timeout=10000)
            await element.press(_select_all_shortcut())
            await element.press("Delete")
            await asyncio.sleep(random.uniform(0.05, 0.15))
        except Exception as exc:
            return FillResult(ok=False, error=f"focus failed: {exc}")

        try:
            for char in value:
                await self.page.keyboard.type(char)
                await asyncio.sleep(random.uniform(TYPING_MIN_DELAY, TYPING_MAX_DELAY))
            # Commit: some components only publish their value on blur.
            await element.press("Tab")
            await asyncio.sleep(0.2)
        except Exception as exc:
            return FillResult(ok=False, error=f"typing failed: {exc}")

        readback = ""
        try:
            readback = (await element.input_value(timeout=3000)) or ""
        except Exception:
            # Not an <input>; treat a contenteditable's text as the read-back.
            try:
                readback = (await element.inner_text()) or ""
            except Exception:
                readback = ""

        mismatch = bool(readback) and readback.strip() != value.strip()
        return FillResult(ok=not mismatch, readback=readback, mismatch=mismatch)

    async def select_option(self, ref: str, value: str) -> SelectResult:
        """Choose an option, native `<select>` or a custom dropdown.

        The previous implementation called `page.select_option()` and stopped.
        That only works on a real `<select>`. Workday and Greenhouse both render
        dropdowns as `div[role=combobox]` over a popup list, where
        `select_option()` raises -- and the surrounding `except` then retried the
        same call and swallowed the failure, so the field silently stayed blank
        and the failure only surfaced at submit time.

        Three phases: try native, otherwise open the control and click the
        option by its visible text.
        """
        try:
            element = await self._resolve(ref)
        except Exception as exc:
            return SelectResult(ok=False, error=str(exc))

        tag = ""
        role = ""
        try:
            tag = (await element.evaluate("el => el.tagName.toLowerCase()")) or ""
            role = (await element.get_attribute("role")) or ""
        except Exception:
            pass

        # Phase 1 -- a real <select>.
        if tag == "select":
            for kwargs in ({"label": value}, {"value": value}):
                try:
                    await element.select_option(timeout=5000, **kwargs)
                    return SelectResult(ok=True, strategy="native_select", selected=value)
                except Exception:
                    continue

        # Phase 2 -- custom control: open it, then choose by visible text.
        try:
            await element.click(timeout=8000)
            await asyncio.sleep(0.4)
        except Exception as exc:
            return SelectResult(ok=False, error=f"could not open control: {exc}")

        # Some "dropdowns" are typeaheads: the control is a text input and the
        # list does not exist until there is a query. Clicking alone shows
        # nothing, so the search below would find no option to click and report
        # a match failure for a control that was simply still empty. LinkedIn's
        # Location (city) field is exactly this.
        if tag == "input":
            try:
                await element.press(_select_all_shortcut())
                await element.press("Delete")
                for char in value:
                    await self.page.keyboard.type(char)
                    await asyncio.sleep(TYPING_MIN_DELAY)
                await asyncio.sleep(0.6)
            except Exception:
                pass

        # A person writes "Sunnyvale, CA"; the suggestion reads "Sunnyvale,
        # California, United States". The full string is not a substring of the
        # suggestion, so the leading component is tried as well. Order matters:
        # the whole value is always attempted first, so a precise answer is never
        # downgraded to a looser match.
        queries = [value]
        head = value.split(",")[0].strip()
        if head and head.lower() != value.lower():
            queries.append(head)

        for query in queries:
            for candidate in (
                self.page.get_by_role("option", name=query, exact=False),
                self.page.locator(f'[role="option"]:has-text("{_css_escape_text(query)}")'),
                self.page.locator(f'li:has-text("{_css_escape_text(query)}")'),
                self.page.get_by_text(query, exact=False),
            ):
                try:
                    count = await candidate.count()
                    for index in range(min(count, 6)):
                        option = candidate.nth(index)
                        if await option.is_visible():
                            await option.click(timeout=5000)
                            return SelectResult(
                                ok=True,
                                strategy="custom_control",
                                selected=query,
                            )
                except Exception:
                    continue

        # Close the popup so it does not shadow later actions.
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        return SelectResult(ok=False, error=f"no visible option matched {value!r} (role={role})")

    async def set_checkbox(self, ref: str, checked: bool = True) -> bool:
        """Check or uncheck a control, tolerating a label-level reference.

        Radio groups often have framework-generated ids, so `locator` addresses
        them by label text. That resolves to the label rather than the input, and
        clicking a label still toggles the control it belongs to.
        """
        try:
            element = await self._resolve(ref)
        except Exception:
            return False

        try:
            current = await element.is_checked()
        except Exception:
            try:
                await element.click(timeout=8000)
                return True
            except Exception:
                return False

        if current == checked:
            return True
        try:
            await element.click(timeout=8000)
            return True
        except Exception:
            return False

    async def click(self, ref: str = "", name: str = "") -> ClickResult:
        """Click an element, following a new tab if one opens.

        Accepts either a field reference (from `get_page_state`) or an
        accessible name for a button/link.
        """
        pages_before = list(self.context.pages)
        try:
            if ref:
                target = await self._resolve(ref)
            elif name:
                target = await locator.find_button(self.page, name)
                if target is None:
                    return ClickResult(error=f"no visible button or link named {name!r}")
            else:
                return ClickResult(error="click requires either ref or name")
            await target.click(timeout=15000)
        except Exception as exc:
            return ClickResult(error=f"{type(exc).__name__}: {exc}")

        await asyncio.sleep(SETTLE_DELAY)
        new_tab = await self._adopt_new_tabs(pages_before)
        return ClickResult(clicked=True, new_tab=new_tab, active_url=self.page.url)

    async def upload_file(self, ref: str, file_path: str) -> bool:
        try:
            element = await self._resolve(ref)
            await element.set_input_files(file_path, timeout=15000)
            return True
        except Exception:
            return False

    # ── scrolling / waiting ──────────────────────────────────────────

    async def scroll(self, direction: str = "down", amount: float = 1.0):
        delta = amount if direction == "down" else -amount
        await self.page.evaluate(f"window.scrollBy(0, {delta} * window.innerHeight)")
        await asyncio.sleep(0.3)

    async def wait_for_navigation(self, timeout: float = 10.0):
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass

    async def wait_for_selector(self, selector: str, timeout: float = 10.0):
        await self.page.wait_for_selector(selector, timeout=timeout * 1000)

    async def wait_for_fields(self, timeout: float = 20.0, poll: float = 1.0) -> list[FieldControl]:
        """Wait until at least one answerable control exists.

        The apply modal mounts asynchronously, and a fixed sleep is a guess that
        is either too short (flaky) or too long (slow). Polling for the actual
        condition is both.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last: list[FieldControl] = []
        while asyncio.get_event_loop().time() < deadline:
            last = await locator.resolve_fields(self.page)
            if last:
                return last
            await asyncio.sleep(poll)
        return last


def _select_all_shortcut() -> str:
    return "Meta+a" if sys.platform == "darwin" else "Control+a"


def _css_escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
