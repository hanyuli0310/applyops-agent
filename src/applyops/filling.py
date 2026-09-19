"""Filling a form from what we actually know -- and reporting what we do not.

The M1 flow could verify a value once someone typed it, but nothing typed it:
`prepare` read the form and asked for approval of an *empty* form. This module
closes that gap, and every choice in it is about the same rule:

**Fill what is knowable, verify every write, and park anything else.**

Where a value can come from, most specific first:

1. **Scoped answers** for this application (M4's store) -- an answer the user
   gave for this posting;
2. **Company-scoped**, then **global** learned answers;
3. **Profile fields**, matched by a normalised form label.

What is *not* here is any form of inference. A label the map does not recognise
does not get a best guess; it goes into `unfilled_required` and the application
waits for a human. That is the difference between "we prepared the form" and
"we invented the form".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .answers import AnswerStore
from .browser import BrowserController
from .memory import MemoryStore
from .resume import ResumeError, ResumeRef

#: Form label -> profile key. Deliberately explicit and small: a fuzzy matcher
#: here would be a machine for putting one field's value into another's box.
LABEL_TO_PROFILE = (
    (re.compile(r"full\s*name|^name$|your name", re.IGNORECASE), "name"),
    (re.compile(r"e-?mail", re.IGNORECASE), "email"),
    (re.compile(r"phone|mobile|telephone", re.IGNORECASE), "phone"),
    (re.compile(r"years?\s+of\s+experience|experience.*years", re.IGNORECASE), "years_experience"),
    (re.compile(r"current\s+(job\s+)?title|job\s+title", re.IGNORECASE), "current_title"),
    (re.compile(r"current\s+(company|employer)", re.IGNORECASE), "current_company"),
    (re.compile(r"location|city", re.IGNORECASE), "location"),
    (re.compile(r"(expected\s+)?salary|compensation", re.IGNORECASE), "expected_salary"),
)

#: Labels whose answer is "which of these options", resolved through answers
#: rather than invented. Kept separate so the reason a field is unresolved is
#: legible in the report.
SPONSORSHIP_PATTERN = re.compile(r"sponsor", re.IGNORECASE)

FILE_LABEL_PATTERN = re.compile(r"resume|cv\b|curriculum", re.IGNORECASE)

#: Bare yes/no option labels. Meaningless alone -- the question lives in the page
#: text -- so they are only resolved when the page itself asks about sponsorship.
BARE_CHOICE_PATTERN = re.compile(r"^(yes|no)$", re.IGNORECASE)


@dataclass
class FieldOutcome:
    label: str
    ref: str
    source: str  # profile | answer:<scope> | resume | (empty when unfilled)
    verification: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class FillReport:
    filled: list[FieldOutcome] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    unfilled_optional: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    mismatched: list[FieldOutcome] = field(default_factory=list)
    resume: FieldOutcome | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Only a form with nothing missing, nothing unreadable and nothing that
        read back wrong may be sent for approval."""
        return not (self.unfilled_required or self.unreadable or self.mismatched or self.problems)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "filled": [f.to_dict() for f in self.filled],
            "unfilled_required": self.unfilled_required,
            "unfilled_optional": self.unfilled_optional,
            "unreadable": self.unreadable,
            "mismatched": [f.to_dict() for f in self.mismatched],
            "resume": self.resume.to_dict() if self.resume else None,
            "problems": self.problems,
        }


def resolve_value(
    label: str,
    *,
    profile: dict[str, str],
    answers: AnswerStore,
    company: str = "",
    application_id: str = "",
) -> tuple[str, str] | None:
    """(value, source) for a form label, or None if we do not know it.

    Answers before profile: a scoped answer is the user speaking about *this*
    form, which outranks a general fact.
    """
    question = label.strip()
    if question:
        entry = answers.resolve(
            question, company=company, application_id=application_id
        )
        if entry is not None:
            return entry.answer, f"answer:{entry.scope}"

    for pattern, key in LABEL_TO_PROFILE:
        if pattern.search(label or ""):
            value = (profile.get(key) or "").strip()
            if value:
                return value, f"profile:{key}"
            return None
    return None


async def fill_application_form(
    controller: BrowserController,
    *,
    memory: MemoryStore,
    answers: AnswerStore,
    resume: ResumeRef | None,
    application_id: str = "",
    company: str = "",
) -> FillReport:
    """Fill, verify, attach -- then report honestly on what is missing."""
    report = FillReport()
    profile = memory.get_profile() if memory is not None else {}

    state = await controller.get_page_state()
    fields = state.form_fields

    # Read once: a bare "Yes"/"No" option is only answerable in context, and the
    # context is the question the page prints above it.
    try:
        page_text = (await controller.page.inner_text("body")).casefold()
    except Exception:  # noqa: BLE001 - context is optional, absence is handled
        page_text = ""

    sponsorship_choice = _sponsorship_choice(
        page_text=page_text,
        profile=profile,
        answers=answers,
        company=company,
        application_id=application_id,
    )

    for control in fields:
        label = (control.label or "").strip()
        field_type = (control.field_type or "").lower()
        ref = control.ref

        if field_type == "file" or FILE_LABEL_PATTERN.search(label):
            continue  # attachments are handled below, with their own verification

        resolved = resolve_value(
            label,
            profile=profile,
            answers=answers,
            company=company,
            application_id=application_id,
        )
        is_bare_choice = bool(BARE_CHOICE_PATTERN.match(label))
        if is_bare_choice and "sponsor" in page_text:
            # A yes/no group is one question with two buttons. Only the option
            # the user's own answer selects is clicked; the sibling is skipped
            # silently rather than reported as a second missing answer.
            if sponsorship_choice and label.strip().casefold() == sponsorship_choice:
                # The value here is the *state* to set, not the option's text.
                # Passing "No" for a boolean would read as falsy and uncheck the
                # very radio we chose -- which is how this shipped a form whose
                # sponsorship question was silently unanswered.
                resolved = ("yes", "profile:requires_sponsorship")
            else:
                continue

        if resolved is None:
            # Sponsorship is the one label worth naming specially: it is the
            # question whose wrong answer has real consequences, so the report
            # should say it was left unanswered rather than quietly unfilled.
            if is_bare_choice and "sponsor" in page_text:
                # The page asks about sponsorship and nothing answers it: report
                # the group once, by the question rather than by two options.
                if "sponsorship question" not in report.unfilled_required:
                    report.unfilled_required.append("sponsorship question")
                continue
            target = (
                report.unfilled_required
                if (control.required or SPONSORSHIP_PATTERN.search(label))
                else report.unfilled_optional
            )
            target.append(label or ref)
            continue

        value, source = resolved

        if field_type in {"radio", "checkbox"}:
            outcome = await controller.set_checkbox(ref, _truthy(value))
            filled = FieldOutcome(label=label, ref=ref, source=source,
                                  verification=outcome.verification, detail=outcome.detail)
        elif field_type in {"select", "combobox"} or control.options:
            outcome = await controller.select_option(ref, value)
            filled = FieldOutcome(label=label, ref=ref, source=source,
                                  verification=outcome.verification, detail=outcome.detail)
        else:
            outcome = await controller.fill_field(ref, value)
            filled = FieldOutcome(label=label, ref=ref, source=source,
                                  verification=outcome.verification, detail=outcome.detail)

        report.filled.append(filled)
        if filled.verification == "unverifiable":
            report.unreadable.append(label or ref)
        elif filled.verification == "mismatch":
            report.mismatched.append(filled)

    # The attachment: only ever the configured resume, verified after upload.
    if resume is not None:
        file_ref = _file_input_ref(fields)
        if file_ref is None:
            report.problems.append("no file input found on this form")
        else:
            upload = await controller.upload_file(file_ref, str(resume.path))
            report.resume = FieldOutcome(
                label="Resume",
                ref=file_ref,
                source="resume",
                verification=upload.verification,
                detail=upload.detail,
            )
            if upload.verification == "verified":
                # A page that already held a different file must not be treated
                # as "our upload worked" -- verify_upload compares the name and size.
                placed = upload.attachments == [resume.filename]
                if not placed:
                    report.mismatched.append(report.resume)
            elif upload.verification == "mismatch":
                report.mismatched.append(report.resume)
            else:
                report.unreadable.append("Resume")
    else:
        report.problems.append("no resume configured")

    return report


def _sponsorship_choice(
    *,
    page_text: str,
    profile: dict[str, str],
    answers: AnswerStore,
    company: str = "",
    application_id: str = "",
) -> str | None:
    """Which yes/no option the user's own answer selects, or None.

    Only consulted when the page itself asks about sponsorship, because a bare
    "Yes"/"No" carries no meaning without its question. A scoped answer wins over
    the profile -- it is the user speaking about this form.
    """
    if "sponsor" not in page_text:
        return None

    entry = answers.resolve(
        "Do you require sponsorship?", company=company, application_id=application_id
    ) or answers.resolve("Visa sponsorship", company=company, application_id=application_id)
    if entry is not None:
        value = entry.answer.strip().casefold()
    else:
        value = (profile.get("requires_sponsorship") or "").strip().casefold()
    if not value:
        return None

    # "No, I do not require sponsorship" -> pick "No". The negations are listed
    # because a stored answer is prose, not a boolean.
    wants_yes = value in {"yes", "y", "true", "1", "是"} or value.startswith("yes")
    if value.startswith("no") or value in {"n", "false", "0", "否"}:
        wants_yes = False
    return "yes" if wants_yes else "no"


def _file_input_ref(fields) -> str | None:
    for control in fields:
        if (control.field_type or "").lower() == "file":
            return control.ref
        if FILE_LABEL_PATTERN.search(control.label or ""):
            return control.ref
    return None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"yes", "y", "true", "1", "checked", "是"}


def resume_for_fill(memory: MemoryStore) -> ResumeRef | None:
    """The configured resume, or None with the reason left to the caller."""
    from .resume import resolve_resume

    try:
        return resolve_resume(memory.profile.value("resume_path"))
    except ResumeError:
        return None
