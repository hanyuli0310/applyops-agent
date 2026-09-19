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

- **Similar text is not the same semantics.** Matching is on the normalised
  question text only; "Do you now require sponsorship?" and "Will you in the
  future require sponsorship?" stay different entries. Anyone can batch-answer
  the same question for several employers; nothing merges different questions.
- **Withdrawal is real.** Removing an answer bumps the store's revision, which
  is part of every submission grant's digest (`authorization.py`), so pending
  approvals stop verifying the moment the facts they were based on go away.
"""

from __future__ import annotations

import hashlib
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


def _question_key(question: str) -> str:
    return hashlib.sha256(_normalize(question).encode("utf-8")).hexdigest()[:16]


@dataclass
class AnswerEntry:
    id: str
    question_key: str
    question: str
    answer: str
    scope: str
    company: str = ""
    application_id: str = ""
    created_at: str = ""
    withdrawn_at: str = ""

    @property
    def active(self) -> bool:
        return not self.withdrawn_at

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class AnswerStore:
    """Scoped answers under one file, one lock, one revision counter."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "scoped_answers.json"
        self._lock_path = data_lock_path(self.data_dir, "scoped_answers")

    # ── storage ──────────────────────────────────────────────────────

    def _read(self) -> dict:
        payload = read_json(self.path, default={}) or {}
        return {
            "entries": [AnswerEntry(**e) for e in payload.get("entries", [])],
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

        Returns None when nothing applies. There is no fuzzy match: two questions
        that differ by one word get different keys, which is the entire defence
        against answering this year's sponsorship question with last year's.
        """
        key = _question_key(question)
        entries = [
            e
            for e in self._read()["entries"]
            if e.active and e.question_key == key
        ]
        by_scope: dict[str, list[AnswerEntry]] = {
            AnswerScope.APPLICATION.value: [],
            AnswerScope.COMPANY.value: [],
            AnswerScope.GLOBAL.value: [],
        }
        for entry in entries:
            by_scope[entry.scope].append(entry)

        if application_id:
            exact = [
                e for e in by_scope[AnswerScope.APPLICATION.value]
                if e.application_id == application_id
            ]
            if exact:
                return exact[-1]
        if company:
            exact = [
                e for e in by_scope[AnswerScope.COMPANY.value]
                if e.company.casefold() == company.casefold()
            ]
            if exact:
                return exact[-1]
        if by_scope[AnswerScope.GLOBAL.value]:
            return by_scope[AnswerScope.GLOBAL.value][-1]
        return None

    def entries(self, *, active_only: bool = True) -> list[AnswerEntry]:
        entries = self._read()["entries"]
        if active_only:
            return [e for e in entries if e.active]
        return entries
