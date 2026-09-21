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
from urllib.parse import parse_qs, urljoin, urlparse

from .action_policy import looks_final_by_name
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


#: Hosts that belong to the site we are standing on, not to the employer.
_SITE_HOSTS = ("linkedin.com",)


def is_employer_destination(destination: str, current_host: str) -> bool:
    """Whether this destination leads to the employer, rather than deeper in.

    Deliberately not "any host but ours". LinkedIn's footer is full of links to
    `business.linkedin.com`, `safety.linkedin.com` and friends, and they appear
    *before* the posting's apply control in the DOM. Verified live: the detection
    step found the employer at `yoailabs.careers-page.com`, the follow step
    picked the earlier footer link instead, and the walk stayed on LinkedIn with
    the two halves of the same decision disagreeing.
    """
    host = host_of(destination)
    if not host or host == (current_host or "").lower():
        return False
    bare = host.removeprefix("www.")
    return not any(bare == site or bare.endswith(f".{site}") for site in _SITE_HOSTS)


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
        destination = urljoin(controller.page.url, unwrap(href)) if href else ""
        if is_employer_destination(destination, current_host):
            return host_of(destination)
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
    """Go to the employer's own application, and return (landing url, label).

    Deliberately narrow: it follows only a control that leads to the *employer*
    (`is_employer_destination`) -- never one that stays on this site, and never
    one of the site's own footer links. A control named like a final submit is
    skipped too, so this cannot walk past the end of an application.

    It navigates to the control's `href` rather than clicking it: verified live
    that a scripted click on LinkedIn's "Apply" does nothing at all -- no
    navigation, no new tab, no modal -- while going to the href it names opens
    the employer's page directly. A control with no href is still clicked, since
    that is the only thing a real button can be driven by.
    """
    current_host = host_of(controller.page.url)
    controls = await controller.page.evaluate(_COLLECT_APPLY_JS)
    for control in controls or []:
        label = str(control.get("text", ""))
        if not _looks_like_apply(label) or looks_final_by_name(label):
            continue
        href = str(control.get("href") or "")
        destination = urljoin(controller.page.url, unwrap(href)) if href else ""
        if destination and not is_employer_destination(destination, current_host):
            continue

        if destination:
            try:
                await controller.goto(destination, settle=2.5)
            except Exception:  # noqa: BLE001, S112 - an unreachable target is data
                continue
            return controller.page.url, label

        pages_before = list(controller.context.pages)
        element = await _element_for(controller, label, href)
        if element is None:
            continue
        try:
            await element.click(timeout=15000, no_wait_after=True)
        except Exception:  # noqa: BLE001, S112 - an unclickable control is not a crash
            continue
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


async def open_onsite_application(controller: BrowserController) -> tuple[str, str]:
    """Follow a *same-site* apply control to the form it opens.

    The other half of the same problem. A real LinkedIn Easy Apply posting puts
    the form one click away (`<a href="/jobs/view/<id>/apply/">Easy Apply</a>`)
    on its own host -- no redirect, no modal, and nothing on the posting page for
    the filler to see. Preparing without following it parks the application under
    "no file input found on this form", which is what happened on a live run.

    Only ever called when the page in front of us has no form, so there is
    nothing a click could submit; and a control whose name reads like a final
    submit is skipped regardless, which is the belt to that structural braces.
    """
    from .action_policy import looks_final_by_name

    current_host = host_of(controller.page.url)
    controls = await controller.page.evaluate(_COLLECT_APPLY_JS)
    for control in controls or []:
        label = str(control.get("text", ""))
        if not _looks_like_apply(label) or looks_final_by_name(label):
            continue
        href = str(control.get("href") or "")
        # Relative hrefs (`/apply`) are the common case here and carry no host of
        # their own: resolving them against the current page is what stops a
        # same-site link being mistaken for the off-site case and skipped.
        destination = urljoin(controller.page.url, unwrap(href)) if href else ""
        if destination and is_employer_destination(destination, current_host):
            continue  # that is the off-site case, handled by follow_offsite_apply
        if href and destination:
            # Navigate rather than click. Verified live: LinkedIn's "Easy Apply"
            # is an `<a href=".../apply/?openSDUIApplyFlow=true">` whose scripted
            # click does nothing at all -- no navigation, no new tab, no modal --
            # while going to the href it names opens the application straight
            # away. An href is a destination; using it is both more reliable and
            # less like poking at a live page.
            try:
                await controller.goto(destination, settle=2.5)
            except Exception:  # noqa: BLE001, S112 - an unreachable target is data
                continue
            return controller.page.url, label

        element = await _element_for(controller, label, href)
        if element is None:
            continue
        try:
            await element.click(timeout=15000, no_wait_after=True)
        except Exception:  # noqa: BLE001, S112 - an unclickable control is not a crash
            continue
        await asyncio.sleep(1.2)
        return controller.page.url, label
    return "", ""


async def has_apply_control(controller: BrowserController) -> bool:
    """Whether the page offers a control whose job is to open the application.

    The count of controls on a page is not the question: a real LinkedIn posting
    has three (its own search boxes) and no form. This asks the specific
    question -- is there something here that opens an application, and is it not
    a submit button in disguise.
    """
    from .action_policy import looks_final_by_name

    try:
        controls = await controller.page.evaluate(_COLLECT_APPLY_JS)
    except Exception:  # noqa: BLE001 - an unreadable page offers nothing
        return False
    return any(
        _looks_like_apply(str(c.get("text", "")))
        and not looks_final_by_name(str(c.get("text", "")))
        for c in controls or []
    )


#: Words a site uses when it wants a person to sign in before the form.
_SIGN_IN_WORDS = ("sign in", "sign-in", "log in", "login", "create an account")


async def sign_in_wall(controller: BrowserController) -> str:
    """The sign-in prompt this page is showing, or "".

    Some employer systems put an account between the posting and the form. That
    is not an empty form to keep poking at -- the password field and the wording
    are right there, and a person has to do this part.
    """
    try:
        found = await controller.page.evaluate(
            """() => {
              const hasPassword = !!document.querySelector('input[type=password]');
              const text = (document.body ? document.body.innerText : '').toLowerCase();
              return { hasPassword, text: text.slice(0, 4000) };
            }"""
        )
    except Exception:  # noqa: BLE001 - an unreadable page claims nothing
        return ""
    if not isinstance(found, dict):
        return ""
    words = next(
        (word for word in _SIGN_IN_WORDS if word in str(found.get("text", ""))), ""
    )
    if found.get("hasPassword") and words:
        return words
    return ""
