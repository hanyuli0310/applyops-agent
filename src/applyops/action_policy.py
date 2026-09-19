"""Deciding whether a click is allowed to leave this machine.

Nothing here touches Playwright; it takes facts already gathered from the DOM
and answers one question: **is this click the final external submit?**

Why the name of the button is not enough, and why it is still needed
---------------------------------------------------------------
The obvious implementation is::

    if "submit" in name.lower(): require_grant()

It fails in both directions. It misses ``<button type="submit">Save</button>``,
which submits the form and is called "Save". And it catches nothing useful
about ``Apply`` on LinkedIn, which merely opens the Easy Apply modal.

So the decision uses two independent signals:

1. **Structure** (authoritative). The element's own semantics: an
   ``<input type="submit">``, or a ``<button>`` whose *type* submits its form.
   A form's default button type is ``submit`` in every browser, so a button
   with no explicit type inside a form counts as submitting it. This catches the
   "Save" button.
2. **Name** (heuristic, generous). Words that mean "this is it": submit, send
   application, complete submission, finish, confirm. Anything matching is
   treated as final *even when it looks unusual*, because wrongly requiring a
   grant costs one extra prompt, while wrongly allowing a click puts an
   unauthorized application into an employer's inbox.

The asymmetry is deliberate. Failing toward "ask a human first" is recoverable;
failing toward "submitted without authorization" is not.

Anything that cannot be classified is not automatically safe -- but neither can
everything be blocked, since walking a multi-step form depends on clicking Next.
The dividing rule: **advancing a form is ordinary; ending it is not.** Words that
mean "keep going" (next, continue, review, upload, back) are explicitly ordinary
so that a form can still be walked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ClickClass(str, Enum):
    """What kind of consequence this click has."""

    FINAL_SUBMIT = "final_submit"
    ADVANCE = "advance"  # moves within the form; no external consequence
    NAVIGATE = "navigate"  # goes somewhere else in the app
    OTHER = "other"


#: Names that mean "the application is now leaving this machine".
FINAL_SUBMIT_PATTERNS = (
    r"\bsubmit\b",
    r"\bsubmission\b",
    r"submit\s+application",
    r"send\s+(my\s+)?application",
    r"\bsend\s+application\b",
    r"complete\s+(my\s+)?application",
    r"finish\s+(my\s+)?application",
    r"confirm\s+and\s+submit",
    r"review\s+and\s+submit",
    r"apply\s+for\s+this\s+job",
    r"提交(申请)?",
    r"応募する",
)

#: Names that mean "keep going inside the form". Listed explicitly so that a
#: generous final-submit heuristic can never strand a multi-step form midway.
ADVANCE_PATTERNS = (
    r"^\s*next\s*$",
    r"^\s*continue\s*$",
    r"^\s*review(\s+application)?\s*$",
    r"^\s*back\s*$",
    r"^\s*return\s*$",
    r"^\s*save\s+and\s+continue\s*$",
    r"^\s*upload\s*$",
    r"^\s*attach\s*$",
    r"^\s*add\s+another\s*$",
    r"^\s*ok\s*$",
    r"^\s*done\s*$",
    r"^\s*cancel\s*$",
)

_SUBMIT_TYPES = {"submit", "image"}


@dataclass(frozen=True)
class TargetFacts:
    """Everything DOM-side we were able to learn about the click target."""

    name: str = ""
    tag: str = ""
    input_type: str = ""
    role: str = ""
    submits_form: bool = False  # the element would submit its enclosing form
    href: str = ""
    ref: str = ""

    @property
    def label(self) -> str:
        return self.name or self.ref or "(unnamed)"


def looks_final_by_name(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in FINAL_SUBMIT_PATTERNS)


def looks_like_advance(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in ADVANCE_PATTERNS)


def classify(target: TargetFacts) -> ClickClass:
    """Classify a click target from its structure first, then its name."""
    tag = (target.tag or "").casefold()
    input_type = (target.input_type or "").casefold()

    # Structure wins: these elements submit whatever they live inside.
    if tag == "input" and input_type in _SUBMIT_TYPES:
        return ClickClass.FINAL_SUBMIT
    if target.submits_form:
        return ClickClass.FINAL_SUBMIT

    # Only now consider what it is called.  A button whose name says "submit" is
    # treated as final even though its type is button/<custom>, because plenty of
    # ATS forms drive their own POST from a JS handler.
    if looks_final_by_name(target.name):
        return ClickClass.FINAL_SUBMIT

    if looks_like_advance(target.name):
        return ClickClass.ADVANCE
    if target.role in {"link"} or target.href:
        return ClickClass.NAVIGATE
    return ClickClass.OTHER


@dataclass(frozen=True)
class ClickDecision:
    allowed: bool
    target_class: ClickClass
    reason: str = ""
    requires_grant: bool = False
    manual_required: bool = False

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "target_class": self.target_class.value,
            "reason": self.reason,
            "requires_grant": self.requires_grant,
            "manual_required": self.manual_required,
        }


def decide_click(
    target: TargetFacts,
    *,
    authorized: bool,
    route_supported: bool = True,
) -> ClickDecision:
    """Allow, demand a grant, or hand it to a human.

    `authorized` is true only when a *verified grant* is being consumed by the
    single submission path. Every other caller gets refused on a final submit,
    which is what closes the "just call click_target instead" hole.
    """
    target_class = classify(target)

    if target_class is not ClickClass.FINAL_SUBMIT:
        return ClickDecision(True, target_class)

    if authorized:
        if not route_supported:
            return ClickDecision(
                False,
                target_class,
                reason=(
                    "this route has no verified final action, so the submit cannot be "
                    "driven automatically. Finish it by hand in the open browser and "
                    "record the result afterwards."
                ),
                manual_required=True,
            )
        return ClickDecision(True, target_class, reason="grant verified")

    if not route_supported:
        return ClickDecision(
            False,
            target_class,
            reason=(
                f"{target.label!r} looks like the end of this application and the "
                "route is not a supported automation path. Finish it by hand."
            ),
            requires_grant=True,
            manual_required=True,
        )

    return ClickDecision(
        False,
        target_class,
        reason=(
            f"{target.label!r} is the final submit. It can only be performed by "
            "submit_final with a verified one-time grant bound to this job, this "
            "field snapshot and this resume."
        ),
        requires_grant=True,
    )
