"""Verification semantics for form actions.

M1 invariant: **an action that was not observed to take effect is not success.**

The previous logic was one line::

    mismatch = bool(readback) and readback.strip() != value.strip()
    ok = not mismatch

`bool(readback)` short-circuits, so every case where the page reported nothing
back -- a number input rejecting text, a component that publishes its value only
through a framework the DOM does not reflect, a read that timed out -- scored
`ok=True`. The read-back exists to make failure *detectable*; treating an absent
read-back as agreement makes it undecidable instead, which is strictly worse
than failing loudly.

So verification is three-valued, and it **fails closed**: anything that is not
observed to match is either a mismatch or unverifiable, never a success.

| verdict | meaning | must the caller treat it as done? |
|---|---|---|
| `verified` | the page reports what we asked for | yes |
| `mismatch` | the page reports something else | no -- it is not filled |
| `unverifiable` | we could not find out | no -- it is not filled |

The comparison lives in this module, away from Playwright, precisely because
this is the rule that needs testing. An integration test through a browser is
the proof that the wiring works; these functions are the proof that the rule is
right.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verification(str, Enum):
    """Three outcomes, not two. There is no 'probably'."""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Outcome:
    """What one action did, as far as the page will tell us."""

    verification: Verification
    expected: str = ""
    observed: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verification is Verification.VERIFIED

    @property
    def failed(self) -> bool:
        return self.verification is Verification.MISMATCH

    def as_dict(self) -> dict:
        return {
            "verification": self.verification.value,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


#: Differences we are willing to call the same value.
#:
#: Trimming and collapsing internal whitespace: pages routinely re-render a
#: value with spacing that differs from what was typed, and a form input that
#: shows "Jane  Doe" for "Jane Doe" has the value we intended.
#:
#: Anything else -- case, digits, punctuation, currency symbols -- is data. A
#: phone number that came back reformatted is a *different string* going onto a
#: real application, so it is a mismatch and must be shown to the user.
def _normalize(value: str) -> str:
    return " ".join(value.split())


def verify_text_value(
    expected: str,
    observed: str | None,
    *,
    readable: bool = True,
) -> Outcome:
    """Compare what was typed against what the field reports.

    `observed` is ``None`` when the value could not be read at all; `readable`
    says whether the read itself succeeded. They are separate inputs because a
    field that read fine and reported an empty string is a *different* case from
    a field we never got to read:

    - expected non-empty, observed empty  -> `unverifiable`: the field did not
      take the value, and we must not claim it did.
    - read failed entirely                -> `unverifiable` for the same reason.
    - expected empty (clearing a field),
      observed empty                      -> `verified`: clearing succeeded.
    """
    if not readable or observed is None:
        return Outcome(
            Verification.UNVERIFIABLE,
            expected,
            None,
            "could not read the field back; the value is not confirmed",
        )

    expected_n = _normalize(expected)
    observed_n = _normalize(observed)

    if expected_n == "":
        # Clearing. Anything left in the field means it did not clear.
        if observed_n == "":
            return Outcome(Verification.VERIFIED, expected, observed)
        return Outcome(
            Verification.MISMATCH,
            expected,
            observed,
            "field was expected to be empty, page still reports a value",
        )

    if observed_n == "":
        return Outcome(
            Verification.UNVERIFIABLE,
            expected,
            observed,
            "page reported nothing back for a non-empty value; treat as unfilled",
        )

    if expected_n != observed_n:
        return Outcome(
            Verification.MISMATCH,
            expected,
            observed,
            "page reports a different value",
        )

    return Outcome(Verification.VERIFIED, expected, observed)


def verify_choice(
    expected: str,
    observed: str | None,
    *,
    readable: bool = True,
) -> Outcome:
    """Compare a dropdown/radio selection against what the control reports.

    Looser than `verify_text_value` in exactly one dimension: **case**. Option
    labels are categorical, and a page that renders "United States" for the
    value "united states" has selected the row we meant. Every other difference
    is still a mismatch -- different job levels are different answers.
    """
    if not readable or observed is None:
        return Outcome(
            Verification.UNVERIFIABLE,
            expected,
            None,
            "could not read the selection back; the choice is not confirmed",
        )

    expected_n = _normalize(expected).casefold()
    observed_n = _normalize(observed).casefold()

    if expected_n and observed_n == "":
        return Outcome(
            Verification.UNVERIFIABLE,
            expected,
            observed,
            "control reports no selection; treat as unanswered",
        )
    if expected_n == "" and observed_n == "":
        return Outcome(Verification.VERIFIED, expected, observed)
    if expected_n != observed_n:
        return Outcome(
            Verification.MISMATCH, expected, observed, "control reports a different option"
        )
    return Outcome(Verification.VERIFIED, expected, observed)


def verify_boolean_state(
    expected: bool,
    observed: bool | None,
    *,
    readable: bool = True,
) -> Outcome:
    """Compare a checkbox/radio against its own checked state.

    There is no "we could not tell" for a checkbox we *can* read: `is_checked()`
    answers definitively. Unreadable still means unverifiable.
    """
    if not readable or observed is None:
        return Outcome(
            Verification.UNVERIFIABLE,
            str(expected),
            None,
            "could not read the checkbox state; it is not confirmed",
        )
    if bool(expected) != bool(observed):
        return Outcome(
            Verification.MISMATCH,
            str(expected),
            str(observed),
            "checkbox is not in the requested state",
        )
    return Outcome(Verification.VERIFIED, str(expected), str(observed))


@dataclass(frozen=True)
class ObservedFile:
    """One attachment the page reports on a file input."""

    name: str = ""
    size: int | None = None


def verify_upload(
    expected_name: str,
    observed: list[ObservedFile] | None,
    *,
    readable: bool = True,
    expected_size: int | None = None,
) -> Outcome:
    """Confirm the file the *user chose* is the file the page holds.

    A browser page can already carry an attachment before we touch it: LinkedIn
    keeps the last resume selected, and ATS forms re-populate a previously
    parsed file after navigating back. Silently trusting that attachment is how
    the wrong resume -- an old version, or another candidate's file on a shared
    machine -- goes out under the user's name.

    So the rule is: no observed file is unverifiable, a different filename is a
    mismatch, and only an exact filename match counts as verified. Size is
    checked when the page exposes it, as a cheap guard against two versions of
    a file sharing a name.
    """
    if not readable or not observed:
        return Outcome(
            Verification.UNVERIFIABLE,
            expected_name,
            None,
            "page reports no attachment; the upload is not confirmed",
        )

    names = [f.name for f in observed]
    if expected_name not in names:
        return Outcome(
            Verification.MISMATCH,
            expected_name,
            ", ".join(n for n in names if n) or "(unnamed)",
            "page holds a file other than the one selected",
        )

    if expected_size is not None:
        matching = next((f for f in observed if f.name == expected_name), None)
        if matching is not None and matching.size is not None and matching.size != expected_size:
            return Outcome(
                Verification.MISMATCH,
                expected_name,
                f"{matching.name} ({matching.size} bytes)",
                "same filename, different size: likely a different revision",
            )

    return Outcome(Verification.VERIFIED, expected_name, ", ".join(n for n in names if n))
