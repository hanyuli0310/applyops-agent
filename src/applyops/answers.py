"""Scoped answers: answer once, reuse at exactly the right breadth.

Why scopes exist
----------------
The flywheel stored every answer globally, keyed on the question text. That is
right for "What is your phone number?" and dangerously wrong for:

- "Are you legally authorised to work in <country>?" -- same question, different
  answer depending on the employer's country;
- "Willing to relocate to <city>?" -- a company-scoped fact at best;
- anything attached to one specific application.

So answers live at one of three scopes, and resolution is always
**most-specific first**: application beats company beats global. A global answer
is only used when nothing more specific exists -- never the other way round.

Two more rules come from the plan and are enforced here rather than promised:

- **Similar text is reused only for explicit safe classes.** A small vocabulary
  (for example sponsorship, W-2 basis, or background checks) can match wording
  variants after one confirmed answer. Unclassified questions still require an
  exact match, so arbitrary prose is never fuzzy-matched.
- **Withdrawal is real.** Removing an answer bumps the store's revision, which
  is part of every submission grant's digest (`authorization.py`), so pending
  approvals stop verifying the moment the facts they were based on go away.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .concurrency import FileLock, atomic_write_json, data_lock_path, read_json


class AnswerScope(str, Enum):
    GLOBAL = "global"
    COMPANY = "company"
    APPLICATION = "application"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize(question: str) -> str:
    return " ".join(question.split()).casefold()


def tidy_question(question: str) -> str:
    """Strip the chrome a form wraps around a question.

    Two shapes show up on real forms and both break an exact-text lookup: the
    form appends its own required marker ("... Degree? Required"), and some
    frameworks render the same question twice with no separator at all
    ("... Degree?Have you completed ... Degree?"). Neither changes what is
    being asked, and a stored answer must still be found.

    Applied to lookups only, never to `_normalize`: the keys already on disk
    were computed with the current normalisation, so changing it would strand
    every answer already recorded.
    """
    text = " ".join((question or "").split())
    stripped = re.sub(r"\s*\bRequired\b\s*$", "", text, flags=re.IGNORECASE).strip()
    if stripped:
        text = stripped
    half = len(text) / 2
    if text and float(half).is_integer():
        if text[: int(half)].strip() == text[int(half) :].strip():
            text = text[: int(half)].strip()
    elif text and int(half) > 3:
        k = int(half)
        if text[:k].strip() == text[k:].strip():
            text = text[:k].strip()
    return text


def _question_key(question: str) -> str:
    return hashlib.sha256(_normalize(question).encode("utf-8")).hexdigest()[:16]


# A deliberately small, conservative vocabulary.  Only questions with an
# unambiguous safety class are reused across wording changes; everything else
# continues to require an exact question match.
_ANSWER_CLASS_RULES = (
    ("sponsorship", re.compile(r"\b(sponsor(?:ship)?|visa sponsorship|work visa)\b", re.I)),
    ("work_authorization", re.compile(r"(authorized|authorised|legally eligible|right)\s+to work", re.I)),
    ("relocation", re.compile(r"\b(relocat|willing to move|move to)\w*\b", re.I)),
    ("workplace_mode", re.compile(r"\b(on[- ]?site|remote|hybrid)\b", re.I)),
    ("compensation_w2", re.compile(r"\b(w[- ]?2|1099|wage basis|payroll basis)\b", re.I)),
    ("salary_expectation", re.compile(r"(expected|desired|target)\s+(salary|pay|compensation)|salary expectation", re.I)),
    ("background_check", re.compile(r"background check", re.I)),
    ("criminal_history", re.compile(r"(felony|criminal|conviction|misdemeanor)", re.I)),
)


def classify_question(question: str) -> str:
    """Return a conservative reusable answer class, or ``""``.

    Classes are intentionally explicit rather than a general fuzzy matcher:
    this lets a confirmed answer cover harmless wording variants without
    allowing an unrelated question to inherit it.
    """
    text = question.strip()
    for name, pattern in _ANSWER_CLASS_RULES:
        if pattern.search(text):
            return name
    return ""


@dataclass
class AnswerEntry:
    id: str
    question_key: str
    question: str
    answer: str
    scope: str
    company: str = ""
    application_id: str = ""
    answer_class: str = ""
    created_at: str = ""
    withdrawn_at: str = ""

    @property
    def active(self) -> bool:
        return not self.withdrawn_at

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class AnswerStore:
    """Scoped answers under one file, one lock, one revision counter."""

    def __init__(self, data_dir: str | Path, *, flywheel_path: str | Path | None = None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "scoped_answers.json"
        self._lock_path = data_lock_path(self.data_dir, "scoped_answers")
        # The flywheel's accumulated answers live in memory.json under
        # `learned_qa`, and answering a question from them used to be impossible
        # from here: this store only ever read scoped_answers.json, so a
        # question answered dozens of times ("Will you now or in the future
        # require sponsorship for employment visa status?") was reported as
        # unanswered on a form that merely worded it differently. They are read
        # through, never copied -- one source of truth, and no drift.
        self.flywheel_path = (
            Path(flywheel_path) if flywheel_path else self.data_dir / "memory.json"
        )

    def _flywheel_entries(self) -> list[AnswerEntry]:
        """Answers the flywheel has accumulated, as entries.

        Malformed rows are skipped rather than repaired: an answer that cannot
        be read whole is not an answer, and half-reading one is how a form gets
        a wrong value.
        """
        try:
            payload = read_json(self.flywheel_path, default={}) or {}
        except Exception:  # noqa: BLE001 - a missing flywheel must not break a fill
            return []
        rows = payload.get("learned_qa") or []
        entries: list[AnswerEntry] = []
        for row in rows:
            try:
                question = str(row.get("question") or "").strip()
                answer = str(row.get("answer") or "").strip()
                if not question or not answer:
                    continue
                entries.append(
                    AnswerEntry(
                        id=f"flywheel:{row.get('id') or _question_key(question)}",
                        question_key=_question_key(question),
                        question=question,
                        answer=answer,
                        scope=AnswerScope.GLOBAL.value,
                        answer_class=str(row.get("answer_class") or "")
                        or classify_question(question),
                        created_at=str(row.get("created_at") or ""),
                    )
                )
            except Exception:  # noqa: BLE001 - one bad row must not hide the rest
                continue
        return entries

    # ── storage ──────────────────────────────────────────────────────

    def _read(self) -> dict:
        payload = read_json(self.path, default={}) or {}
        # Flywheel entries come *first*, so a scoped answer to the same question
        # stays the last one in its scope and still wins the lookup below.
        entries = self._flywheel_entries()
        entries.extend(AnswerEntry(**e) for e in payload.get("entries", []))
        return {
            "entries": entries,
            "revision": payload.get("revision", "rev-0"),
        }

    def _write(self, entries: list[AnswerEntry], revision: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "entries": [e.to_dict() for e in entries],
                "revision": revision,
            },
        )

    @property
    def revision(self) -> str:
        """Changes on every write; part of submission-grant digests."""
        return self._read()["revision"]

    @staticmethod
    def _next_revision(revision: str) -> str:
        try:
            n = int(revision.rsplit("-", 1)[-1]) + 1
        except ValueError:
            n = 1
        return f"rev-{n}"

    # ── writing ──────────────────────────────────────────────────────

    def set_answer(
        self,
        question: str,
        answer: str,
        *,
        scope: AnswerScope | str = AnswerScope.GLOBAL,
        company: str = "",
        application_id: str = "",
        answer_class: str = "",
    ) -> AnswerEntry:
        """Store one answer at one scope. Re-setting the same scope replaces it.

        Scope boundaries are validated, not assumed: a company answer without a
        company, or an application answer without an application, is exactly the
        kind of sloppiness that later leaks one employer's answer to another.
        """
        scope = AnswerScope(scope)
        if scope is AnswerScope.COMPANY and not company.strip():
            raise ValueError("a company-scoped answer requires the company name")
        if scope is AnswerScope.APPLICATION and not application_id.strip():
            raise ValueError("an application-scoped answer requires the application id")
        if not answer.strip():
            raise ValueError("refusing to store an empty answer; withdraw instead")

        entry = AnswerEntry(
            id=str(uuid.uuid4()),
            question_key=_question_key(question),
            question=question.strip(),
            answer=answer,
            scope=scope.value,
            company=company.strip(),
            application_id=application_id.strip(),
            answer_class=answer_class.strip() or classify_question(question),
            created_at=_now(),
        )
        with FileLock(self._lock_path):
            state = self._read()
            entries = [
                e
                for e in state["entries"]
                if not (
                    e.active
                    and e.question_key == entry.question_key
                    and e.scope == entry.scope
                    and e.company == entry.company
                    and e.application_id == entry.application_id
                )
            ]
            entries.append(entry)
            new_revision = self._next_revision(state["revision"])
            self._write(entries, new_revision)
        entry_revision = new_revision
        assert entry_revision  # for readability; the revision is the point
        return entry

    def withdraw(self, entry_id: str) -> bool:
        """Retract one answer. Every grant minted while it was live stops applying."""
        with FileLock(self._lock_path):
            state = self._read()
            changed = False
            for entry in state["entries"]:
                if entry.id == entry_id and entry.active:
                    entry.withdrawn_at = _now()
                    changed = True
            if changed:
                self._write(state["entries"], self._next_revision(state["revision"]))
        return changed

    # ── reading ──────────────────────────────────────────────────────

    def resolve(
        self,
        question: str,
        *,
        company: str = "",
        application_id: str = "",
    ) -> AnswerEntry | None:
        """The answer to use, at the most specific scope that has one.

        Returns None when nothing applies. Exact text wins; a conservative
        semantic class is consulted only when the question belongs to one of
        the explicit reusable classes.
        """
        key = _question_key(question)
        reusable_class = classify_question(question)
        all_entries = [e for e in self._read()["entries"] if e.active]

        def from_entries(candidates: list[AnswerEntry]) -> AnswerEntry | None:
            by_scope = {
                AnswerScope.APPLICATION.value: [],
                AnswerScope.COMPANY.value: [],
                AnswerScope.GLOBAL.value: [],
            }
            for entry in candidates:
                by_scope[entry.scope].append(entry)
            if application_id:
                exact = [e for e in by_scope[AnswerScope.APPLICATION.value] if e.application_id == application_id]
                if exact:
                    return exact[-1]
            if company:
                exact = [e for e in by_scope[AnswerScope.COMPANY.value] if e.company.casefold() == company.casefold()]
                if exact:
                    return exact[-1]
            if by_scope[AnswerScope.GLOBAL.value]:
                return by_scope[AnswerScope.GLOBAL.value][-1]
            return None

        # Exact question text is always authoritative, even if a class match
        # exists at a more-specific scope.
        exact = from_entries([e for e in all_entries if e.question_key == key])
        if exact is not None:
            return exact

        # Same question, minus the form's own chrome. A key computed on the
        # tidied text, plus a comparison of tidied text on both sides, so an
        # entry stored *with* the noise is found by a lookup without it and the
        # other way round.
        tidied = tidy_question(question)
        if tidied:
            if tidied != " ".join((question or "").split()):
                exact = from_entries(
                    [e for e in all_entries if e.question_key == _question_key(tidied)]
                )
                if exact is not None:
                    return exact
            exact = from_entries(
                [e for e in all_entries if tidy_question(e.question) == tidied]
            )
            if exact is not None:
                return exact

        # Only fall back to a known semantic class when no exact entry exists.
        # Generic questions have no class and therefore still require a human.
        if reusable_class:
            return from_entries([e for e in all_entries if e.answer_class == reusable_class])
        return None

    def entries(self, *, active_only: bool = True) -> list[AnswerEntry]:
        entries = self._read()["entries"]
        if active_only:
            return [e for e in entries if e.active]
        return entries
