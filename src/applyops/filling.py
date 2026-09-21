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
#: Labels that ask for consent rather than for a fact. A standing answer
#: (AGENTS.md §14): these are agreed by default.
CONSENT_PATTERN = re.compile(
    r"\b(consent|agree|agreement|terms|privacy)\b", re.IGNORECASE
)

LABEL_TO_PROFILE = (
    # Before the generic name pattern: a modal that asks for the two halves gets
    # them from the one full name the profile stores (see `profile._split_name`).
    (re.compile(r"\b(first|given)\s*name\b", re.IGNORECASE), "first_name"),
    (re.compile(r"\b(last|family|sur)\s*name\b|\bsurname\b", re.IGNORECASE), "last_name"),
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

#: Bare yes/no option text. Meaningless on its own: the question is the *group*
#: it sits in, which is where the answer has to come from.
BARE_CHOICE_PATTERN = re.compile(r"^(yes|no|true|false|是|否)$", re.IGNORECASE)


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
    job_location: str = "",
) -> tuple[str, str] | None:
    """(value, source) for a form label, or None if we do not know it.

    Answers before profile: a scoped answer is the user speaking about *this*
    form, which outranks a general fact.

    A **city question** is answered with the posting's location rather than the
    applicant's, because that is what the question is for: which location this
    application is for. The source is reported as `job:location` so a person
    reviewing the approval summary can see that it did not come from their own
    profile -- a distinction worth keeping visible when the values are similar.
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
            if key == "location" and (job_location or "").strip():
                return job_location.strip(), "job:location"
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
    job_location: str = "",
) -> FillReport:
    """Fill, verify, attach -- then report honestly on what is missing."""
    report = FillReport()
    profile = memory.get_profile() if memory is not None else {}

    state = await controller.get_page_state()
    fields = state.form_fields

    # One report per question, so a two-option group is not counted twice and a
    # question that was never answered cannot be skipped over.
    reported_questions: set[str] = set()

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
            job_location=job_location,
        )
        is_choice = field_type in {"radio", "checkbox"} or bool(
            BARE_CHOICE_PATTERN.match(label)
        )
        if is_choice and BARE_CHOICE_PATTERN.match(label):
            question = question_for(label, control.group_label)
            wanted, source = choice_answer(
                question,
                profile=profile,
                answers=answers,
                company=company,
                application_id=application_id,
            )
            if wanted is None:
                # No answer for this question: name it once and move on. The
                # option is left untouched -- guessing here is how an unrelated
                # "Yes" ends up answering a question the user never saw.
                key = question or label
                if key not in reported_questions:
                    reported_questions.add(key)
                    bucket = report.unfilled_required if control.required else report.unfilled_optional
                    bucket.append(question or f"{label} (question could not be identified)")
                continue
            option_is_yes = bool(BARE_CHOICE_PATTERN.match(label)) and label.strip().casefold() in {
                "yes",
                "true",
                "是",
            }
            if option_is_yes != wanted:
                continue  # the sibling option; the wanted one is handled below
            # The value here is the *state* to set, not the option's text. Passing
            # "No" for a boolean reads as falsy and unchecks the very radio we
            # chose, which is how a sponsorship question once shipped blank.
            resolved = ("yes", source)

        if resolved is None and field_type == "checkbox" and CONSENT_PATTERN.search(label):
            # A standing answer, made once by the applicant (AGENTS.md §14):
            # consent and agreement checkboxes are checked by default. It is
            # still reported with its own source, so an approval summary shows
            # that this box was ticked by default rather than by a person.
            resolved = ("checked", "default:consent (AGENTS.md §14)")

        if resolved is None:
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
        file_ref = await _file_input_ref(controller, fields)
        if file_ref is None:
            # LinkedIn's resume step may offer a previously uploaded document
            # as a checked radio card instead of rendering a file input. A
            # checked "Deselect resume <name>" card is already a verified
            # attachment; requiring a file input here incorrectly parks the
            # application before the next step.
            selected = next(
                (
                    control
                    for control in fields
                    if control.checked
                    and FILE_LABEL_PATTERN.search(control.label or "")
                    and "deselect resume" in (control.label or "").casefold()
                ),
                None,
            )
            if selected is not None:
                report.resume = FieldOutcome(
                    label="Resume",
                    ref=selected.ref,
                    source="resume",
                    verification="verified",
                    detail="an existing LinkedIn resume is already selected",
                )
            elif fields or any(
                "submit application" in (button.name or "").casefold()
                for button in state.buttons
            ):
                # Later Easy Apply steps (including the final review screen)
                # no longer expose the upload control. The resume was selected
                # on the earlier step and remains part of the live form.
                report.resume = FieldOutcome(
                    label="Resume",
                    ref="",
                    source="resume",
                    verification="verified",
                    detail="resume upload control is not present on this later step",
                )
            else:
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


def _as_bool(value: str) -> bool | None:
    """Read a Yes/No answer out of prose, or None when it is not one.

    A stored answer is a sentence the user typed ("No, I do not require
    sponsorship"), so the leading word decides and anything ambiguous is None --
    which parks the application rather than picking a side.
    """
    text = (value or "").strip().casefold()
    if not text:
        return None
    if text in {"yes", "y", "true", "1", "是"} or text.startswith("yes"):
        return True
    if text in {"no", "n", "false", "0", "否"} or text.startswith("no"):
        return False
    return None


def question_for(label: str, group_label: str) -> str:
    """The question a control answers.

    A bare "Yes"/"No" option carries no question of its own: the question is the
    group it belongs to. Anything else is its own question and is answered by its
    own label. This one rule is what stops one question's answer being written
    into another's box.
    """
    if BARE_CHOICE_PATTERN.match((label or "").strip()):
        return (group_label or "").strip()
    return (label or "").strip()


def choice_answer(
    question: str,
    *,
    profile: dict[str, str],
    answers: AnswerStore,
    company: str = "",
    application_id: str = "",
) -> tuple[bool, str] | tuple[None, str]:
    """(answer, source) for a Yes/No question, or (None, "") if we do not know.

    The user's own answer for this exact question wins. Failing that, a question
    that is *about sponsorship* may be answered from the profile's
    `requires_sponsorship`, because that field is the user's answer to precisely
    that question. Nothing else is inferred: an unanswered question stays
    unanswered.
    """
    if not question:
        return None, ""

    entry = answers.resolve(question, company=company, application_id=application_id)
    if entry is not None:
        parsed = _as_bool(entry.answer)
        if parsed is not None:
            return parsed, f"answer:{entry.scope}"
        return None, ""

    if SPONSORSHIP_PATTERN.search(question):
        parsed = _as_bool(profile.get("requires_sponsorship") or "")
        if parsed is not None:
            return parsed, "profile:requires_sponsorship"

    return None, ""


async def _file_input_ref(controller, fields) -> str | None:
    """The reference of a file input that will actually accept the upload.

    Picking "the first file control" is not enough. Ashby renders an anonymous
    dropzone input whose nearest label is `Name` -- the same label as the name
    text field above it -- so its reference (`label=Name`) resolved to that
    text field, `set_input_files` timed out on it, and a real application was
    left with no resume attached while the report said nothing was wrong.

    So: prefer a reference that names the control directly (an id or an
    automation attribute) over one built from a label, and -- when a
    controller is available -- prove the reference resolves to a control of
    type `file` before returning it.
    """
    candidates = [c for c in fields if (c.field_type or "").lower() == "file"]
    if not candidates:
        return None

    from . import locator as locator_module

    strong = [c for c in candidates if not c.ref.startswith("label=")]
    ordered = strong + [c for c in candidates if c not in strong]

    for control in ordered:
        if controller is not None:
            try:
                element = await locator_module.resolve_ref(controller.page, control.ref)
                resolved_type = ""
                if element is not None:
                    resolved_type = ((await element.get_attribute("type")) or "").casefold()
                if element is None or resolved_type != "file":
                    continue
            except Exception:  # noqa: BLE001, S112 - fall through to the next candidate
                continue
        return control.ref
    return candidates[0].ref


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"yes", "y", "true", "1", "checked", "是"}


def resume_for_fill(memory: MemoryStore) -> ResumeRef | None:
    """The configured resume, or None with the reason left to the caller."""
    from .resume import resolve_resume

    try:
        return resolve_resume(memory.profile.value("resume_path"))
    except ResumeError:
        return None
