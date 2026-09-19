"""Result evidence: what a page must say before anyone believes it worked.

Kept apart from both the MCP tools and the service because *every* driver of a
submission -- harness, batch runner, local UI -- must ask the same question with
the same vocabulary. Two modules each guessing their own success phrases is how
one of them ends up calling a page that says nothing a confirmation.
"""

from __future__ import annotations

from .action_policy import ClickClass, decide_click
from .browser import BrowserController
from .platforms.naming import platform_for_url
from .submission import FinalAction

# The demo ATS is recognised by its own URL, never by guessing at a hostname.
DEMO_HOSTS = ("127.0.0.1", "localhost")

#: What a page has to say before we believe an application reached anyone.
#: Per platform because each employer writes a different sentence, and a single
#: global guess would either miss everything or accept anything. Empty means:
#: this route has no verified final action, so its result can only ever be
#: "unknown" -- which is a far better answer than "success".
SUCCESS_EVIDENCE = {
    "DemoATS": ("Application received",),
    "LinkedIn": ("application was sent", "application sent", "successfully applied"),
    "Greenhouse": ("application submitted", "thanks for applying"),
    "Lever": ("application submitted", "thanks for applying"),
    "Workday": ("application submitted", "thank you for applying"),
}


def evidence_platform(url: str) -> str:
    """Which success vocabulary applies to this page."""
    lowered = (url or "").lower()
    if any(host in lowered for host in DEMO_HOSTS):
        return "DemoATS"
    return platform_for_url(url)


def success_patterns_for(url: str) -> tuple[str, ...]:
    return SUCCESS_EVIDENCE.get(evidence_platform(url), ())


async def detect_final_action(
    browser: BrowserController, *, ref: str = "", name: str = ""
) -> tuple[FinalAction | None, str]:
    """Find the control that genuinely ends this application.

    Detection rather than trust: the caller does not get to nominate any button
    it likes, because nominating "Submit" is how a partial form gets sent. When a
    candidate is supplied it still has to classify as a final submit; otherwise
    the page itself is scanned and an ambiguous result is reported, never guessed
    between.
    """
    candidates: list[tuple[str, str]] = []  # (ref, name)

    if ref or name:
        facts, error = await browser.inspect_target(ref=ref, name=name)
        if facts is None:
            return None, error
        if decide_click(facts, authorized=True).target_class is ClickClass.FINAL_SUBMIT:
            candidates.append((ref, name))
        else:
            other = decide_click(facts, authorized=True).target_class.value
            return None, (
                f"{facts.label!r} is not a final submit control (classified {other})"
            )
    else:
        state = await browser.get_page_state()
        for button in state.buttons:
            facts, _ = await browser.inspect_target(name=button.name)
            if facts is None:
                continue
            if (
                decide_click(facts, authorized=True).target_class
                is ClickClass.FINAL_SUBMIT
            ):
                candidates.append((facts.ref, button.name))

    if not candidates:
        return None, (
            "no final submit control found on this page. Either the form is not "
            "at its last step, or this route's final action is unknown -- in "
            "which case the application must be finished by hand."
        )
    if len(candidates) > 1:
        return None, (
            "more than one final submit control found "
            f"({', '.join(n for _, n in candidates)}); refusing to guess which "
            "one ends the application. Pass final_ref explicitly."
        )

    found_ref, found_name = candidates[0]
    return (
        FinalAction(
            ref=found_ref,
            name=found_name,
            success_patterns=success_patterns_for(browser.page.url),
        ),
        "",
    )
