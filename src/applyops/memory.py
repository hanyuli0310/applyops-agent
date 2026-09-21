"""Guided Memory Store — the data flywheel.

Four compounding layers:

1. **Profile** — static candidate facts (name, email, resume path, …).
2. **Learned Q&A** — every answer the human ever gave, scored by *confidence*.
   Answers above the confidence threshold are auto-filled silently; answers that
   keep failing get demoted and eventually re-asked, so bad memories decay.
3. **Platform knowledge** — which DOM selectors actually worked on each ATS.
   Seeded with human priors, then refined by real runs (hits / misses).
4. **Apply-route knowledge** — the *shape* of an application on each route: how
   many screens, what stands before the first field, which gate needs a human.
   Layer 3 assumes the form lives on the page you already opened; this layer is
   what covers the applications where it does not.

The flywheel loop:

    application run → answers & selectors are used
                    → run outcome is attributed back to them (success / failure)
                    → confidence rises or falls
                    → high-confidence knowledge is injected into the next run's prompt
                    → next run is faster, needs fewer human interruptions

Every layer is persisted to a single JSON file and is backwards compatible with
older memory files (new fields fall back to defaults).

Several processes write this file at once -- the MCP server, the scheduled
pass, a manual run -- so the write path is a **locked merge**, not a dump of
whatever this process happens to remember. See `_save` and `_absorb`. Two
properties come out of that, and they are the reason it is built this way:

* Nothing that is a *record* is ever lost. Application history, routes,
  platforms and learned answers are merged by identity, so a process that never
  saw them still writes them back.
* A *counter* can come out low, never high. Where two writers disagree, the
  larger value wins -- undercounting makes the flywheel more cautious than it
  needs to be, while inventing evidence would make it confidently wrong.
  `merge_conflicts` records how often that happened.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, model_validator

from . import concurrency
from .platforms.detector import detect_platform
from .profile import PROFILE_FILENAME, ProfileStore, migrate_legacy_profile

# Answers at or above this confidence are auto-filled without asking the human.
AUTO_FILL_THRESHOLD = 0.60
# Below this confidence the memory is considered unreliable and gets pruned from
# the prompt instead of cluttering it.
TRUST_FLOOR = 0.25
# How many successful deployments before the stability bonus saturates.
USAGE_RAMP = 5
# How strongly a single outcome can move trust. Higher = faster adaptation,
# lower = stickier memory. 0.35 means ~3 contradictions retire an answer.
TRUST_ALPHA = 0.35
# Trust a brand-new answer starts with when it was derived rather than stated.
INITIAL_TRUST = 0.70
# Trust a brand-new answer starts with when the user stated it themselves.
# Higher than INITIAL_TRUST because a direct statement is the strongest evidence
# this system can ever get: there is nothing to infer and nobody to ask again.
USER_ANSWER_TRUST = 0.80
# Bumped whenever MemoryData gains fields that need backfilling on load.
#   3: job_id / apply_route / ats / vision_fallbacks, plus the flywheel gap log.
#   4: routes -- the apply-route knowledge base (see RouteKnowledge). Additive:
#      the field defaults empty and is re-seeded from _SEED_ROUTES on load, so a
#      schema-3 file opens without losing anything.
#   5: merge_conflicts -- how often two processes disagreed about the same fact,
#      now that the write path merges instead of overwriting. Additive: it
#      defaults to 0 and needs no backfill. The version also became monotone at
#      this point (a save writes `max(mine, on-disk)`), so a process running the
#      older code can no longer roll the file back to 3 -- which is how the
#      route layer was deleted once already.
SCHEMA_VERSION = 5

_STOPWORDS = frozenset(
    """a an the is are was were be been being am do does did doing have has had
    you your yours we our us i me my it its this that these those to of in on at
    for with by from as if then than so and or but not no yes can could would
    should will shall may might must about into over under again further once
    here there when where why how what which who whom any all some each few more
    most other such only own same too very s t just don now please provide""".split()
)

# Suffixes stripped from content words before building the dedup key, longest
# first so the most specific rule wins. This is deliberately *only* applied to
# the dedup key — retrieval scoring still uses the raw words — so that
# morphological variants of one question ('expected' / 'expectation') collapse
# into a single memory instead of being asked again, without loosening how we
# match a question to a *different* question.
_SUFFIXES = (
    "ations", "ation", "ities", "ingly", "edly", "ness", "ance", "ence",
    "ment", "ing", "ed", "es", "ly", "ity", "s",
)
# Shortest stem we will accept; below this the "suffix" is more likely to be
# part of the word than a grammatical ending.
_MIN_STEM_LEN = 4


def _stem(word: str) -> str:
    """Very light suffix stripping, enough to merge question variants."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM_LEN:
            return word[: -len(suffix)]
    return word


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_question(question: str) -> str:
    """Reduce a question to its discriminating content words.

    'What is your expected salary range?' -> 'expect range salary'
    Two questions that normalize to the same string are treated as the same
    question, so light stemming is applied first and reworded duplicates such as
    'Salary range expectation?' land on the same key.
    """
    words = re.findall(r"\w+", question.lower())
    content = [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1]
    # Stable, order-insensitive key so reworded duplicates collapse together.
    return " ".join(sorted(set(content))) or " ".join(words[:5])


def question_keywords(question: str) -> set[str]:
    """Content words of a question, used for fast local recall."""
    words = re.findall(r"\w+", question.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


# Posting URLs carry a pile of tracking parameters (`trk`, `refId`, `trackingId`),
# so one posting has many distinct URLs. Deduplicating on the raw URL therefore
# misses obvious repeats, which is how the same job gets applied to twice.
_JOB_ID_PATTERNS = (
    re.compile(r"[?&]currentJobId=(\d+)"),
    re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)"),
    re.compile(r"[?&]gh_jid=(\d+)"),
    re.compile(r"/jobs/(\d+)"),
)


def extract_job_id(url: str) -> str:
    """Best-effort stable job identifier for a posting URL, or '' if unknown."""
    if not url:
        return ""
    for pattern in _JOB_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def dedup_key(url: str, job_id: str = "") -> str:
    """The value applications are deduplicated on.

    Prefers the platform-level job id, falling back to the URL with its query
    string stripped. The job id is extracted here rather than left to callers,
    because a caller that forgets would silently get weaker deduplication.
    """
    resolved = job_id or extract_job_id(url)
    if resolved:
        return f"id:{resolved}"
    if not url:
        return ""
    return f"url:{url.split('?', 1)[0].rstrip('/').lower()}"


# ── Models ───────────────────────────────────────────────────────────


class QAEntry(BaseModel):
    """A learned question-answer pair with confidence tracking."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    answer: str
    context: str = ""
    source: str = "user"  # user | derived
    key: str = ""  # normalized question key for dedup

    times_used: int = 0  # answer was surfaced / filled
    success_count: int = 0  # the run it was used in succeeded
    failure_count: int = 0  # the run it was used in failed
    asked_count: int = 0  # we had to go back to the human for it
    trust: float = 0.70  # EWMA of recent outcomes — decays toward the truth
    # Someone actually said this answer out loud, as opposed to us inferring it.
    # Kept separate from `times_used` so that counter keeps meaning "deployments"
    # and the stats stay honest -- see `_evidence_units`.
    confirmed_by_human: bool = False

    created_at: str = Field(default_factory=_now)
    learned_at: str = Field(default_factory=_now)
    last_used_at: str = ""
    last_asked_at: str = ""

    def model_post_init(self, __context):
        if not self.key:
            self.key = normalize_question(self.question)

    @property
    def attempts(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        """Raw lifetime success rate, kept for display and debugging."""
        return self.success_count / self.attempts if self.attempts else 0.0

    def _reinforce(self, positive: bool, alpha: float = TRUST_ALPHA):
        """Move trust toward the latest evidence.

        An EWMA is used rather than a lifetime average on purpose: a memory that
        used to work but stopped working must fall below the auto-fill threshold
        quickly, or the agent would keep silently sending a stale answer.
        """
        signal = 1.0 if positive else 0.0
        self.trust = round(alpha * signal + (1.0 - alpha) * self.trust, 4)

    @property
    def _evidence_units(self) -> int:
        """How many independent pieces of evidence stand behind this answer.

        A deployment counts, and so does the user having stated the answer: that
        is evidence in its own right, not a promise of future use.

        Without the second term the "ask once" promise quietly becomes "ask
        twice" for every brand-new question, because a fresh entry scores
        0.8 * 0.70 = 0.56, under the 0.60 auto-fill threshold -- so the answer
        the user just gave would be handed back as a mere suggestion the next
        time the same question appears, forever.
        """
        return max(self.times_used, 1 if self.confirmed_by_human else 0)

    @property
    def confidence(self) -> float:
        """0..1 trust score. Drives whether we auto-fill or ask.

        Recent evidence dominates (80%); repeated successful deployment adds a
        smaller stability bonus (20%) so a long-proven answer outranks a fresh one.
        """
        if self._evidence_units == 0:
            # Never deployed and never stated — unproven, promotable but not
            # auto-ready.
            return round(0.8 * self.trust, 4)
        usage_ramp = min(self._evidence_units / USAGE_RAMP, 1.0)
        return round(0.8 * self.trust + 0.2 * usage_ramp, 4)

    @property
    def is_auto_ready(self) -> bool:
        return self.confidence >= AUTO_FILL_THRESHOLD and self._evidence_units > 0

    def mark_used(self):
        self.times_used += 1
        self.last_used_at = _now()

    def mark_asked(self):
        self.asked_count += 1
        self.last_asked_at = _now()


class SelectorStat(BaseModel):
    """How well one CSS selector has performed for one role on one platform."""

    selector: str
    hits: int = 0
    misses: int = 0

    @property
    def attempts(self) -> int:
        return self.hits + self.misses

    @property
    def score(self) -> float:
        """Laplace-smoothed success rate — an untried prior starts at 0.5."""
        return (self.hits + 1.0) / (self.attempts + 2.0)


class PlatformKnowledge(BaseModel):
    """Everything we know about automating one ATS."""

    platform: str
    selectors: dict[str, list[SelectorStat]] = Field(default_factory=dict)
    runs: int = 0
    successes: int = 0
    notes: str = ""

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0


class RouteStep(BaseModel):
    """One action in an application flow.

    Deliberately coarse. The purpose is not to replay a recording; it is to tell
    the next caller what this *kind* of application is made of -- how many
    screens stand between the posting and the receipt, and which of them no
    machine can pass alone.
    """

    ordinal: int
    kind: str  # open | click | fill | upload | verify | otp | submit
    detail: str
    selector: str = ""
    human_required: bool = False


class RouteKnowledge(BaseModel):
    """What it takes to get from a posting to a submitted application, on one
    route of one platform.

    The selector memory answers "what does the button look like". This answers
    "what is the shape of the journey" -- a different question, and the one that
    Easy-Apply-only thinking was missing. A LinkedIn Easy Apply never leaves the
    page; an `external_ats` posting hands the browser to the employer's own
    account system, where a login -- often an emailed one-time code -- stands
    *before* the first field. Knowing that up front is the difference between
    asking the human once and failing at a wall you did not know was there.

    `runs` / `successes` / `blocked_at` are the flywheel: every real attempt
    votes, and `blocked_at` records the step it died on, so the route's own
    history names the gate that deserves the next adapter.
    """

    platform: str
    route: str
    entry_signature: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[RouteStep] = Field(default_factory=list)
    runs: int = 0
    successes: int = 0
    #: Every blockage ever recorded on this route, counted. This is the raw
    #: evidence and it is never edited in place: dropping an entry here is what
    #: used to be undone by the next save, because the merge rebuilds it from
    #: disk by taking the largest count.
    blockage_counts: dict[str, int] = Field(default_factory=dict)
    #: Gates this route has been *proven* to pass, mapped to the count they were
    #: retracted at. A retraction forgives every blockage up to that count and
    #: no later one, so a gate that blocks again simply outgrows it.
    retracted_at: dict[str, int] = Field(default_factory=dict)
    #: Derived: `blockage_counts` minus what has been retracted. Read this, and
    #: never treat a retracted gate as a wall -- a route that has been walked to
    #: the end does not stop at a door it has already opened.
    blocked_at: dict[str, int] = Field(default_factory=dict)
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def _read_older_shapes(cls, data):
        """Read the shapes this record has had, rather than only the latest.

        Two shape changes are absorbed here, and both matter for the same
        reason: a memory file that cannot be parsed is treated as no memory at
        all, which drops every accumulated answer and every recorded route in a
        single read.

        `retracted_at` was briefly a list of gate names -- read as "retracted,
        forgiving everything counted so far". And `blocked_at` used to *be* the
        count, before the raw counts were split out: files written then have no
        `blockage_counts`, and seeding from `blocked_at` is what keeps a save
        from recomputing the derived value out of an empty record and
        discarding every gate the route was known to die at.

        Done on the whole record rather than per field because the two fields
        are read against each other, and pydantic validates them in
        declaration order.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get("blockage_counts"):
            data["blockage_counts"] = dict(data.get("blocked_at") or {})
        retracted = data.get("retracted_at")
        if isinstance(retracted, list):
            # "Forgiving everything counted so far" -- the count it was made at
            # is what a later blockage has to outgrow.
            counts = data["blockage_counts"]
            data["retracted_at"] = {
                gate: counts.get(gate, 0) for gate in retracted if isinstance(gate, str)
            }
        return data

    @model_validator(mode="after")
    def _derive_blocked(self):
        _recompute_blocked(self)
        return self

    @property
    def key(self) -> str:
        return f"{self.platform}/{self.route}"

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def human_gates(self) -> list[str]:
        """Steps that require the human, in order -- what to warn about early."""
        return [s.detail for s in self.steps if s.human_required]

    @property
    def hardest_gate(self) -> str:
        """The step this route most often dies on -- the next adapter's target.

        Empty until the route has actually failed somewhere; "no evidence" and
        "failed nowhere" must not look alike.
        """
        if not self.blocked_at:
            return ""
        return max(self.blocked_at.items(), key=lambda kv: kv[1])[0]


class ApplicationRecord(BaseModel):
    """Record of a past job application.

    `job_id` is what deduplication keys on -- see `extract_job_id`. `apply_route`
    records *how* the application was submitted (Easy Apply, the employer's own
    ATS, …) and `vision_fallbacks` how much visual guessing that route needed,
    which is the signal that tells us where the next adapter is missing.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_url: str
    job_id: str = ""
    job_title: str = ""
    company: str = ""
    platform: str = ""
    ats: str = ""  # greenhouse | lever | workday | ... "" for native apply
    apply_route: str = ""  # easy_apply | external | unknown
    status: str = "applied"  # applied, failed, paused, unknown
    # How sure we are that anything reached the employer:
    #   verified   -- the page confirmed it
    #   unverified -- sent, possibly received, never confirmed
    #   failed     -- never sent, or the page rejected it
    # Defaults to the *weakest* claim. Guessing upward ("it probably worked")
    # is how a failed run becomes a confident number in someone's stats.
    outcome: str = "unverified"
    grant_id: str = ""  # the one-time authorization that allowed the final submit
    resume_sha256: str = ""  # which file actually went out
    applied_at: str = Field(default_factory=_now)
    notes: str = ""
    answers_used: list[str] = Field(default_factory=list)  # QA ids
    vision_fallbacks: int = 0
    steps: int = 0


class VisionFallback(BaseModel):
    """One occurrence of "the DOM locator failed and vision had to rescue it".

    This is not telemetry for its own sake. Every entry is a gap report for the
    platform adapter: it names a field we could not locate semantically, so it
    tells us exactly which adapter rule to write next. It turns the flywheel
    from "tune the rules we have" into "discover the rules we are missing".
    """

    platform: str
    field_label: str
    dom_attempts: int = 0
    succeeded: bool = False
    url: str = ""
    created_at: str = Field(default_factory=_now)


class MemoryData(BaseModel):
    """Top-level memory structure."""

    version: int = SCHEMA_VERSION
    profile: dict = Field(default_factory=dict)
    learned_qa: list[QAEntry] = Field(default_factory=list)
    platforms: dict[str, PlatformKnowledge] = Field(default_factory=dict)
    routes: dict[str, RouteKnowledge] = Field(default_factory=dict)
    application_history: list[ApplicationRecord] = Field(default_factory=list)
    vision_fallbacks: list[VisionFallback] = Field(default_factory=list)

    # ── flywheel counters ──
    questions_encountered: int = 0
    questions_automated: int = 0  # answered from memory, no human involved
    questions_asked: int = 0  # had to interrupt the human
    selectors_suggested: int = 0
    selectors_hit: int = 0
    # Times two processes disagreed about a fact in this file and the larger
    # value was kept. Not an error: it is the only visible sign that more than
    # one writer is live, and it is what tells a future reader that a counter
    # may be lower than the number of runs that actually happened.
    merge_conflicts: int = 0


# ── Human priors for the selector knowledge base ────────────────────
# These give the very first run on a platform a head start. Every subsequent run
# reinforces or punishes them with real evidence.

_SEED_SELECTORS: dict[str, dict[str, list[str]]] = {
    "LinkedIn": {
        "apply_button": [
            "button.jobs-apply-button",
            'button:has-text("Easy Apply")',
            ".jobs-s-apply button",
        ],
        "next_button": [
            'button[aria-label="Continue to next step"]',
            ".jobs-easy-apply-modal footer button.artdeco-button--primary",
            'button:has-text("Next")',
        ],
        "review_button": [
            'button[aria-label="Review your application"]',
            'button:has-text("Review")',
        ],
        "submit_button": [
            'button[aria-label="Submit application"]',
            'button:has-text("Submit application")',
        ],
        "resume_upload": ['input[type="file"]'],
        "dismiss_button": [
            'button[aria-label="Dismiss"]',
            ".artdeco-modal__dismiss",
        ],
    },
    "Indeed": {
        "apply_button": [
            "#indeedApplyButton",
            ".indeed-apply-button",
            'button:has-text("Apply now")',
        ],
        "next_button": ['button:has-text("Continue")', "#form-action-continue"],
        "submit_button": ['button:has-text("Apply")'],
        "resume_upload": ['input[type="file"]'],
    },
    "Workday": {
        "apply_button": [
            '[data-automation-id="applyButton"]',
            'button:has-text("Apply")',
        ],
        "next_button": ['[data-automation-id="bottom-navigation-next-button"]'],
        "submit_button": ['[data-automation-id="bottom-navigation-complete-button"]'],
        "resume_upload": ['input[type="file"]'],
    },
    "Greenhouse": {
        "apply_button": ["a#apply_button", "#main_fields .apply button"],
        "next_button": ['button:has-text("Next")'],
        "submit_button": ['input[type="submit"]', 'button:has-text("Submit")'],
        "resume_upload": ['input[type="file"]', "#resume"],
    },
    "Lever": {
        "apply_button": ["a.postings-btn", 'a:has-text("Apply")'],
        "submit_button": ['button.postings-btn[type="submit"]'],
        "resume_upload": ['input[type="file"][name="resume"]'],
    },
}


# ── Human priors for the apply-route knowledge base ─────────────────
# The selector priors above assume the application happens on the page you
# already opened. That is the assumption these correct: there is more than one
# way to apply, and the differences between them are structural, not cosmetic.
# A route seed describes a *shape* -- how many screens, what stands before the
# first field, which gate needs a human -- and never invents a selector. A
# fabricated selector is worse than none, because the flywheel adopts it and
# then has to unlearn it on real evidence.
#
# `human_required` marks the gates a machine genuinely cannot pass alone, so an
# unattended run knows to stop and ask rather than spin against a wall.

_SEED_ROUTES: dict[str, dict] = {
    "LinkedIn/easy_apply": {
        "entry_signature": [
            'an "Easy Apply" button on the posting itself',
            "the form opens as a modal; the page never navigates away",
        ],
        "prerequisites": ["a LinkedIn session", "a resume on file, or a PDF to upload"],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": "Open the Easy Apply modal"},
            {
                "ordinal": 2,
                "kind": "fill",
                "detail": "Answer the steps (contact info, resume, screening questions)",
            },
            {"ordinal": 3, "kind": "verify", "detail": "Review the summary before submitting"},
            {"ordinal": 4, "kind": "submit", "detail": "Submit the application"},
        ],
    },
    "Amazon/external_ats": {
        "entry_signature": [
            'an "Apply now" link on the amazon.jobs posting',
            "clicking it lands on passport.amazon.jobs",
        ],
        "prerequisites": [
            "an Amazon Jobs account",
            "an email Amazon accepts, or a Google / LinkedIn / Apple social login",
        ],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": 'Click "Apply now" on the posting'},
            {
                "ordinal": 2,
                "kind": "open",
                "detail": 'Land on passport.amazon.jobs ("Log in or create account")',
            },
            {
                "ordinal": 3,
                "kind": "fill",
                "detail": "Enter the account email",
                "selector": "#preLoginEmailField",
            },
            {
                "ordinal": 4,
                "kind": "otp",
                "detail": "Continue and complete sign-in (email verification / social login)",
                "human_required": True,
            },
            {"ordinal": 5, "kind": "fill", "detail": "Complete the application form"},
            {"ordinal": 6, "kind": "upload", "detail": "Attach the resume"},
            {
                "ordinal": 7,
                "kind": "fill",
                "detail": "Answer screening questions (work authorization, sponsorship, prior Amazon employment)",
            },
            {"ordinal": 8, "kind": "submit", "detail": "Submit the application"},
        ],
        "notes": (
            "Entry observed 2026-09-18 on posting 10529830: Apply now goes straight "
            "to passport.amazon.jobs, which asks for an email only and offers "
            "Amazon/Google/LinkedIn/Apple social logins. Everything past the "
            "sign-in gate is public knowledge, not yet observed on this route."
        ),
    },
    "Workday/external_ats": {
        "entry_signature": [
            "a *.myworkdayjobs.com / workday.com posting",
            "Apply opens Workday's own account gate",
        ],
        "prerequisites": [
            "a Workday account on that employer's tenant (created in-flow)",
            "a resume Workday can parse",
        ],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": "Start the application"},
            {"ordinal": 2, "kind": "fill", "detail": "Create an account or sign in (email + password)"},
            {
                "ordinal": 3,
                "kind": "otp",
                "detail": "Verify the email if the tenant requires it",
                "human_required": True,
            },
            {"ordinal": 4, "kind": "upload", "detail": "Attach the resume and let Workday parse it"},
            {
                "ordinal": 5,
                "kind": "fill",
                "detail": "Work through the multi-page form (My Information, Experience, Questions)",
            },
            {"ordinal": 6, "kind": "submit", "detail": "Submit from the final review page"},
        ],
        "notes": (
            "Shape is public knowledge: several screens, and the account gate "
            "cannot be skipped."
        ),
    },
    "Greenhouse/external_ats": {
        "entry_signature": [
            "a boards.greenhouse.io / greenhouse.io posting, or an embedded board",
            "the application is a single page, with no account",
        ],
        "prerequisites": ["a resume PDF"],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": "Open the application form"},
            {
                "ordinal": 2,
                "kind": "fill",
                "detail": "Complete the single-page form (name, contact, links)",
            },
            {"ordinal": 3, "kind": "upload", "detail": "Attach the resume (and cover letter if asked)"},
            {"ordinal": 4, "kind": "fill", "detail": "Answer any custom questions"},
            {"ordinal": 5, "kind": "submit", "detail": "Submit"},
        ],
        "notes": "No account gate -- the shortest of the external routes.",
    },
    "Lever/external_ats": {
        "entry_signature": [
            "a jobs.lever.co posting",
            "the application is a single page, with no account",
        ],
        "prerequisites": ["a resume PDF"],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": 'Open the form via "Apply for this job"'},
            {"ordinal": 2, "kind": "fill", "detail": "Complete the form (name, email, phone, links)"},
            {"ordinal": 3, "kind": "upload", "detail": "Attach the resume"},
            {"ordinal": 4, "kind": "submit", "detail": "Submit"},
        ],
        "notes": "No account gate.",
    },
    "Generic/external_ats": {
        "entry_signature": [
            '"Apply on company site" / "Apply externally" on the posting',
            "the click navigates away from the job board",
        ],
        "prerequisites": [
            "possibly an account on the employer's system",
            "an email that account can be verified through",
        ],
        "steps": [
            {"ordinal": 1, "kind": "click", "detail": "Follow the posting's apply link off-site"},
            {
                "ordinal": 2,
                "kind": "open",
                "detail": "Land on the employer's system; read the page before acting",
            },
            {
                "ordinal": 3,
                "kind": "otp",
                "detail": "Sign in / register if a gate is present",
                "human_required": True,
            },
            {"ordinal": 4, "kind": "fill", "detail": "Complete the form, however many screens it spans"},
            {"ordinal": 5, "kind": "upload", "detail": "Attach the resume"},
            {"ordinal": 6, "kind": "submit", "detail": "Submit"},
        ],
        "notes": "Fallback shape for an employer system with no dedicated route yet.",
    },
}


def _recompute_blocked(record: RouteKnowledge) -> None:
    """Rebuild `blocked_at` from the raw counts minus what was retracted.

    Kept as one function because it is the only place the two kinds of
    evidence meet, and doing it in three places is how they drifted apart.
    """
    record.blocked_at = {
        gate: count
        for gate, count in record.blockage_counts.items()
        if count > record.retracted_at.get(gate, 0)
    }


class MemoryStore:
    """Local-first memory store backed by a JSON file.

    The store is deliberately synchronous — it is a small file and the agent
    already awaits plenty elsewhere. All mutations persist immediately so a crash
    mid-run never loses learned answers.

    It is also **multi-process**: the MCP server, the scheduled pass and a
    manual run all open this file at once. Two things make that survivable:

    * every save takes an exclusive lock for the read-merge-write, so no writer
      can interleave with another's; and
    * the save *merges* rather than overwrites, so a process that never saw the
      other's records still writes them back. That is what makes a writer which
      does not take the lock -- an older version of this code, a one-off script
      -- unable to destroy the flywheel.
    """

    def __init__(self, path: str | Path | None = None, profile: ProfileStore | None = None):
        if path is None:
            path = Path(__file__).parent.parent.parent / "data" / "memory.json"
        self.path = Path(path)
        # The profile lives in its own markdown file, next to memory.json. Two
        # reasons: a clone must not ship anyone's personal data inside the file
        # that is *supposed* to accumulate state, and the profile is the one
        # store a human is expected to hand-edit, which memory.json is not.
        self._profile = (
            profile if profile is not None
            else ProfileStore(self.path.parent / PROFILE_FILENAME)
        )
        # Top-level keys written by a newer version of this code, kept verbatim
        # and written back. Dropping them is how an older process would delete
        # a field its own model has never heard of.
        self._passthrough: dict = {}
        # (inode, mtime, size) as of our last read or write, so `refresh` can
        # tell "another process wrote" from "nothing changed" with one stat.
        self._signature: tuple | None = None
        self._lock_path = concurrency.data_lock_path(self.path.parent, "memory")
        # Runs before _load() so the JSON we read has already had any schema-3
        # profile stripped out of it -- otherwise _data would keep a stale copy
        # in memory and write it straight back on the next save.
        self.migrated_profile_fields = migrate_legacy_profile(self._profile, self.path)
        self._data = MemoryData()
        self._load()
        self._seed_priors()
        self._seed_routes()

    # ── persistence ──────────────────────────────────────────────────

    def refresh(self) -> bool:
        """Re-read if another process has written since we last looked.

        A long-lived process -- the MCP server is the one that matters -- holds
        an in-memory copy for hours. Without this, a question answered by the
        scheduled pass in the meantime would still be asked, and a posting it
        already applied to would still be offered. One `stat` per call is cheap
        enough to do it on the read paths where being stale costs something.

        Returns True if anything was re-read.
        """
        signature = concurrency.file_signature(self.path)
        if signature is None:
            # Nothing on disk to read. Keeping this process's copy is right:
            # `_load` on a missing file would drop the seeds installed at
            # startup and hand back an empty flywheel.
            return False
        if signature == self._signature:
            return False
        self._load()
        return True

    def _load(self):
        self._passthrough = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = MemoryData.model_validate(raw)
                self._passthrough = {
                    key: value
                    for key, value in raw.items()
                    if key not in MemoryData.model_fields
                }
            except Exception:
                # Never silently discard the flywheel. Preserve the unreadable
                # file so it can be inspected, then start from a clean slate.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                salvage = self.path.with_suffix(f".corrupt-{stamp}.json")
                try:
                    self.path.replace(salvage)
                    print(
                        f"warning: {self.path.name} was unreadable and has been "
                        f"preserved as {salvage.name}; starting from empty memory"
                    )
                except OSError:
                    pass
                self._data = MemoryData()
        else:
            self._data = MemoryData()

        # Re-key entries written by an older version (or an older normalizer),
        # then collapse whatever duplicates that re-keying exposes.
        dirty = False
        previous_version = self._data.version
        if self._data.version < SCHEMA_VERSION:
            self._data.version = SCHEMA_VERSION
            dirty = True
        for qa in self._data.learned_qa:
            key = normalize_question(qa.question)
            if qa.key != key:
                qa.key = key
                dirty = True
        # Applications written before schema 3 have no job_id; backfill it so
        # deduplication works against history that predates the field.
        for rec in self._data.application_history:
            if not rec.job_id:
                derived = extract_job_id(rec.job_url)
                if derived:
                    rec.job_id = derived
                    dirty = True
        # Routes gained counters in schema 4. `runs: 0` is the flywheel's word
        # for "never tried", so leaving a route at zero while a dozen of its
        # applications already sit in the history would be exactly the signal
        # confusion the counters exist to prevent. Fold the history in.
        #
        # Guarded on *both* the version and the route's own counters: a process
        # still running the pre-schema-4 code writes the file back without
        # `routes` and with the old version, so this migration can legitimately
        # run more than once on the same file. Re-applying it to a route that
        # already carries evidence would silently multiply that evidence.
        if previous_version < 4:
            tally: dict[str, list[int]] = {}
            for rec in self._data.application_history:
                if not rec.apply_route:
                    continue
                key = f"{rec.platform or 'Generic'}/{rec.apply_route}"
                row = tally.setdefault(key, [0, 0])
                row[0] += 1
                if rec.status in ("applied", "success"):
                    row[1] += 1
                dirty = True
            for key, (runs, successes) in tally.items():
                platform, _, name = key.partition("/")
                route = self._data.routes.get(key)
                if route is None:
                    route = RouteKnowledge(platform=platform, route=name)
                    self._data.routes[key] = route
                if route.runs:
                    continue
                route.runs = runs
                route.successes = successes
        if self._collapse_duplicate_keys():
            dirty = True
        if dirty:
            self._save()
        self._signature = concurrency.file_signature(self.path)

    def _collapse_duplicate_keys(self) -> bool:
        """Fold memories that share a key into one.

        The survivor is the better-trusted entry; the absorbed entry's counters
        are added in so no evidence is thrown away. Returns True if anything
        was merged.
        """
        survivors: dict[str, QAEntry] = {}
        merged = False
        for qa in self._data.learned_qa:
            existing = survivors.get(qa.key)
            if existing is None:
                survivors[qa.key] = qa
                continue
            merged = True
            winner, loser = (
                (existing, qa) if existing.confidence >= qa.confidence else (qa, existing)
            )
            winner.times_used += loser.times_used
            winner.success_count += loser.success_count
            winner.failure_count += loser.failure_count
            winner.asked_count += loser.asked_count
            winner.trust = round(max(winner.trust, loser.trust), 4)
            if not winner.context:
                winner.context = loser.context
            survivors[qa.key] = winner
        if merged:
            self._data.learned_qa = list(survivors.values())
        return merged

    def _save(self):
        """Persist under an exclusive lock, merging with what is on disk.

        Three separate guarantees are stacked here, and each one covers a
        failure the others do not:

        1. **Atomic swap.** Writing to a temp file, fsyncing, then
           `os.replace` means a reader sees the old file or the new one, never
           a truncated one. A crash -- or a full disk -- halfway through used to
           cost every learned answer at once.
        2. **Exclusive read-merge-write.** The lock makes "read what is there,
           fold it in, write" indivisible. Without it two processes can both
           read the same state, both write, and the second one silently undoes
           the first -- which is how an easy-apply batch deleted the entire
           route knowledge base while its own run looked fine.
        3. **Merge, not overwrite.** The lock only helps writers that take it.
           Folding the on-disk file in first means a writer which took nothing
           -- an older version of this code, a script somebody wrote -- still
           cannot delete a record it has never heard of: we write those records
           back on its behalf, on the next save, from whichever process is
           current.

        `version` is written as `max(mine, on-disk)` for the same reason. A
        rollback to an older schema is the one downgrade that can cascade: the
        file then re-runs migrations it has already run, and any field the older
        writer did not know about has to be rebuilt from history.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with concurrency.exclusive(
            self.path.parent, "memory", purpose=f"memory.json write (pid {os.getpid()})"
        ):
            on_disk = concurrency.read_json(self.path, None)
            if isinstance(on_disk, dict):
                self._absorb(on_disk)
            payload = self._data.model_dump(mode="json")
            # Whatever a newer version of this code wrote, it goes back
            # untouched. `setdefault` so a field we now own wins over a stale
            # passthrough copy of it.
            for key, value in self._passthrough.items():
                payload.setdefault(key, value)
            concurrency.atomic_write_json(self.path, payload)
            self._signature = concurrency.file_signature(self.path)

    # ── merging with other writers ───────────────────────────────────

    def _absorb(self, on_disk: dict) -> int:
        """Fold the on-disk file into this process's copy, in place.

        In place rather than by replacing `_data`, because callers hold
        references to entries they just mutated; swapping the object would
        silently detach them and the next save would drop that change.

        The rules, and why each is the conservative direction:

        * **Records merge by identity and are never dropped** -- history, routes,
          platforms, selectors, answers. Two processes that applied to different
          postings must not cost each other a row, and the history is the
          deduplication surface, so losing one means applying twice.
        * **Counters take the larger value.** Two writers disagreeing means both
          incremented from a shared base, so neither number is right; picking
          the larger can only undercount, and an undercounted flywheel is
          cautious while an overcounted one is confidently wrong. Max also
          cannot double-count a save, which summing would.
        * **A coupled record -- a learned answer -- is taken whole.** Its fields
          are not independent: picking the larger `trust` alongside the larger
          counters would resurrect an answer that evidence had just demoted.

        Returns how many disagreements it had to resolve.
        """
        try:
            disk = MemoryData.model_validate(on_disk)
        except ValidationError:
            # Unreadable on the other side is not something to guess about. Our
            # own copy is still written, which is strictly better than either
            # failing the save or merging against nonsense.
            return 0

        conflicts = 0
        # Both of these are monotone by design: a schema version that can move
        # backwards makes the file re-run migrations it has already run, and a
        # conflict count that can move backwards stops being a count.
        self._data.version = max(self._data.version, disk.version)
        self._data.merge_conflicts = max(self._data.merge_conflicts, disk.merge_conflicts)

        # `profile` is skipped deliberately and unconditionally. Its home is
        # `profile.md`; the JSON field is only the schema-3 staging area that
        # `migrate_legacy_profile` empties on startup, so merging it would put
        # back the duplicate that migration exists to remove.
        self._passthrough = {
            key: value for key, value in on_disk.items() if key not in MemoryData.model_fields
        }

        conflicts += self._merge_answers(disk)
        conflicts += self._merge_platforms(disk)
        conflicts += self._merge_routes(disk)
        self._merge_history(disk)
        self._merge_vision_fallbacks(disk)

        for field in (
            "questions_encountered",
            "questions_automated",
            "questions_asked",
            "selectors_suggested",
            "selectors_hit",
        ):
            mine, theirs = getattr(self._data, field), getattr(disk, field)
            if theirs > mine:
                setattr(self._data, field, theirs)

        if conflicts:
            self._data.merge_conflicts += conflicts
        return conflicts

    def _merge_answers(self, disk: MemoryData) -> int:
        by_key = {qa.key: qa for qa in self._data.learned_qa}
        conflicts = 0
        for theirs in disk.learned_qa:
            mine = by_key.get(theirs.key)
            if mine is None:
                self._data.learned_qa.append(theirs)
                by_key[theirs.key] = theirs
                continue
            if mine.model_dump() == theirs.model_dump():
                continue
            conflicts += 1
            winner, loser = (
                (mine, theirs)
                if mine._evidence_units >= theirs._evidence_units
                else (theirs, mine)
            )
            if winner is theirs:
                self._data.learned_qa[self._data.learned_qa.index(mine)] = theirs
                by_key[theirs.key] = theirs
            # Blanks are filled from the loser because a missing answer is not a
            # disagreement; it is an entry that only one side ever completed.
            if not winner.answer and loser.answer:
                winner.answer = loser.answer
                winner.source = loser.source
            if not winner.context and loser.context:
                winner.context = loser.context
        return conflicts

    def _merge_platforms(self, disk: MemoryData) -> int:
        conflicts = 0
        for name, theirs in disk.platforms.items():
            mine = self._data.platforms.get(name)
            if mine is None:
                self._data.platforms[name] = theirs
                continue
            for field in ("runs", "successes"):
                if getattr(theirs, field) > getattr(mine, field):
                    setattr(mine, field, getattr(theirs, field))
                    conflicts += 1
            if not mine.notes and theirs.notes:
                mine.notes = theirs.notes
            for role, their_stats in theirs.selectors.items():
                mine_stats = mine.selectors.setdefault(role, [])
                known = {stat.selector: stat for stat in mine_stats}
                for their_stat in their_stats:
                    stat = known.get(their_stat.selector)
                    if stat is None:
                        mine_stats.append(their_stat)
                        known[their_stat.selector] = their_stat
                        continue
                    for field in ("hits", "misses"):
                        if getattr(their_stat, field) > getattr(stat, field):
                            setattr(stat, field, getattr(their_stat, field))
                            conflicts += 1
        return conflicts

    def _merge_routes(self, disk: MemoryData) -> int:
        conflicts = 0
        for key, theirs in disk.routes.items():
            mine = self._data.routes.get(key)
            if mine is None:
                self._data.routes[key] = theirs
                continue
            # Shape first, and it is fill-blanks-only: the shape is what the
            # human prior and the observed run both describe, and whichever
            # arrived first is not more true than the other.
            if not mine.steps and theirs.steps:
                mine.steps = theirs.steps
            if not mine.entry_signature and theirs.entry_signature:
                mine.entry_signature = theirs.entry_signature
            if not mine.prerequisites and theirs.prerequisites:
                mine.prerequisites = theirs.prerequisites
            if not mine.notes and theirs.notes:
                mine.notes = theirs.notes
            for field in ("runs", "successes"):
                if getattr(theirs, field) > getattr(mine, field):
                    setattr(mine, field, getattr(theirs, field))
                    conflicts += 1
            for gate, count in theirs.blockage_counts.items():
                if count > mine.blockage_counts.get(gate, 0):
                    mine.blockage_counts[gate] = count
            for gate, count in theirs.retracted_at.items():
                if count > mine.retracted_at.get(gate, 0):
                    mine.retracted_at[gate] = count
            before = dict(mine.blocked_at)
            _recompute_blocked(mine)
            if mine.blocked_at != before:
                conflicts += 1
        return conflicts

    def _merge_history(self, disk: MemoryData) -> None:
        """Union of every application on record. Nothing here is ever dropped.

        This list is what "have I already applied to this" is answered from, so
        losing a row does not lose a statistic -- it loses the only thing
        standing between the user and applying to the same posting twice.
        """
        known = {self._record_identity(rec): rec for rec in self._data.application_history}
        for theirs in disk.application_history:
            key = self._record_identity(theirs)
            mine = known.get(key)
            if mine is None:
                self._data.application_history.append(theirs)
                known[key] = theirs
                continue
            # Two views of one posting: the terminal one wins, because
            # "applied" is the fact that matters and "" is what a half-written
            # record looks like.
            if mine.status in ("", "unknown") and theirs.status not in ("", "unknown"):
                self._data.application_history[
                    self._data.application_history.index(mine)
                ] = theirs
                known[key] = theirs
                continue
            for field in ("job_title", "company", "platform", "ats", "apply_route", "notes"):
                if not getattr(mine, field) and getattr(theirs, field):
                    setattr(mine, field, getattr(theirs, field))

    def _merge_vision_fallbacks(self, disk: MemoryData) -> None:
        seen = {self._fallback_identity(item) for item in self._data.vision_fallbacks}
        for theirs in disk.vision_fallbacks:
            key = self._fallback_identity(theirs)
            if key not in seen:
                self._data.vision_fallbacks.append(theirs)
                seen.add(key)

    @staticmethod
    def _record_identity(rec: ApplicationRecord) -> str:
        return rec.job_id or dedup_key(rec.job_url) or rec.id

    @staticmethod
    def _fallback_identity(item: VisionFallback) -> tuple:
        # No id on the model, and giving one now would not help records written
        # before it existed. The tuple below is what distinguishes one
        # occurrence from another; two processes hitting the same missing
        # selector on the same page in the same second is a duplicate, and
        # collapsing that is the right answer.
        return (item.platform, item.field_label, item.url, item.created_at)

    def _seed_priors(self):
        """Install curated selector priors for platforms we haven't met yet."""
        added = False
        for platform, roles in _SEED_SELECTORS.items():
            pk = self._data.platforms.get(platform)
            if pk is None:
                pk = PlatformKnowledge(platform=platform)
                self._data.platforms[platform] = pk
            for role, selectors in roles.items():
                stats = pk.selectors.setdefault(role, [])
                known = {s.selector for s in stats}
                for sel in selectors:
                    if sel not in known:
                        stats.append(SelectorStat(selector=sel))
                        known.add(sel)
                        added = True
        if added:
            self._save()

    def _seed_routes(self):
        """Install curated route priors for routes we haven't met yet.

        The prior supplies the *shape* -- how many screens, what stands before
        the first field, which gate needs a human. It never touches the counters:
        `runs` / `successes` / `blocked_at` are evidence, and a human prior must
        not overwrite what real attempts taught us. The two sets do not overlap,
        which is what makes it safe to fill blanks on every load -- and what
        keeps it correct when history backfills a counter before the seed has
        had a chance to run.
        """
        added = False
        for key, spec in _SEED_ROUTES.items():
            platform, _, route = key.partition("/")
            record = self._data.routes.get(key)
            if record is None:
                record = RouteKnowledge(platform=platform, route=route)
                self._data.routes[key] = record
                added = True
            steps = [RouteStep(**step) for step in spec.get("steps", [])]
            if steps and not record.steps:
                record.steps = steps
                added = True
            if not record.entry_signature and spec.get("entry_signature"):
                record.entry_signature = list(spec["entry_signature"])
                added = True
            if not record.prerequisites and spec.get("prerequisites"):
                record.prerequisites = list(spec["prerequisites"])
                added = True
            if not record.notes and spec.get("notes"):
                record.notes = spec["notes"]
                added = True
        if added:
            self._save()

    # ── profile ──────────────────────────────────────────────────────
    #
    # These delegate. The profile is deliberately *not* stored here any more:
    # `MemoryData.profile` was the schema-3 location and is kept only so the
    # migration below can read it. A clone with an empty memory.json must not
    # inherit anyone's phone number, and a user editing their own profile must
    # not have to touch the file the flywheel rewrites on every tool call.

    @property
    def profile(self) -> ProfileStore:
        return self._profile

    def get_profile(self) -> dict:
        return self._profile.get()

    def set_profile(self, key: str, value: str):
        self._profile.set_many({key: value})

    def update_profile(self, data: dict) -> dict:
        return self._profile.set_many(data)

    # ── learned Q&A ──────────────────────────────────────────────────

    def learn(self, question: str, answer: str, context: str = "", source: str = "user") -> QAEntry:
        """Save a question-answer pair, merging into an existing memory if equivalent.

        Relearning the same question is not a no-op: it refreshes and *reinforces*
        the existing entry, which is exactly how the flywheel compounds.
        """
        key = normalize_question(question)
        existing = next((qa for qa in self._data.learned_qa if qa.key == key), None)

        if existing is not None:
            existing.answer = answer
            existing.source = source
            if context:
                existing.context = context
            # A fresh human confirmation is a strong positive signal.
            existing.success_count += 1
            existing._reinforce(True)
            existing.learned_at = _now()
            if source == "user":
                # Answering the same question again, in the user's own words, is
                # a statement rather than a deployment: mark it as such and leave
                # `times_used` to mean what it says. (This used to call
                # `mark_used()` to break a circular dependency in `is_auto_ready`;
                # `confirmed_by_human` is the honest way to break it.)
                existing.confirmed_by_human = True
        else:
            existing = QAEntry(
                question=question,
                answer=answer,
                context=context,
                source=source,
                key=key,
                success_count=1,  # the human vouched for it once already
                trust=USER_ANSWER_TRUST if source == "user" else INITIAL_TRUST,
                confirmed_by_human=(source == "user"),
            )
            self._data.learned_qa.append(existing)

        self._save()
        return existing

    def find_answer(self, question: str, threshold: float = 0.5) -> Optional[QAEntry]:
        """Fast local recall by keyword overlap. Returns highest-overlap match.

        Re-reads first when another process has written. The scheduled pass and
        the interactive session both answer questions, and a process that keeps
        its startup view would ask the human something the other one recorded
        an hour ago -- which is the exact interruption the flywheel exists to
        remove.
        """
        self.refresh()
        if not self._data.learned_qa:
            return None

        q_words = question_keywords(question)
        if not q_words:
            return None

        best: Optional[QAEntry] = None
        best_score = 0.0
        for qa in self._data.learned_qa:
            if qa.confidence < TRUST_FLOOR:
                continue
            qa_words = question_keywords(qa.question)
            if not qa_words:
                continue
            # Containment-style score: how much of the shorter question is covered.
            overlap = len(q_words & qa_words)
            score = overlap / max(min(len(q_words), len(qa_words)), 1)
            if score >= threshold and score > best_score:
                best_score, best = score, qa

        return best

    def get_confident_answer(self, question: str, threshold: float = 0.5) -> Optional[QAEntry]:
        """Recall only if confident enough to auto-fill."""
        qa = self.find_answer(question, threshold=threshold)
        return qa if (qa and qa.is_auto_ready) else None

    def get_suggestion(self, question: str) -> Optional[QAEntry]:
        """Best guess to prefill the ask dialog, even if not confident enough to auto-fill."""
        return self.find_answer(question, threshold=0.34)

    def get_all_qa(self) -> list[dict]:
        """All learned Q&A including derived scores, so the UI can show current trust."""
        out = []
        for qa in self._data.learned_qa:
            row = qa.model_dump()
            row["confidence"] = qa.confidence
            row["auto_ready"] = qa.is_auto_ready
            row["success_rate"] = round(qa.success_rate, 3)
            out.append(row)
        return out

    def get_qa(self, qa_id: str) -> Optional[QAEntry]:
        return next((qa for qa in self._data.learned_qa if qa.id == qa_id), None)

    def delete_qa(self, qa_id: str):
        self._data.learned_qa = [qa for qa in self._data.learned_qa if qa.id != qa_id]
        self._save()

    def update_qa(
        self,
        qa_id: str,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        context: Optional[str] = None,
    ):
        qa = self.get_qa(qa_id)
        if qa is None:
            return
        if question is not None and question != qa.question:
            qa.question = question
            qa.key = normalize_question(question)
        if answer is not None:
            qa.answer = answer
        if context is not None:
            qa.context = context
        self._save()

    def record_question(self, automated: bool):
        """Bookkeeping for the automation-rate metric."""
        self._data.questions_encountered += 1
        if automated:
            self._data.questions_automated += 1
        else:
            self._data.questions_asked += 1
        self._save()

    def record_answer_outcome(self, qa_id: str, success: bool):
        """Fold one run's outcome back into an answer's confidence."""
        qa = self.get_qa(qa_id)
        if qa is None:
            return
        if success:
            qa.success_count += 1
        else:
            qa.failure_count += 1
        qa._reinforce(success)
        self._save()

    # ── platform / selector knowledge ────────────────────────────────

    def get_platform(self, platform: str) -> PlatformKnowledge:
        pk = self._data.platforms.get(platform)
        if pk is None:
            pk = PlatformKnowledge(platform=platform)
            self._data.platforms[platform] = pk
            self._save()
        return pk

    def list_platforms(self) -> list[str]:
        return sorted(self._data.platforms.keys())

    def get_selector_hints(self, platform: str, top_k: int = 3) -> dict[str, list[str]]:
        """Best-known selectors per role, strongest first. Fed into the LLM prompt."""
        pk = self._data.platforms.get(platform)
        if pk is None:
            return {}
        hints: dict[str, list[str]] = {}
        for role, stats in pk.selectors.items():
            ranked = sorted(stats, key=lambda s: (-s.score, -s.hits))
            top = [s.selector for s in ranked[:top_k] if s.score >= 0.5]
            if top:
                hints[role] = top
        return hints

    def record_selector_result(self, platform: str, role: str, selector: str, success: bool):
        """Reinforce or punish a selector after it was tried for real.

        `selectors_suggested` counts *attempts*, not offers: a selector we told
        the caller about and then never tried teaches us nothing. Counting the
        attempt is what makes `selectors_hit / selectors_suggested` a real hit
        rate -- and a non-zero `selectors_suggested` is the probe for the
        flywheel actually being wired up rather than silently starving.
        """
        if not role or not selector:
            return
        pk = self.get_platform(platform)
        stats = pk.selectors.setdefault(role, [])
        stat = next((s for s in stats if s.selector == selector), None)
        if stat is None:
            stat = SelectorStat(selector=selector)
            stats.append(stat)
        self._data.selectors_suggested += 1
        if success:
            stat.hits += 1
            self._data.selectors_hit += 1
        else:
            stat.misses += 1
        # Keep each role's candidate list bounded.
        pk.selectors[role] = sorted(stats, key=lambda s: -s.score)[:8]
        self._save()

    def count_selector_suggestion(self):
        self._data.selectors_suggested += 1

    # ── apply-route knowledge ────────────────────────────────────────
    #
    # The selector layer above answers "what does the button look like". These
    # answer a question it structurally cannot: what kind of journey is this,
    # and where will it stop needing a machine. They are separate stores on
    # purpose -- a route we understand perfectly can still have no selectors,
    # and a page full of working selectors can still be the wrong route.

    def get_route(self, platform: str, route: str) -> RouteKnowledge:
        """Fetch a route record, creating it if we have never run it before.

        Called with an unseen route, this is how a new *kind* of application
        enters the base: it starts with no evidence, keeps whatever prior the
        seeds supplied, and accumulates from the first real attempt.
        """
        key = f"{platform}/{route}"
        record = self._data.routes.get(key)
        if record is None:
            record = RouteKnowledge(platform=platform, route=route)
            self._data.routes[key] = record
            self._save()
        return record

    def list_routes(self) -> list[str]:
        return sorted(self._data.routes.keys())

    def record_journey(
        self,
        platform: str,
        route: str,
        steps: list[RouteStep],
        *,
        entry_signature: list[str] | None = None,
        notes: str = "",
    ) -> RouteKnowledge:
        """Remember how an application of this kind was actually walked.

        `RouteStep`'s own docstring used to say these steps were "not to replay a
        recording" -- they described the shape of a journey, not the journey. This
        writes the journey: the order the screens came in, the control each step
        used, what was typed into each field and where the value came from.

        Stored whole rather than appended: a journey is a path, and a path that
        has half of yesterday's steps in it is worse than no path at all. Every
        successful walk overwrites the previous recording, so what the next run
        replays is what worked last time.
        """
        record = self.get_route(platform, route)
        record.steps = list(steps)
        if entry_signature is not None:
            record.entry_signature = list(entry_signature)
        if notes:
            record.notes = notes
        self._save()
        return record

    def record_route_blockage(
        self, platform: str, route: str, step: str, notes: str = ""
    ) -> RouteKnowledge:
        """Record that an attempt died at `step` on this route.

        Outcome counters live on `add_application`; this records the
        *diagnosis*. A success rate can only tell you a route is hard --
        `blocked_at` tells you which gate makes it hard, and that sentence is
        what the next adapter gets written from.
        """
        record = self.get_route(platform, route)
        if step:
            # Counted against the raw evidence, so a gate that blocks again
            # outgrows its old retraction instead of being silently forgiven.
            record.blockage_counts[step] = record.blockage_counts.get(step, 0) + 1
            _recompute_blocked(record)
        if notes:
            record.notes = notes
        self._save()
        return record

    def clear_route_blockage(
        self, platform: str, route: str, step: str, notes: str = ""
    ) -> RouteKnowledge:
        """Retract a gate this route has been proven to pass.

        A blockage could only ever be added, never withdrawn, so a route that
        had been walked to the end still announced the gate it once died at --
        and the next run treated that stale sentence as a wall and stopped in
        front of a door that was open. Evidence outweighs history: when the
        route completes, the gate goes.
        """
        record = self.get_route(platform, route)
        if step:
            # Forgive everything counted so far; a later blockage is a higher
            # count and stands on its own.
            record.retracted_at[step] = max(
                record.retracted_at.get(step, 0), record.blockage_counts.get(step, 0)
            )
            _recompute_blocked(record)
        if notes:
            record.notes = notes
        self._save()
        return record

    def routes_for_url(self, url: str) -> list[RouteKnowledge]:
        """Every route we know that could serve this URL, best-evidenced first.

        Detection is by platform, and the caller gets *all* of that platform's
        routes rather than one guess: a LinkedIn URL can legitimately be either
        Easy Apply or a redirect to an employer's system, and which one it is
        only becomes visible after the page is open. Ordering puts the route
        with the most real evidence first, so a caller forced to act on one
        acts on the one we have actually run. Falls back to `Generic` only when
        the platform itself is unknown.

        Re-reads first: this is what `route_guide` answers from, and the harness
        asks it *before* opening a form. Telling it a route still needs a login
        the scheduled pass has since got past is a warning that costs a human
        interruption for nothing.
        """
        self.refresh()
        platform = detect_platform(url).value
        matched = [r for r in self._data.routes.values() if r.platform == platform]
        if not matched:
            matched = [r for r in self._data.routes.values() if r.platform == "Generic"]
        return sorted(matched, key=lambda r: (-r.runs, -r.successes, r.route))

    # ── application history & attribution ────────────────────────────

    def add_application(
        self,
        job_url: str,
        job_title: str = "",
        company: str = "",
        platform: str = "",
        status: str = "applied",
        notes: str = "",
        answers_used: Optional[list[str]] = None,
        steps: int = 0,
        job_id: str = "",
        ats: str = "",
        apply_route: str = "",
        vision_fallbacks: int = 0,
        outcome: str = "unverified",
        grant_id: str = "",
        resume_sha256: str = "",
    ) -> ApplicationRecord:
        """Record an application attempt.

        `outcome` is what the statistics count, and it defaults to
        `unverified`: being *sent* is the default state of uncertainty, and it
        is never evidence of being *received*. Only `execute_authorized_submission`
        reporting `verified` may pass anything stronger.
        """
        record = ApplicationRecord(
            job_url=job_url,
            job_id=job_id or extract_job_id(job_url),
            job_title=job_title,
            company=company,
            platform=platform,
            ats=ats,
            apply_route=apply_route,
            status=status,
            notes=notes,
            answers_used=answers_used or [],
            vision_fallbacks=vision_fallbacks,
            steps=steps,
            outcome=outcome,
            grant_id=grant_id,
            resume_sha256=resume_sha256,
        )
        self._data.application_history.append(record)

        # ---- attribution: this is what makes the wheel spin ----
        # Success is counted from `outcome`, never from `status`. The old rule
        # counted anything not explicitly failed, which meant a run whose result
        # was never confirmed looked identical to a confirmed one -- and made the
        # success rate a number nobody could act on.
        verified = outcome == "verified"
        if platform:
            pk = self.get_platform(platform)
            pk.runs += 1
            if verified:
                pk.successes += 1
        if apply_route:
            # The same outcome also votes on how the application was *routed*.
            # Counting it here, rather than only in `record_route_blockage`,
            # keeps every attempt on the record: a route that is only ever
            # reported when it fails would read as one that never succeeds.
            routed = self.get_route(platform or "Generic", apply_route)
            routed.runs += 1
            if verified:
                routed.successes += 1
        for qa_id in record.answers_used:
            self.record_answer_outcome(qa_id, verified)

        self._save()
        return record

    def get_history(self) -> list[dict]:
        return [rec.model_dump() for rec in self._data.application_history]

    def find_application(self, job_url: str = "", job_id: str = "") -> Optional[ApplicationRecord]:
        """Most recent application record matching this job, if any.

        Re-reads first. This is the deduplication surface, and a stale answer
        here does not lose a statistic -- it means the same posting gets
        submitted twice, once by each process that had not seen the other's
        record yet.
        """
        self.refresh()
        wanted = dedup_key(job_url, job_id or extract_job_id(job_url))
        if not wanted:
            return None
        for rec in reversed(self._data.application_history):
            if dedup_key(rec.job_url, rec.job_id) == wanted:
                return rec
        return None

    def is_already_applied(self, job_url: str, job_id: str = "") -> bool:
        """True if we already submitted to this posting.

        Keyed on the job id rather than the URL so that the same posting reached
        through different tracking links is recognised as a repeat.
        """
        record = self.find_application(job_url, job_id)
        return record is not None and record.status in ("applied", "success")

    # ── vision fallback: the adapter gap report ──────────────────────

    def record_vision_fallback(
        self,
        platform: str,
        field_label: str,
        dom_attempts: int = 0,
        succeeded: bool = False,
        url: str = "",
    ) -> VisionFallback:
        """Log one case where DOM location failed and vision had to step in.

        Each row names a field we could not address semantically, which is
        precisely the rule the platform adapter is missing.
        """
        entry = VisionFallback(
            platform=platform,
            field_label=field_label,
            dom_attempts=dom_attempts,
            succeeded=succeeded,
            url=url,
        )
        self._data.vision_fallbacks.append(entry)
        self._save()
        return entry

    def get_vision_fallbacks(self, platform: str = "", limit: int = 50) -> list[dict]:
        """Fallbacks, optionally filtered by platform, newest first."""
        rows = [
            v for v in self._data.vision_fallbacks if not platform or v.platform == platform
        ]
        return [v.model_dump() for v in reversed(rows[-limit:])]

    def get_adapter_gaps(self, limit: int = 10) -> list[dict]:
        """Field labels that keep defeating the DOM locators, most frequent first.

        This is the shopping list for the next adapter: if 'country' shows up
        nine times on Workday, the Workday adapter needs a country rule.
        """
        counts: dict[tuple[str, str], dict] = {}
        for fb in self._data.vision_fallbacks:
            key = (fb.platform, fb.field_label)
            row = counts.setdefault(
                key, {"platform": fb.platform, "field_label": fb.field_label, "count": 0, "rescued": 0}
            )
            row["count"] += 1
            if fb.succeeded:
                row["rescued"] += 1
        ranked = sorted(counts.values(), key=lambda r: -r["count"])
        return ranked[:limit]

    # ── LLM-facing summaries ─────────────────────────────────────────

    def get_profile_summary(self) -> str:
        return self._profile.summary()

    def get_qa_summary(self, min_confidence: float = TRUST_FLOOR) -> str:
        """Learned Q&A for the prompt, ranked by confidence and trimmed.

        Only memories we still trust are shown, so poisoned entries naturally
        fall out of the model's context and stop wasting tokens.
        """
        usable = [qa for qa in self._data.learned_qa if qa.confidence >= min_confidence]
        if not usable:
            return "No learned answers yet."
        ordered = sorted(usable, key=lambda qa: (-qa.confidence, -qa.times_used))
        lines = []
        for qa in ordered:
            tag = "auto-ready" if qa.is_auto_ready else f"confidence {qa.confidence:.2f}"
            lines.append(f"Q: {qa.question}\nA: {qa.answer}   [{tag}]")
        return "\n---\n".join(lines)

    def get_platform_hints_text(self, platform: str) -> str:
        """Known-good selectors for this platform, formatted for the prompt."""
        hints = self.get_selector_hints(platform)
        if not hints:
            return ""
        lines = []
        for role, selectors in hints.items():
            lines.append(f"- {role}: {' | '.join(selectors)}")
        return "\n".join(lines)

    def get_route_hints_text(self, platform: str, route: str) -> str:
        """What we know about one apply route, formatted for the prompt.

        Written to be read *before* the browser is touched. The whole value of a
        route record is that it warns about the gate ahead of time -- discovering
        an email-verification wall by walking into it is the failure this exists
        to prevent.
        """
        record = self._data.routes.get(f"{platform}/{route}")
        if record is None:
            return ""
        lines: list[str] = []
        if record.entry_signature:
            lines.append("Looks like: " + "; ".join(record.entry_signature))
        if record.prerequisites:
            lines.append("Needs first: " + "; ".join(record.prerequisites))
        if record.steps:
            rendered = [
                f"{step.ordinal}. ({step.kind}) {step.detail}"
                + (" [needs the human]" if step.human_required else "")
                for step in record.steps
            ]
            lines.append("Steps:\n  " + "\n  ".join(rendered))
        if record.runs:
            history = f"History: {record.successes}/{record.runs} submitted"
            if record.hardest_gate:
                history += f"; most often blocked at: {record.hardest_gate}"
            lines.append(history)
        if record.notes:
            lines.append("Notes: " + record.notes)
        return "\n".join(lines)

    # ── flywheel telemetry ───────────────────────────────────────────

    def get_stats(self) -> dict:
        """Everything the UI needs to show whether the flywheel is accelerating.

        Re-reads first, so the numbers reported are the file's and not this
        process's memory of it. A report that shows what the *other* process
        learned five minutes ago is the whole point of keeping the file.
        """
        self.refresh()
        qa_list = self._data.learned_qa
        auto_ready = [qa for qa in qa_list if qa.is_auto_ready]
        total_questions = self._data.questions_encountered
        automation_rate = (
            self._data.questions_automated / total_questions if total_questions else 0.0
        )

        platform_stats = []
        for pk in self._data.platforms.values():
            validated = sum(
                1 for stats in pk.selectors.values() for s in stats if s.attempts > 0
            )
            total = sum(len(stats) for stats in pk.selectors.values())
            platform_stats.append(
                {
                    "platform": pk.platform,
                    "runs": pk.runs,
                    "success_rate": round(pk.success_rate, 3),
                    "selectors_total": total,
                    "selectors_validated": validated,
                }
            )

        # Routes are reported separately from platforms because they answer a
        # different question: platforms say "is this site getting easier",
        # routes say "which kind of application is still beyond us". A platform
        # can look healthy while its only ever-run route is the easy one.
        route_stats = [
            {
                "route": record.key,
                "runs": record.runs,
                "success_rate": round(record.success_rate, 3),
                "steps": len(record.steps),
                "human_gates": len(record.human_gates),
                "hardest_gate": record.hardest_gate,
            }
            for record in self._data.routes.values()
        ]

        return {
            "qa_total": len(qa_list),
            "qa_auto_ready": len(auto_ready),
            "qa_unproven": sum(1 for qa in qa_list if qa.times_used == 0),
            "questions_encountered": total_questions,
            "questions_automated": self._data.questions_automated,
            "questions_asked": self._data.questions_asked,
            "automation_rate": round(automation_rate, 3),
            # Raw counters alongside the rate. The rate alone cannot distinguish
            # "nothing was ever tried" from "everything was tried and missed" --
            # both report 0.0 -- and "nothing was ever tried" is precisely the
            # failure mode where the flywheel is dead but the file still looks
            # healthy. `selectors_suggested` is the probe that tells them apart.
            "selectors_suggested": self._data.selectors_suggested,
            "selectors_hit": self._data.selectors_hit,
            "selector_hit_rate": round(
                self._data.selectors_hit / self._data.selectors_suggested, 3
            )
            if self._data.selectors_suggested
            else 0.0,
            "applications_total": len(self._data.application_history),
            # Same rule as everywhere else: only a confirmed outcome is a
            # success. `status` says the attempt happened, `outcome` says
            # whether anyone confirmed it.
            "applications_success": sum(
                1 for r in self._data.application_history if r.outcome == "verified"
            ),
            "vision_fallbacks": len(self._data.vision_fallbacks),
            "adapter_gaps": self.get_adapter_gaps(5),
            "platforms": platform_stats,
            "routes": route_stats,
            # How often another process had a larger number than this one did.
            # Reported next to the counters rather than buried, because it is
            # the caveat on all of them: a non-zero value is the visible sign
            # that more than one writer is live, and that a run count here can
            # be lower than the number of runs that actually happened. Zero is
            # the normal reading for a single-writer install.
            "merge_conflicts": self._data.merge_conflicts,
            "concurrency": {
                "schema": self._data.version,
                "write_path": "exclusive lock + merge on write",
                "conflicts_merge_low": True,
            },
        }
