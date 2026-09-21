"""Where an application actually happens.

The route is decided from the URL we discovered the posting at, and for LinkedIn
that URL is always `linkedin.com/jobs/view/<id>` -- so a posting whose Apply
control leaves for Greenhouse is filed as `easy_apply` and counted as drivable.
The queue then fills nothing (there is nothing to fill) and honestly reports...
"no file input found on this form".

The signal that says otherwise is on the page, and only on the page: the apply
control. For the real non-Easy-Apply case LinkedIn wraps the destination in its
own redirect, which makes the destination readable:

    <a href="https://www.linkedin.com/safety/go/?url=<encoded ATS url>">Apply</a>

This module reads that control and answers one question: does this page host the
application, or point somewhere else that does?
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from .browser import BrowserController

#: Query parameters sites use to wrap an outbound link. `url` is LinkedIn's
#: (`/safety/go/?url=...`); the others are common enough to be worth unwrapping.
_REDIRECT_PARAMS = ("url", "u", "target", "redirect", "continue")

_APPLY_LABELS = (
    "apply",
    "apply now",
    "apply for this job",
    "apply on company site",
    "apply externally",
    "easy apply",
)

#: Read every apply-ish control with its destination. Deliberately returns all of
#: them: a page can offer both ("Easy Apply" and "Apply on company site").
_COLLECT_APPLY_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    const text = ((el.innerText || el.getAttribute('aria-label') || '') + '').trim();
    if (!text) continue;
    const lowered = text.toLowerCase().replace(/\\s+/g, ' ');
    if (lowered.length > 40) continue;
    out.push({ text: text.slice(0, 60), href: el.getAttribute('href') || '', tag: el.tagName });
  }
  return out;
}
"""


def _looks_like_apply(text: str) -> bool:
    lowered = (text or "").strip().casefold()
    return any(lowered == label or lowered.startswith(label) for label in _APPLY_LABELS)


def unwrap(href: str) -> str:
    """The real destination behind a redirect wrapper, or the href unchanged."""
    if not href:
        return ""
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    for param in _REDIRECT_PARAMS:
        values = query.get(param)
        if values and values[0].startswith(("http://", "https://")):
            return values[0]
    return href


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


async def offsite_apply_host(controller: BrowserController) -> str:
    """The foreign host the application lives on, or "" when it lives here.

    Only a control that *leaves* this site counts: an in-page "Easy Apply"
    button, or a link to another page of the same site, is not this problem.
    """
    current_host = host_of(controller.page.url)
    try:
        controls = await controller.page.evaluate(_COLLECT_APPLY_JS)
    except Exception:  # noqa: BLE001 - a page we cannot read is not a claim about it
        return ""
    for control in controls or []:
        if not _looks_like_apply(str(control.get("text", ""))):
            continue
        href = str(control.get("href") or "")
        destination = host_of(unwrap(href))
        if destination and destination != current_host:
            return destination
    return ""


async def form_control_count(controller: BrowserController) -> int:
    """How many answerable controls the page has.

    A cheap count for the "is there a form here at all?" question. Asking the
    locator for a full snapshot instead would read every field's value and
    resolve every control, which is real work on the shared prepare path -- and
    on a page with no form it is work spent to learn there is nothing to read.
    """
    from .locator import CONTROL_SELECTOR

    try:
        return int(
            await controller.page.evaluate(
                "sel => document.querySelectorAll(sel).length", CONTROL_SELECTOR
            )
        )
    except Exception:  # noqa: BLE001 - an unreadable page is not an empty one
        return 1


async def follow_offsite_apply(controller: BrowserController) -> tuple[str, str]:
    """Click the off-site apply control and return (landing url, clicked label).

    Deliberately narrow: it clicks only a control that *leaves this site*, matched
    the same way `offsite_apply_host` matches. A button called "Submit
    application" is never this, and a link that stays on this host is not this
    either -- so this cannot walk past the end of an application.
    """
    current_host = host_of(controller.page.url)
    controls = await controller.page.evaluate(_COLLECT_APPLY_JS)
    for control in controls or []:
        label = str(control.get("text", ""))
        if not _looks_like_apply(label):
            continue
        href = str(control.get("href") or "")
        destination = host_of(unwrap(href))
        if not destination or destination == current_host:
            continue

        pages_before = list(controller.context.pages)
        element = await _element_for(controller, label, href)
        if element is None:
            continue
        await element.click(timeout=15000, no_wait_after=True)
        await controller._adopt_new_tabs(pages_before)
        await asyncio.sleep(0.6)
        return controller.page.url, label
    return "", ""


async def _element_for(controller: BrowserController, label: str, href: str):
    """The clickable element for a control we found by reading the page."""
    from .locator import find_button, resolve_ref

    if href:
        element = await resolve_ref(controller.page, f"css=a[href=\"{href}\"]")
        if element is not None:
            return element
    return await find_button(controller.page, label)
