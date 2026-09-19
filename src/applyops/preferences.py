"""Job preferences: the rules that decide what gets queued, and why.

Two things this module refuses to do:

- **Refuse silently.** Every evaluation returns the reasons, in the words the UI
  can show a person. "Filtered" with no explanation is how a user concludes the
  tool is broken rather than that their rule was too strict.
- **Match loosely.** Titles are matched on whole tokens, not substrings, so
  "intern" does not match "internal tools" -- a substring match here would quietly
  exclude postings the user wanted, and they would never see why.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .concurrency import FileLock, atomic_write_json, data_lock_path, read_json


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class JobPreferences:
    """What the user is looking for. Empty lists mean "no opinion"."""

    target_titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict) -> JobPreferences:
        known = {k: v for k, v in payload.items() if k in cls.__annotations__}
        return cls(**known)


class PreferenceStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "job_preferences.json"
        self._lock_path = data_lock_path(self.data_dir, "job_preferences")

    def get(self) -> JobPreferences:
        return JobPreferences.from_dict(read_json(self.path, default={}) or {})

    def set(self, prefs: JobPreferences) -> JobPreferences:
        prefs.updated_at = _now()
        with FileLock(self._lock_path):
            atomic_write_json(self.path, prefs.to_dict())
        return prefs


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+#]+", (text or "").casefold()) if t}


def evaluate(
    *,
    title: str,
    company: str = "",
    location: str = "",
    prefs: JobPreferences,
) -> dict:
    """Keep or filter one posting, with the reason in every case."""
    reasons: list[str] = []
    title_tokens = _tokens(title)

    if prefs.exclude_companies:
        blocked = [c for c in prefs.exclude_companies if c.strip().casefold() in (company or "").casefold()]
        if blocked:
            return {
                "keep": False,
                "reasons": [f"company matches an exclusion rule: {blocked[0]}"],
            }

    excluded = [k for k in prefs.exclude_keywords if k.strip().casefold() in (title or "").casefold()]
    if excluded:
        return {"keep": False, "reasons": [f"title contains an excluded keyword: {excluded[0]}"]}

    if prefs.target_titles:
        # Token-subset match: every word of the target title has to appear in the
        # posting title, so "backend engineer" does not match "frontend engineer"
        # and "intern" never matches "internal tools".
        subset_matches = [
            t for t in prefs.target_titles if _tokens(t) and _tokens(t).issubset(title_tokens)
        ]
        if not subset_matches:
            return {
                "keep": False,
                "reasons": [
                    (
                        "title does not match any target title "
                        f"({', '.join(prefs.target_titles)})"
                    )
                ],
            }
        reasons.append(f"title matches target: {subset_matches[0]}")

    if prefs.locations:
        wanted = [
            loc for loc in prefs.locations if loc.strip().casefold() in (location or "").casefold()
        ]
        if not wanted:
            return {
                "keep": False,
                "reasons": [
                    (
                        f"location {location!r} is outside the wanted locations "
                        f"({', '.join(prefs.locations)})"
                    )
                ],
            }
        reasons.append(f"location matches: {wanted[0]}")

    if prefs.include_keywords:
        hits = [k for k in prefs.include_keywords if k.strip().casefold() in (title or "").casefold()]
        if not hits:
            return {
                "keep": False,
                "reasons": [
                    (
                        "none of the required keywords appear in the title "
                        f"({', '.join(prefs.include_keywords)})"
                    )
                ],
            }
        reasons.append(f"keyword present: {hits[0]}")

    if not reasons:
        reasons.append("no rules configured; everything is queued")
    return {"keep": True, "reasons": reasons}
