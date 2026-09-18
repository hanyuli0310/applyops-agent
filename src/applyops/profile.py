"""The candidate's own facts, kept in a file a human can read and edit.

Why a markdown file rather than more JSON
-----------------------------------------
`memory.json` holds what the system *learns*: answered questions, selector hit
rates, application history. Nobody should hand-edit it -- a human who opens it
will corrupt it, and the code guards it accordingly.

The profile is the exact opposite. It is the one part of the system whose
content only the user knows, and which they will want to correct the moment a
form gets filled with something stale. So it lives in `profile.md`: parseable,
diffable, editable in any editor.

Splitting them also fixes a reuse problem. A clone of this repo must not carry
anyone's name, phone number or resume path -- but `memory.json` is runtime state
that *must* accumulate. Two files with two lifecycles: `profile.md` is written
once by setup and edited rarely; `memory.json` is written constantly.

First run
---------
Nothing is pre-filled and nothing is guessed. `questionnaire()` describes every
field in the order it should be asked, with the current value so a caller can
show what is already known. `missing_required()` says what still blocks an
application. Setup is therefore idempotent: run it again and it only asks for
what is blank.

Format
------
A flat `- key: value` list under `## group` headings. Deliberately dumb, because
the whole point is that a human can edit it without reading this file. Blank
fields carry an inline HTML comment explaining what belongs there, which is
stripped before parsing -- guidance exactly where it is needed and nowhere else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from . import concurrency

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE_FILENAME = "profile.md"


def default_profile_path() -> Path:
    """Where the profile lives.

    `data/` rather than the project root, to sit with the other personal
    artefacts (memory.json, guard_state.json, browser-profile) under the single
    `.gitignore` entry that keeps all of them out of version control.
    """
    return PROJECT_ROOT / "data" / PROFILE_FILENAME


def example_profile_path() -> Path:
    return PROJECT_ROOT / "profile.example.md"


# ── the field spec ───────────────────────────────────────────────────
# Ordered by the sequence a person would naturally be asked. This list is the
# single source of truth: the questionnaire, the markdown renderer, the
# validator and the CLI all derive from it.

GROUP_LABELS: dict[str, str] = {
    "identity": "身份",
    "resume": "简历",
    "authorization": "工作授权",
    "experience": "经历",
    "compensation": "薪酬",
    "preferences": "求职偏好",
    "legal": "常见法律问题",
    "demographics": "自愿人口统计",
    "custom": "自定义字段",
}

GROUP_NOTES: dict[str, str] = {
    "legal": "这些问题美国申请表常问。答错比答慢更糟，所以宁可留空去问。",
    "demographics": "美国 EEO 自愿统计问题，法律上完全可选，全部可以回答 "
                    "「Prefer not to say」或者整组跳过。",
    "custom": "你自己加的字段。系统会原样保存，但不会主动拿来填表 —— "
              "除非名字正好对上表单问题。",
}


# A salary can legitimately have no number attached to it. "Open to market
# rate" is a real answer, and storing a number the user never gave would be
# inventing one. These phrasings all collapse to OPEN_VALUE.
OPEN_VALUE = "open"
_OPEN_PHRASES = {
    "open", "open to market", "open to market rate", "market", "market rate",
    "negotiable", "flexible", "any", "no preference", "no limit", "unlimited",
    "unrestricted", "whatever", "depends", "not sure", "n/a", "na", "tbd",
    "不限", "无", "均可", "面议", "看情况", "没有要求", "都可以",
}


class FieldSpec(BaseModel):
    """One piece of information about the candidate."""

    key: str
    question: str
    group: str
    # text | email | phone | path | int | bool | choice
    kind: str = "text"
    required: bool = False
    choices: list[str] = Field(default_factory=list)
    hint: str = ""
    why: str = ""
    example: str = ""
    # Numeric fields only: whether OPEN_VALUE is an acceptable answer. True for
    # salary (people genuinely have no target), false for years of experience
    # (nobody is "open" about how long they have worked).
    allow_open: bool = False

    @property
    def is_boolean(self) -> bool:
        return self.kind == "bool"


PROFILE_FIELDS: list[FieldSpec] = [
    # ── identity ─────────────────────────────────────────────────────
    FieldSpec(
        key="name", group="identity", kind="text", required=True,
        question="What is your full legal name?",
        hint="Exactly as it appears on your ID, not a nickname.",
        example="Jane Doe",
    ),
    FieldSpec(
        key="email", group="identity", kind="email", required=True,
        question="Which email address should applications use?",
        hint="Recruiter replies land here, so use one you actually read.",
        example="you@example.com",
    ),
    FieldSpec(
        key="phone", group="identity", kind="phone", required=True,
        question="What is your mobile number, with country code?",
        example="+1 555 010 4477",
    ),
    FieldSpec(
        key="location", group="identity", kind="text", required=True,
        question="Where do you currently live?",
        hint="City and country at minimum; forms often split this into fields.",
        example="Austin, TX, USA",
    ),
    FieldSpec(
        key="linkedin_url", group="identity", kind="text",
        question="Your LinkedIn profile URL?",
        example="https://www.linkedin.com/in/your-handle",
    ),
    FieldSpec(
        key="github_url", group="identity", kind="text",
        question="Your GitHub profile URL?",
        why="Engineering forms ask for this more often than you would expect.",
    ),
    FieldSpec(
        key="website_url", group="identity", kind="text",
        question="A portfolio or personal site?",
    ),
    # ── resume ───────────────────────────────────────────────────────
    FieldSpec(
        key="resume_path", group="resume", kind="path", required=True,
        question="Absolute path to the resume PDF you want to submit?",
        hint="A copy inside data/ works well -- that whole directory is "
             "git-ignored, so the file cannot be committed by accident.",
        example="/Users/you/data/resume.pdf",
        why="Every application uploads this file. If the path is wrong the "
            "submit step fails after the form is already filled.",
    ),
    FieldSpec(
        key="cover_letter_path", group="resume", kind="path",
        question="A default cover letter PDF, if you keep one?",
    ),
    # ── authorization ────────────────────────────────────────────────
    FieldSpec(
        key="work_authorization", group="authorization", kind="choice",
        required=True,
        question="What is your legal right to work where you are applying?",
        choices=[
            "US Citizen",
            "US Permanent Resident (Green Card)",
            "H-1B",
            "F-1 (student, CPT / OPT)",
            "F-1 OPT",
            "F-1 CPT",
            "TN",
            "Requires sponsorship",
            "Other",
        ],
        why="Nearly every US posting asks this, and it is a question the system "
            "must never answer on your behalf by guessing.",
    ),
    FieldSpec(
        key="requires_sponsorship", group="authorization", kind="bool",
        required=True,
        question="Will you need visa sponsorship now or in the future?",
        why="Note the wording: most forms mean 'ever', not 'for this job'.",
    ),
    FieldSpec(
        key="notice_period", group="authorization", kind="text",
        question="What notice period does your current job require?",
        example="2 weeks",
    ),
    FieldSpec(
        key="earliest_start_date", group="authorization", kind="text",
        question="What is the earliest date you could start?",
        example="2026-11-01",
    ),
    # ── experience ───────────────────────────────────────────────────
    FieldSpec(
        key="years_experience", group="experience", kind="int", required=True,
        question="How many years of full-time professional experience do you have?",
        hint="A whole number of years. Entry level is 0.",
        example="3",
    ),
    FieldSpec(
        key="current_title", group="experience", kind="text", required=True,
        question="What is your current or most recent job title?",
        example="Software Engineer",
    ),
    FieldSpec(
        key="current_company", group="experience", kind="text", required=True,
        question="Who is your current or most recent employer?",
    ),
    FieldSpec(
        key="highest_degree", group="experience", kind="choice",
        question="What is the highest degree you have completed, or are "
                 "currently pursuing?",
        hint="If you are mid-degree, name that degree -- it is what "
             "screeners filter on -- and record the expected date in "
             "graduation_date.",
        choices=["High School", "Associate", "Bachelor's", "Master's", "PhD", "Other"],
    ),
    FieldSpec(
        key="school", group="experience", kind="text",
        question="Which school awarded that degree?",
    ),
    FieldSpec(
        key="major", group="experience", kind="text",
        question="What did you study?",
    ),
    FieldSpec(
        key="graduation_date", group="experience", kind="text",
        question="When did -- or will -- you graduate?",
        hint="YYYY-MM. Near-universal on student and new-grad "
             "applications; they use it to slot you into a cohort.",
        example="2027-03",
    ),
    # ── compensation ─────────────────────────────────────────────────
    FieldSpec(
        key="expected_salary", group="compensation", kind="int", required=True,
        allow_open=True,
        question="What annual salary are you targeting? Digits only.",
        hint="A single number, or 'open' if you have no figure in mind. The "
             "field will be rendered into whatever format the form asks for "
             "(per year, per hour where obvious).",
        example="150000",
    ),
    FieldSpec(
        key="salary_currency", group="compensation", kind="choice", required=True,
        question="Which currency is that figure in?",
        choices=["USD", "CNY", "EUR", "GBP", "CAD", "AUD", "SGD"],
    ),
    FieldSpec(
        key="current_salary", group="compensation", kind="int", allow_open=True,
        question="Your current annual salary, digits only?",
        why="Frequently asked, frequently refused. Leaving it blank is a valid "
            "answer -- the system will ask rather than invent one.",
    ),
    FieldSpec(
        key="salary_negotiable", group="compensation", kind="bool",
        question="Is your expected salary negotiable?",
    ),
    # ── preferences ──────────────────────────────────────────────────
    FieldSpec(
        key="willing_locations", group="preferences", kind="text", required=True,
        question="Which locations would you accept? Comma separated.",
        hint="Include Remote if that is on the table.",
        example="Austin, TX, New York, NY, Remote",
    ),
    FieldSpec(
        key="work_mode", group="preferences", kind="choice",
        question="Which work arrangement do you prefer?",
        choices=["remote", "hybrid", "onsite", "any"],
    ),
    FieldSpec(
        key="willing_to_relocate", group="preferences", kind="bool",
        question="Are you open to relocating?",
    ),
    FieldSpec(
        key="target_titles", group="preferences", kind="text",
        question="Which job titles should the system search for? Comma separated.",
        hint="Used when discovering postings rather than applying to a link you "
             "were handed.",
        example="Software Engineer, Backend Engineer",
    ),
    # ── legal ────────────────────────────────────────────────────────
    FieldSpec(
        key="felony_conviction", group="legal", kind="bool",
        question="Have you ever been convicted of a felony?",
        why="US forms ask this and it must be answered truthfully or not at all. "
            "Never let a system guess here.",
    ),
    FieldSpec(
        key="non_compete_agreement", group="legal", kind="bool",
        question="Are you currently bound by a non-compete agreement?",
    ),
    FieldSpec(
        key="background_check_ok", group="legal", kind="bool",
        question="Do you consent to a background check?",
    ),
    # ── demographics (all optional, whole group skippable) ───────────
    FieldSpec(
        key="gender", group="demographics", kind="choice",
        question="Gender (voluntary)?",
        choices=["Male", "Female", "Non-binary", "Prefer not to say"],
    ),
    FieldSpec(
        key="race_ethnicity", group="demographics", kind="choice",
        question="Race / ethnicity (voluntary)?",
        choices=[
            "Asian",
            "Black or African American",
            "Hispanic or Latino",
            "White",
            "Two or more races",
            "Prefer not to say",
        ],
    ),
    FieldSpec(
        key="veteran_status", group="demographics", kind="choice",
        question="Veteran status (voluntary)?",
        choices=[
            "I am not a protected veteran",
            "I am a protected veteran",
            "Prefer not to say",
        ],
    ),
    FieldSpec(
        key="disability_status", group="demographics", kind="choice",
        question="Disability status (voluntary)?",
        choices=[
            "Yes, I have a disability",
            "No, I do not have a disability",
            "Prefer not to say",
        ],
    ),
]

FIELDS_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in PROFILE_FIELDS}
REQUIRED_KEYS: list[str] = [f.key for f in PROFILE_FIELDS if f.required]
GROUP_ORDER: list[str] = list(GROUP_LABELS)

# Grouped once, in spec order, so nothing has to re-scan the list repeatedly.
FIELDS_BY_GROUP: dict[str, list[FieldSpec]] = {
    group: [f for f in PROFILE_FIELDS if f.group == group] for group in GROUP_ORDER
}

_TRUE = {"yes", "y", "true", "1", "on"}
_FALSE = {"no", "n", "false", "0", "off"}


# ── markdown round trip ──────────────────────────────────────────────

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_LINE = re.compile(r"^\s*[-*]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


def legacy_profile(memory_path: Path) -> dict[str, str]:
    """Read a schema-3 profile out of a `memory.json`, if one is still in there.

    Before this module existed the profile lived inside `memory.json`. Anyone
    upgrading has already answered the setup questions and must not be asked
    again, so the old values are carried across once and then cleared from the
    JSON. Shared by both entry points -- the CLI and `MemoryStore` -- because
    whichever one a user happens to run first should perform the migration.
    """
    try:
        raw = json.loads(Path(memory_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    staged = raw.get("profile")
    if not isinstance(staged, dict):
        return {}
    return {k: v for k, v in staged.items() if isinstance(v, str) and v.strip()}


def _atomic_write(path: Path, text: str):
    concurrency.atomic_write_text(path, text)


def migrate_legacy_profile(profile_store: ProfileStore, memory_path: Path) -> int:
    """Move a schema-3 profile from `memory.json` into `profile.md`.

    Owns *both* halves -- the copy and the removal from the JSON -- because doing
    only the copy leaves a stale duplicate, and a stale duplicate is worse than
    no copy at all: a later reader would have to guess which file is
    authoritative. Both entry points (the CLI and `MemoryStore`) call this rather
    than reimplementing it, so they cannot drift.

    Blanks only. A value already in `profile.md` always wins, so this is safe to
    run on every startup and can never clobber a hand-written correction.

    Returns how many fields were moved.
    """
    staged = legacy_profile(memory_path)
    if not staged:
        return 0

    to_move = {k: v for k, v in staged.items() if not profile_store.value(k)}
    if to_move:
        profile_store.set_many(to_move)

    # Clear the staged copy even if nothing moved -- the values are already
    # represented in profile.md (or were superseded by it), so leaving them here
    # would just be a second, diverging source of truth.
    #
    # Read-modify-write under the same lock the store uses: this rewrites the
    # whole file from a raw copy, so without the lock it would drop whatever
    # another process recorded between this read and this write. It runs at
    # startup, which is exactly when a scheduled pass is likely to be mid-save.
    memory_path = Path(memory_path)
    try:
        with concurrency.exclusive(memory_path.parent, "memory", purpose="profile migration"):
            raw = json.loads(memory_path.read_text(encoding="utf-8"))
            if raw.get("profile"):
                raw["profile"] = {}
                _atomic_write(memory_path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    except (OSError, ValueError, concurrency.LockTimeout):
        # A profile that could not be cleared is not a reason to fail startup:
        # the values are already in profile.md, and the next load will try again.
        return len(to_move)
    return len(to_move)


def parse_profile(text: str) -> dict[str, str]:
    """Read `- key: value` lines out of the markdown.

    Unknown keys are kept rather than dropped: someone who adds
    `security_clearance: TS/SCI` should get it back, not silently lose it. HTML
    comments (the blank-field guidance) are stripped first so the `--` inside
    them cannot confuse the line pattern.
    """
    values: dict[str, str] = {}
    for line in _COMMENT.sub("", text).splitlines():
        match = _LINE.match(line)
        if match:
            key, value = match.group(1), match.group(2)
            values[key] = value.strip()
    return values


def _clean(value: str) -> str:
    """Strip the trailing comment a hand-edit might have left behind."""
    return _COMMENT.sub("", value).strip()


class ProfileStore:
    """Loads, validates and persists `profile.md`."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_profile_path()
        self._values: dict[str, str] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────

    def _load(self):
        if not self.path.exists():
            self._values = {}
            return
        self._values = parse_profile(self.path.read_text(encoding="utf-8"))

    def save(self):
        concurrency.atomic_write_text(self.path, self.render())

    def exists(self) -> bool:
        return self.path.exists()

    # ── reads ────────────────────────────────────────────────────────

    def get(self) -> dict[str, str]:
        """Every stored value, including custom keys."""
        return {k: v for k, v in self._values.items() if v}

    def value(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def unknown_keys(self) -> list[str]:
        return [k for k in self._values if k not in FIELDS_BY_KEY]

    def missing_required(self) -> list[str]:
        return [k for k in REQUIRED_KEYS if not self._values.get(k)]

    def missing_optional(self) -> list[str]:
        return [
            f.key for f in PROFILE_FIELDS
            if not f.required and not self._values.get(f.key)
        ]

    def is_ready(self) -> bool:
        """True when nothing required is missing.

        Deliberately says nothing about whether the values are *sensible* --
        only the user can judge that. This answers one question: would an
        application stall for lack of a fact we did not collect?
        """
        return not self.missing_required()

    # ── writes ───────────────────────────────────────────────────────

    def set_many(self, values: dict) -> dict:
        """Merge values in, normalizing and validating each one.

        Never raises on bad input and never silently drops it: every problem
        comes back in `warnings` so the caller can tell the user what was
        rejected and why.

        The read-modify-write is locked, because `save()` renders the *whole*
        file from `_values` -- which was read when this object was built. A
        process that has been open for hours would otherwise write back its
        first minute's view and drop every field another writer added since. A
        full re-render is right for a file a human edits by hand; doing it from
        a stale read is not.
        """
        with concurrency.exclusive(self.path.parent, "profile", purpose="profile.md write"):
            self._load()
            result = self._apply_many(values)
        for key, note in self._path_warnings():
            result["warnings"].append(note)
        return result

    def _apply_many(self, values: dict) -> dict:
        applied: dict[str, str] = {}
        warnings: list[str] = []
        unknown: list[str] = []

        for key, raw in values.items():
            if raw is None:
                continue
            text = str(raw).strip()
            spec = FIELDS_BY_KEY.get(key)

            if spec is None:
                if text:
                    self._values[key] = text
                    applied[key] = text
                unknown.append(key)
                continue

            if not text:
                # An explicit empty answer clears the field. That is how a user
                # retracts something, so it must not be treated as "no change".
                self._values.pop(key, None)
                applied[key] = ""
                continue

            normalized, problem = self._normalize(spec, text)
            if problem:
                warnings.append(problem)
                continue
            self._values[key] = normalized
            applied[key] = normalized

        self.save()

        return {
            "applied": applied,
            "unknown_keys": unknown,
            "warnings": warnings,
            "missing_required": self.missing_required(),
            "ready": self.is_ready(),
        }

    def _normalize(self, spec: FieldSpec, text: str) -> tuple[str, str]:
        """Return (value, problem). A non-empty problem means reject."""
        if spec.kind == "bool":
            lowered = text.lower()
            if lowered in _TRUE:
                return "yes", ""
            if lowered in _FALSE:
                return "no", ""
            return "", (f"{spec.key}: '{text}' is not a yes/no answer "
                        f"(use yes or no)")

        if spec.kind == "int":
            # Salary and years-of-experience are the two fields where a silent
            # misparse is expensive. "$150,000" and "150k" both mean the same
            # thing to a human, so both must land on 150000 -- and anything we
            # cannot read confidently must be *rejected*, not truncated, because
            # "150k" quietly becoming "150" would put a wrong number on a form.
            if spec.allow_open and text.strip().lower() in _OPEN_PHRASES:
                # Not a number, and deliberately not turned into one. A stored
                # 150000 would be a figure the user never chose, and salary is
                # exactly the field where that gets noticed.
                return OPEN_VALUE, ""

            compact = re.sub(r"[,\s]", "", text)
            compact = re.sub(r"^[$¥€£]", "", compact)
            compact = re.sub(r"(?i)(usd|cny|rmb|eur|gbp|cad|aud|sgd)$", "", compact)

            multiplier = 1
            if compact[-1:] in ("k", "K"):
                multiplier, compact = 1000, compact[:-1]
            elif compact[-1:] in ("m", "M"):
                multiplier, compact = 1_000_000, compact[:-1]

            if not re.fullmatch(r"\d+(\.\d+)?", compact):
                return "", (
                    f"{spec.key}: '{text}' is not a plain number -- "
                    f"write it in digits (e.g. 150000)"
                )
            number = float(compact) * multiplier
            return (str(int(number)) if number == int(number) else str(number)), ""

        if spec.kind == "email":
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                return "", f"{spec.key}: '{text}' does not look like an email"
            return text, ""

        if spec.kind == "phone":
            # Keep whatever the user wrote -- formats vary too much to rewrite --
            # but insist there are enough digits to be a real number.
            if len(re.sub(r"\D", "", text)) < 7:
                return "", f"{spec.key}: '{text}' does not look like a phone number"
            return text, ""

        if spec.kind == "path":
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / candidate
            return str(candidate), ""

        if spec.kind == "choice" and spec.choices:
            for choice in spec.choices:
                if text.lower() == choice.lower():
                    return choice, ""
            # Not a hard failure: the list is a prompt, not an enum. A user with
            # a work authorization we did not think of should still be storable.
            return text, ""

        return text, ""

    def _path_warnings(self) -> list[tuple[str, str]]:
        """Flag file fields that point at nothing.

        A wrong resume path is the classic silent failure: the form fills
        perfectly and then the upload step dies at submit time. Better to say so
        during setup.
        """
        out = []
        for spec in PROFILE_FIELDS:
            if spec.kind != "path":
                continue
            value = self._values.get(spec.key)
            if value and not Path(value).exists():
                out.append((spec.key, f"{spec.key}: no file at {value}"))
        return out

    # ── describing the setup task ────────────────────────────────────

    def describe_field(self, spec: FieldSpec) -> dict:
        return {
            "key": spec.key,
            "question": spec.question,
            "group": spec.group,
            "kind": spec.kind,
            "required": spec.required,
            "choices": spec.choices,
            "hint": spec.hint,
            "why": spec.why,
            "example": spec.example,
            "current": self._values.get(spec.key, ""),
        }

    def questionnaire(
        self,
        include_optional: bool = True,
        only_missing: bool = False,
    ) -> list[dict]:
        """The setup questions, grouped, in the order they should be asked.

        `current` is included on every field so a caller can show what is
        already known and ask only for the rest -- which is what makes running
        setup a second time useful rather than annoying.
        """
        groups = []
        for group in GROUP_ORDER:
            if group == "custom":
                extra = self.unknown_keys()
                if not extra:
                    continue
                groups.append({
                    "group": group,
                    "label": GROUP_LABELS[group],
                    "note": GROUP_NOTES.get(group, ""),
                    "skippable": True,
                    "fields": [
                        {"key": k, "question": f"Custom field {k}?",
                         "kind": "text", "required": False, "choices": [],
                         "hint": "", "why": "", "example": "",
                         "current": self._values.get(k, "")}
                        for k in extra
                    ],
                })
                continue

            specs = FIELDS_BY_GROUP.get(group, [])
            if not include_optional:
                specs = [f for f in specs if f.required]
            if only_missing:
                specs = [f for f in specs if not self._values.get(f.key)]
            if not specs:
                continue

            groups.append({
                "group": group,
                "label": GROUP_LABELS[group],
                "note": GROUP_NOTES.get(group, ""),
                # A group with nothing required in it can be skipped wholesale,
                # which is what keeps a 35-field questionnaire bearable.
                "skippable": not any(f.required for f in specs),
                "fields": [self.describe_field(f) for f in specs],
            })
        return groups

    def status(self) -> dict:
        missing_required = self.missing_required()
        return {
            "profile_path": str(self.path),
            "exists": self.exists(),
            "ready": not missing_required,
            "answered": len(self.get()),
            "missing_required": missing_required,
            "missing_optional": self.missing_optional(),
            "missing_required_questions": [
                FIELDS_BY_KEY[k].question for k in missing_required
            ],
            "unknown_keys": self.unknown_keys(),
        }

    # ── rendering ────────────────────────────────────────────────────

    def render(self) -> str:
        lines = [
            "# ApplyOps Profile",
            "",
            "<!-- 你的投递档案，用来填申请表。可以直接用编辑器改，改完立即生效。",
            "     这个文件已在 .gitignore 里 —— 不要提交，里面有个人信息。",
            "     想补空缺字段就跑 `applyops-init`，它只问空着的。",
            "     全部字段的详细说明见 profile.example.md。 -->",
            "",
        ]
        for group in GROUP_ORDER:
            if group == "custom":
                extra = self.unknown_keys()
                if not extra:
                    continue
                lines.append(f"## {GROUP_LABELS[group]}")
                lines.append("")
                lines.append("<!-- 你自己加的字段，系统原样保存。 -->")
                for key in sorted(extra):
                    lines.append(f"- {key}: {self._values[key]}")
                lines.append("")
                continue

            specs = FIELDS_BY_GROUP.get(group) or []
            if not specs:
                continue
            lines.append(f"## {GROUP_LABELS[group]}")
            lines.append("")
            note = GROUP_NOTES.get(group)
            if note:
                lines.append(f"<!-- {note} -->")
            for spec in specs:
                value = self._values.get(spec.key, "")
                if value:
                    lines.append(f"- {spec.key}: {value}")
                else:
                    lines.append(f"- {spec.key}:  <!-- {_blank_note(spec)} -->")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def example(self) -> str:
        """The annotated reference file, committed to the repo."""
        return render_example()

    def summary(self) -> str:
        """A short human-readable digest, for logs and CLI output."""
        if not self._values:
            return "No profile yet."
        lines = []
        for group in GROUP_ORDER:
            if group == "custom":
                continue
            specs = FIELDS_BY_GROUP.get(group) or []
            shown = [
                f"  {s.key}: {self._values[s.key]}"
                for s in specs if self._values.get(s.key)
            ]
            if shown:
                lines.append(f"{GROUP_LABELS[group]}")
                lines.extend(shown)
        return "\n".join(lines)


def _blank_note(spec: FieldSpec) -> str:
    """The guidance shown next to an unanswered field."""
    parts = [spec.question]
    if spec.choices:
        parts.append("取值：" + " / ".join(spec.choices))
    elif spec.kind == "bool":
        parts.append("取值：yes / no")
    elif spec.kind == "path":
        parts.append("取值：绝对路径")
    if spec.example:
        parts.append(f"示例：{spec.example}")
    return " ｜ ".join(parts)


def _shape_of(spec: FieldSpec) -> str:
    if spec.kind == "bool":
        return "yes / no"
    if spec.kind == "int":
        return "纯数字"
    if spec.kind == "choice":
        return " / ".join(spec.choices)
    if spec.kind == "path":
        return "文件的绝对路径"
    return ""


def render_example() -> str:
    """The annotated reference file, committed to the repo.

    Carries each field's `why`. The setup questionnaire cannot explain itself in
    the moment -- someone being asked "will you need visa sponsorship?" wants to
    know why it matters -- but a file they read once before starting can.
    """
    lines = [
        "# ApplyOps Profile — 字段说明与示例",
        "",
        "这是 `data/profile.md` 的带注释示例。**真正的档案不在这个文件里。**",
        "",
        "两种方式生成你的 `data/profile.md`：",
        "",
        "1. 跑 `applyops-init`（推荐）—— 逐项问你，只问空着的字段；",
        "2. 把这个文件复制成 `data/profile.md` 再手填。",
        "",
        "格式很简单：`## 分组` 标题下面写 `- 字段名: 值`。空值表示「还不知道」，",
        "系统遇到空值会去问你，而**不会自己猜**。",
        "",
        "标 **必填** 的字段缺失时投递会停在半路，建议先填齐。",
        "",
        "> `data/` 整个目录已在 `.gitignore` 里，所以简历也可以直接放进",
        "> `data/`，不会误提交。",
        "",
        "---",
        "",
    ]
    for group in GROUP_ORDER:
        if group == "custom":
            continue
        specs = FIELDS_BY_GROUP.get(group) or []
        if not specs:
            continue
        lines.append(f"## {GROUP_LABELS[group]}")
        lines.append("")
        note = GROUP_NOTES.get(group)
        if note:
            lines.append(f"_{note}_")
            lines.append("")
        for spec in specs:
            lines.append(f"### `{spec.key}` — {'必填' if spec.required else '选填'}")
            lines.append("")
            lines.append(f"- 问题：{spec.question}")
            shape = _shape_of(spec)
            if shape:
                lines.append(f"- 取值：{shape}")
            if spec.hint:
                lines.append(f"- 提示：{spec.hint}")
            if spec.why:
                lines.append(f"- 为什么要问：{spec.why}")
            if spec.example:
                lines.append(f"- 示例：`{spec.example}`")
            lines.append("")
    lines += [
        "---",
        "",
        "## 自定义字段",
        "",
        "想额外记点什么（比如 `security_clearance: TS/SCI`），直接在",
        "`data/profile.md` 里加一行 `- 字段名: 值` 就行。系统会原样保存，",
        "但不会主动拿来填表 —— 除非名字正好对上表单问的问题。",
        "",
    ]
    return "\n".join(lines)


def write_example(path: Path | None = None) -> Path:
    """Regenerate `profile.example.md` from the field spec."""
    target = path or example_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_example(), encoding="utf-8")
    return target
