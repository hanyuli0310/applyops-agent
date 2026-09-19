#!/usr/bin/env python3
"""Batch Easy Apply runner: discover -> filter -> apply -> record.

Personal / local development helper. **Not part of the shipped MCP surface.**

It drives the *real* MCP tool layer (same `build_server()` the shipped server
uses), so the guardrails, the memory flywheel, the locator and the dedupe all
run their production code paths. Only the browser's owner differs: it is
attached over CDP to a long-lived Chrome (`tools/attach.py`) because a real
application needs a tab that survives between decisions.

Two phases
----------
    python tools/auto_apply.py discover            # write data/candidates.json
    python tools/auto_apply.py apply               # consume it, applying

Why the discovery is two passes per keyword
-------------------------------------------
LinkedIn's `f_TPR` (time window) and `f_AL` (Easy Apply) facets do not
reliably compose -- asking for both can silently drop one. So each keyword is
searched twice (Easy Apply, and past-24h) and the *intersection* is kept, then
every surviving card is re-checked against its own "listed X ago" text. A
posting only counts as recent if its own text says so; the URL parameter is a
hint, never the verdict.

Recording
---------
`data/application_log.json` is written after every attempt -- success or
skip. It is the dedupe surface of record: job ids alone are not enough,
because LinkedIn reissues a new job id when a posting is reposted, so a
normalised `company|title` fingerprint is kept alongside.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from applyops import concurrency  # noqa: E402
from applyops.authorization import SubmissionAuthorizer  # noqa: E402
from applyops.mcp.server import RUNTIME, build_server  # noqa: E402
from tools import singlewriter  # noqa: E402
from tools.attach import session  # noqa: E402

DATA = PROJECT_ROOT / "data"
LOG_PATH = DATA / "application_log.json"
CANDIDATES_PATH = DATA / "candidates.json"
PENDING_PATH = DATA / "pending_questions.json"

# Deliberately No RESUME_PATH constant. The batch runner used to hard-code
# `data/resume.pdf`, which meant the file it attached could differ from the one
# configured in the profile while every log line confidently named the hard-coded
# file. There is now one source of truth: the profile's `resume_path`, resolved
# per run through `get_profile`.
RESUME_KEY = "resume_path"

# software / ML / agent -- the three families the user named.
KEYWORDS = [
    "software engineer",
    "machine learning engineer",
    "AI agent engineer",
    "LLM engineer",
    "backend engineer",
    "applied AI engineer",
]
LOCATION = "United States"
MAX_HOURS = 24.0

# How long a posting will wait for a human to approve its submission request.
# Deliberately generous -- an unattended run now *waits for a person* instead of
# approving on their behalf, and a short window would turn that into a silent
# skip of nearly everything.
GRANT_WAIT_SECONDS = 900.0
GRANT_POLL_SECONDS = 3.0


async def wait_for_grant(request_id: str, timeout: float = GRANT_WAIT_SECONDS) -> str:
    """Poll for a human's decision. Returns a grant id, or "" if there is none.

    This is what replaces the runner minting its own permission. It cannot
    approve, only notice -- which is the entire boundary.
    """
    authorizer = SubmissionAuthorizer(DATA)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = authorizer.get_request(request_id)
        if request is not None:
            if request.status == "approved" and request.grant_id:
                return request.grant_id
            if request.status == "rejected":
                return ""
        await asyncio.sleep(GRANT_POLL_SECONDS)
    return ""


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Driver:
    """Calls real MCP tools and gives back parsed JSON."""

    def __init__(self, server) -> None:
        self._server = server

    async def call(self, tool: str, **args) -> dict:
        result = await self._server.call_tool(tool, args)
        text = ""
        for block in getattr(result, "content", None) or []:
            if getattr(block, "text", None) is not None:
                text += block.text
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = {"raw": text}
        if isinstance(payload, dict) and payload.get("error"):
            log_line(f"    ! {tool}: {payload['error']}")
        return payload if isinstance(payload, dict) else {"raw": payload}


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return default


def _write(path: Path, payload) -> None:
    # Atomic, because `write_text` truncates first: a crash mid-write leaves
    # half a file, and for the ledger that is the dedupe surface of record --
    # the thing that stops the same posting being applied to twice.
    concurrency.atomic_write_json(path, payload)


# --------------------------------------------------------------------------
# recency
# --------------------------------------------------------------------------

_AGE_RE = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)", re.I)


def age_hours(text: str) -> float | None:
    """Hours since listing, parsed from LinkedIn's own 'listed X ago' text.

    Returns None when the text says nothing usable -- an unknown age is never
    treated as recent, and never as old; the caller decides.
    """
    if not text:
        return None
    low = text.lower()
    if "just now" in low or "moment" in low:
        return 0.0
    m = _AGE_RE.search(low)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    return {
        "minute": n / 60.0,
        "hour": float(n),
        "day": n * 24.0,
        "week": n * 168.0,
        "month": n * 720.0,
        "year": n * 8760.0,
    }[unit]


def is_recent(job: dict) -> bool:
    # Strictly less than, not "<=": LinkedIn rounds up, so "1 day ago" can mean
    # anything from 24 to 47 hours. Admitting it would quietly break the
    # 24-hour rule the user asked for.
    age = age_hours(job.get("listed", ""))
    return age is not None and age < MAX_HOURS


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------


# Relevance is a filter, not a ranking of taste. It exists because a day's
# quota is finite: spending it on "Sales Development Representative" costs the
# same as spending it on an agent engineering role, so the obviously-off-target
# postings are dropped before anything is submitted. Anything borderline is
# KEPT -- dropping a real match is worse than submitting one extra.
_INCLUDE = re.compile(
    r"(software|machine learning|\bml\b|\bai\b|agent|backend|full[- ]?stack|"
    r"data engineer|forward deployed|founding|platform|infrastructure|devops|"
    r"cloud|python|reliability|\bsre\b|research engineer|developer)",
    re.I,
)
# Seniority is part of the same filter. The user is a new grad (MS CS, ~2 yrs
# including internships); a Staff or Principal req is not a stretch application,
# it is a wasted slot in a finite daily quota, so those titles are dropped.
_EXCLUDE = re.compile(
    r"(sales|presales|business development|account executive|recruit|"
    r"headhunter|program manager|quality assurance|\bqa\b|\bqa[ /]|"
    r"test engineer|in test|\bsdet\b|intern\b|internship|embedded|firmware|"
    r"hardware|sensor|\bplm\b|3dexperience|solutions architect|"
    r"business intelligence|consultant|payload|"
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|\bprincipal\b|\blead\b|\bdirector\b|"
    r"\bmanager\b|\bhead of\b|\bvp\b|\barchitect\b|\biii\b|\biv\b|"
    # Hard disqualifiers, spelled out in the title. These are not taste
    # judgements: the user is on an F-1 (CPT/OPT), so a posting that demands
    # citizenship or a clearance is unwinnable, and one that states a years
    # requirement is by definition not the entry level he asked for. Spending a
    # slot from a finite daily quota on either is pure waste.
    r"\d+\s*\+?\s*(?:years|yrs)|\bno\s+(?:cpt|opt)\b|no\s+cpt\s*/\s*opt|"
    r"u\.?s\.?\s*citizen|citizenship\s+required|security\s+clearance|"
    r"clearance\s+required)",
    re.I,
)
_TOO_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|director)\b", re.I)
# Positive signals for the level the user is actually applying at.
_ENTRY = re.compile(
    r"(new grad|new-grad|entry[- ]level|entry level|junior|\bjr\.?\b|"
    r"\bassociate\b|graduate|university|campus|early career|"
    r"\bengineer i\b|\bengineer 1\b|software engineer\b)",
    re.I,
)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def relevance(job: dict) -> tuple[bool, str]:
    """Is this worth a slot in a finite daily quota?"""
    title = job.get("title") or ""
    if _CJK.search(title):
        return False, "non-English title; needs human judgement"
    if _EXCLUDE.search(title):
        return False, f"off-target title: {_EXCLUDE.search(title).group(0)!r}"
    if not _INCLUDE.search(title):
        return False, "title matches none of software / ML / agent / backend"
    return True, ""


def relevance_score(job: dict) -> int:
    """Rank what survives: agent/AI first, then backend, then generic SWE."""
    title = (job.get("title") or "").lower()
    score = 0
    if re.search(r"\bagent", title):
        score += 3
    if re.search(r"\bai\b|machine learning|\bml\b|llm|genai", title):
        score += 3
    if re.search(r"backend|platform|infrastructure|harness", title):
        score += 2
    if re.search(r"forward deployed|founding", title):
        score += 1
    if _ENTRY.search(title):
        score += 2
    if _TOO_SENIOR.search(title):
        score -= 5
    return score


def fingerprint(job: dict) -> str:
    """Stable identity for a posting that survives a repost.

    LinkedIn mints a fresh job id when a listing is reposted, so dedupe on the
    id alone leaks. Company + normalised title catches the repost; heavy words
    like "senior"/"remote" are dropped so cosmetic retitling still matches.
    """
    company = re.sub(r"[^a-z0-9]+", "", (job.get("company") or "").lower())
    title = (job.get("title") or "").lower()
    title = re.sub(
        r"\b(senior|sr|junior|jr|mid|level|staff|principal|remote|hybrid|onsite|us|usa)\b",
        " ",
        title,
    )
    title = re.sub(r"[^a-z0-9]+", "", title)
    return f"{company}|{title}"


# A skip is a judgement about one attempt, not a verdict on the posting. These
# reasons are the ones a later run can plausibly fix -- a question the flywheel
# could not answer yet, a form that stalled on a field we have since learned to
# fill. Everything else (an application already sent, a preflight refusal) is
# final and stays final.
RETRYABLE_PREFIXES = (
    "unanswered required question",
    "could not answer",
    "required radio group unanswered",
    "form stopped responding",
    "form did not reach a submit control",
    "no Easy Apply entry on the posting",
)
MAX_RETRIES = 2
# Consecutive transport/tool failures after which the run stops. Three dead
# postings in a row is not three unlucky postings; it is a dead browser, an
# expired session or a changed tool contract, and the run can neither diagnose
# nor fix that -- it can only write more ledger rows that look like work.
MAX_CONSECUTIVE_ERRORS = 3
# Distinct cap for transport failures, deliberately larger than MAX_RETRIES: it
# takes repeated *harness* failure, not two unlucky attempts, to give up on a
# posting whose form was never even read.
MAX_ERRORS = 5


def job_from_skip(skip: dict) -> dict:
    """Rebuild a queue entry from a ledger skip, so a retry needs no re-scrape."""
    return {
        "job_id": skip.get("job_id"),
        "url": skip.get("job_url"),
        "title": skip.get("job_title"),
        "company": skip.get("company"),
        "location": skip.get("location"),
        "listed": skip.get("listed"),
        "easy_apply": skip.get("easy_apply"),
        "found_via": skip.get("found_via"),
    }


def _row_key(row: dict) -> str:
    """What makes two ledger rows the same row: the posting they describe."""
    return str(
        row.get("fingerprint")
        or row.get("job_id")
        or row.get("job_url")
        or row.get("url")
        or json.dumps(row, sort_keys=True, default=str)
    )


def _row_evidence(row: dict) -> int:
    """How much this row knows. Higher wins when two rows describe one posting."""
    return (
        int(row.get("retries") or 0)
        + int(row.get("error_count") or 0)
        + (1 if str(row.get("outcome") or "").startswith("submitted") else 0)
    )


def _merge_rows(older, newer) -> list[dict]:
    """Union two row lists keyed by posting, better-evidenced row winning.

    Deliberately not a plain concatenation. The ledger is read back to answer
    "have I already done this posting", so N copies of one posting would inflate
    the totals and make the retry queue look longer than the work. Where two
    rows disagree the one that recorded more attempts wins, which can only
    under-report how many times we tried -- the same direction the memory file
    resolves conflicts in, and for the same reason: inventing evidence is worse
    than missing some.
    """
    merged: dict[str, dict] = {}
    for row in list(older or []) + list(newer or []):
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        current = merged.get(key)
        if current is None or _row_evidence(row) >= _row_evidence(current):
            merged[key] = row
    return list(merged.values())


class Ledger:
    """The JSON record of every attempt, and the dedupe surface built on it."""

    def __init__(self) -> None:
        self.data = _load(
            LOG_PATH,
            {"created": _now(), "max_hours": MAX_HOURS, "applications": [], "skipped": []},
        )
        self.data.setdefault("applications", [])
        self.data.setdefault("skipped", [])

    @staticmethod
    def is_settled(skip: dict) -> bool:
        """Whether this skip should stop future runs from looking at the posting."""
        # A transport or tool failure says nothing about the posting -- the form
        # was never read. Letting one settle would silently blacklist every
        # posting that happened to be in the queue when the browser died, which
        # is exactly what the first unattended run did: 20 rows, all
        # `UnexpectedToolError: Error executing tool browser_open`. Counted on
        # its own axis (MAX_ERRORS) rather than against the posting's retries.
        if str(skip.get("outcome") or "") == "error":
            return int(skip.get("error_count") or 0) >= MAX_ERRORS
        if int(skip.get("retries") or 0) >= MAX_RETRIES:
            return True
        reason = str(skip.get("reason") or "")
        return not any(reason.startswith(p) for p in RETRYABLE_PREFIXES)

    def retry_queue(self) -> list[dict]:
        """Skips worth another attempt, oldest first, capped by MAX_RETRIES."""
        return [s for s in self.data["skipped"] if not self.is_settled(s)]

    def seen_ids(self) -> set[str]:
        out = set()
        for a in self.data["applications"]:
            if a.get("job_id"):
                out.add(str(a["job_id"]))
            for jid in a.get("job_ids_seen") or []:
                out.add(str(jid))
        for s in self.data["skipped"]:
            if s.get("job_id") and self.is_settled(s):
                out.add(str(s["job_id"]))
        return out

    def seen_prints(self) -> set[str]:
        out = set()
        for a in self.data["applications"]:
            if a.get("fingerprint"):
                out.add(a["fingerprint"])
        for s in self.data["skipped"]:
            if s.get("fingerprint") and self.is_settled(s):
                out.add(s["fingerprint"])
        return out

    def known(self, job: dict) -> str | None:
        """Why this posting is already known, or None if it is new."""
        if str(job.get("job_id", "")) in self.seen_ids():
            return "job_id already in the ledger"
        fp = fingerprint(job)
        if fp in self.seen_prints():
            return f"fingerprint already in the ledger ({fp})"
        return None

    def record_application(self, entry: dict) -> None:
        self.data["applications"].append(entry)
        self.flush()

    def record_skip(self, entry: dict) -> None:
        self.data["skipped"].append(entry)
        self.flush()

    def flush(self) -> None:
        """Persist, merging with whatever another process has added meanwhile.

        The ledger is rewritten in full after every attempt, so with two writers
        -- a scheduled pass and a manual run -- the second one to save used to
        delete the first one's rows. Those rows are what say "already applied",
        so losing them is not a lost statistic: it is the same posting getting
        submitted twice.

        The totals are recomputed from the merged rows rather than carried
        across, so `applied_total` can never disagree with the rows it counts.
        """
        with concurrency.exclusive(DATA, "ledger", purpose="application_log.json"):
            on_disk = _load(LOG_PATH, {})
            merged = dict(on_disk) if isinstance(on_disk, dict) else {}
            for field in ("applications", "skipped"):
                merged[field] = _merge_rows(merged.get(field), self.data.get(field))
            for key, value in self.data.items():
                if key not in ("applications", "skipped"):
                    merged[key] = value
            merged["last_updated"] = _now()
            merged["applied_total"] = len(
                [a for a in merged["applications"] if a.get("outcome") == "submitted"]
            )
            merged["skipped_total"] = len(merged["skipped"])
            _write(LOG_PATH, merged)
            # Adopt the merged view, so the next flush merges against what is
            # actually on disk instead of against this process's own row set.
            self.data = merged


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


async def discover(driver: Driver, keywords: list[str], location: str) -> list[dict]:
    found: dict[str, dict] = {}
    for kw in keywords:
        for easy, days in ((True, 0), (True, 1)):
            res = await driver.call(
                "discover_jobs",
                keywords=kw,
                location=location,
                limit=25,
                easy_apply_only=easy,
                recent_days=days,
            )
            jobs = res.get("jobs") or []
            fresh = [j for j in jobs if is_recent(j)]
            log_line(
                f"  {kw[:26]:<26} easy={int(easy)} tpr={days}d -> "
                f"{len(jobs)} cards, {len(fresh)} within {MAX_HOURS:.0f}h"
            )
            for j in fresh:
                j.setdefault("found_via", kw)
                found[str(j["job_id"])] = j
            await asyncio.sleep(1.5)
    return list(found.values())


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------

NEXT_WORDS = ("continue to next step", "next", "review your application", "continue")
SUBMIT_WORDS = ("submit application",)
SAFETY_WORDS = ("continue applying",)

# LinkedIn labels its carousel arrows just "Next" / "Previous" / "Back". They
# are real buttons in the listing, they sit above the apply dialog in DOM
# order, and clicking them throws (the shadow-root outlet intercepts the
# pointer). Matching "Next" loosely therefore aims the form at a decorative
# control. A form's own advance button always says what it does, so the
# shortest generic names are dropped and the longest match wins.
_GENERIC_NAMES = {"next", "previous", "back", "continue", "close", "dismiss"}


def pick(buttons: list[dict], words: tuple[str, ...]) -> str | None:
    """The button named by the earliest matching `words` entry.

    `words` is ordered by intent, not alphabetically, so the caller decides
    which control to prefer. Within one word the longest name wins, because a
    longer label is a more specific one.
    """
    for w in words:
        hits = [
            (b.get("name") or "").strip()
            for b in buttons
            if w in (b.get("name") or "").lower()
            and (b.get("name") or "").strip().lower() not in _GENERIC_NAMES
        ]
        if hits:
            return max(hits, key=len)
    return None


# Selects render a placeholder as their value, so "is this answered?" cannot be
# a non-empty check. LinkedIn labels its placeholder "Select an option".
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "select an option",
        "select an option...",
        "select...",
        "please select",
        "choose an option",
        "choose...",
        "--",
        "-",
        "select",
    }
)


def is_blank(field: dict) -> bool:
    """Whether a single (non-radio) required control is still unanswered.

    Two traps, one per control family:
      * a dropdown reports the *placeholder* as its value, so a non-empty check
        reads "already answered" and the run walks past a required question
        straight into LinkedIn's own validation error;
      * a checkbox is answered by being checked, never by its value.
    Radios are handled per-group by `unanswered_radio_groups`, because no single
    member of a group can be judged on its own.
    """
    ftype = (field.get("field_type") or "").lower()
    if ftype == "checkbox":
        return not field.get("checked")
    return (field.get("value") or "").strip().lower() in _PLACEHOLDER_VALUES


def radio_group_key(ref: str) -> str:
    """The question a radio belongs to.

    LinkedIn names an Easy Apply screening control
    `id=urn:li:fsd_formElement:urn:li:jobs_applyformcommon_easyApplyFormElement:(<job>,<question>,multipleChoice)-<index>`,
    so everything up to the trailing option index identifies the question. Judging
    a radio on its own would call every unselected member "blank" -- including the
    sibling of an option that is already selected.
    """
    return re.sub(r"-\d+$", "", ref or "")


def unanswered_radio_groups(fields: list[dict]) -> list[dict]:
    """Required radio groups with nothing selected, one entry per question."""
    groups: dict[str, list[dict]] = {}
    for f in fields:
        if (f.get("field_type") or "").lower() == "radio":
            groups.setdefault(radio_group_key(f.get("ref") or ""), []).append(f)

    out: list[dict] = []
    for members in groups.values():
        if any(m.get("checked") for m in members):
            continue
        if not any(m.get("required") for m in members):
            continue
        first = dict(members[0])
        # The group's option labels are the only vocabulary we have for it.
        first["options"] = [m.get("label") for m in members if m.get("label")]
        out.append(first)
    return out


def choose_option(options: list[str], answer: str) -> str:
    """Map a stored answer onto one of the field's own option strings.

    The stored answer is the value the user stated ("3"), while the widget offers
    prose ("3 years", "3-5 years"). Both are right, so the answer is translated
    rather than re-typed blindly -- and if nothing matches, the answer is
    returned untouched rather than guessed at.
    """
    clean = [o.strip() for o in options if o and o.strip().lower() not in _PLACEHOLDER_VALUES]
    if not clean:
        return answer
    want = (answer or "").strip()
    low = want.lower()
    for option in clean:
        if option.lower() == low:
            return option

    # A numeric answer matches an option that leads with the same number: "3"
    # lands on "3 years", and also on "3-5 years" (an inclusive range the user's
    # 3 falls inside) rather than being typed into a control that has no "3".
    want_nums = re.findall(r"\d+(?:\.\d+)?", want)
    if want_nums:
        for option in clean:
            option_nums = re.findall(r"\d+(?:\.\d+)?", option)
            if option_nums and option_nums[0] == want_nums[0]:
                return option

    for option in clean:
        if low in option.lower() or option.lower() in low:
            return option
    return answer


async def answer_field(driver: Driver, field: dict, answer: str) -> tuple[bool, str]:
    """Put one answer into one control, using the action that control expects.

    `fill_field` types keystrokes, which is right for a text input and wrong for
    a dropdown: there is nothing to type into, and the keystrokes go to the page
    instead (landing on whatever has focus, which is how a stray character ends
    up in a search box). Radios are refused outright rather than clicked -- the
    option refs in `browser_state` carry no question text, so there is no way to
    prove which Yes/No pair a click would belong to, and a wrong answer on a real
    application is worse than an application not sent.
    """
    ftype = (field.get("field_type") or "").lower()
    ref = field.get("ref") or ""

    if ftype in ("select", "combobox", "listbox"):
        choice = choose_option(list(field.get("options") or []), answer)
        res = await driver.call(
            "select_option",
            ref=ref,
            value=choice,
            role="form_answer",
            reason="answered from the memory flywheel",
        )
        return bool(res.get("ok")), choice

    if ftype == "checkbox":
        res = await driver.call(
            "set_checkbox",
            ref=ref,
            checked=True,
            role="form_answer",
            reason="answered from the memory flywheel",
        )
        return bool(res.get("ok")), answer

    if ftype == "radio":
        return False, "radio controls carry no question text; not clicked blind"

    res = await driver.call(
        "fill_field",
        ref=ref,
        value=answer,
        role="form_answer",
        reason="answered from the memory flywheel",
    )
    return bool(res.get("ok")), answer


async def answer_or_bail(
    driver: Driver, label: str, pending: list[dict], job: dict, field: dict | None = None
):
    """Ask the flywheel. Anything it has not earned -> recorded, not guessed."""
    res = await driver.call("get_answer", question=label)
    status = res.get("status")
    if status == "answered" and res.get("answer"):
        await driver.call(
            "record_answer",
            question=label,
            answer=res["answer"],
            context="applied from memory during batch run",
        )
        return res["answer"]
    row = {
        "asked_at": _now(),
        "question": label,
        "job_id": job.get("job_id"),
        "company": job.get("company"),
        "title": job.get("title"),
        "flywheel_status": status,
        "confidence": res.get("confidence"),
    }
    # The allowed vocabulary matters to the human: answering "3" when the widget
    # only offers "3-5 years" is the difference between a filed application and
    # another skip.
    options = [o for o in ((field or {}).get("options") or []) if o and o.strip()]
    if options:
        row["options"] = options
    if res.get("answer"):
        row["flywheel_suggested"] = res["answer"]
    pending.append(row)
    return None


async def apply_one(driver: Driver, job: dict, ledger: Ledger, pending: list[dict]) -> dict:
    job_url = job["url"]
    job_id = str(job.get("job_id", ""))
    # The resume now comes from the profile, resolved once per job. No default:
    # a missing resume must fail this posting loudly rather than attach whatever
    # happens to sit at a hard-coded path.
    profile = await driver.call("get_profile")
    resume_path = (profile.get("profile") or {}).get(RESUME_KEY) or ""
    entry = {
        "attempted_at": _now(),
        "job_id": job_id,
        "job_url": job_url,
        "job_title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "listed": job.get("listed"),
        "easy_apply": job.get("easy_apply"),
        "fingerprint": fingerprint(job),
        "found_via": job.get("found_via"),
        "outcome": "unknown",
        "steps": 0,
        "answers_used": [],
        "notes": [],
    }

    pre = await driver.call("preflight", job_url=job_url, job_id=job_id)
    if not pre.get("allowed"):
        entry["outcome"] = "skipped"
        entry["reason"] = pre.get("reason", "preflight refused")
        log_line(f"  skip {job.get('company')}: {entry['reason']}")
        return entry

    if not resume_path:
        entry["outcome"] = "skipped"
        entry["reason"] = f"no resume configured (set `{RESUME_KEY}` in data/profile.md)"
        log_line(f"  skip {job.get('company')}: {entry['reason']}")
        return entry

    await driver.call("browser_open", url=job_url)
    await asyncio.sleep(2.5)
    state = await driver.call("browser_state")
    opened = pick(state.get("buttons") or [], ("easy apply",))
    if not opened:
        entry["outcome"] = "skipped"
        entry["reason"] = "no Easy Apply entry on the posting"
        return entry
    entered = await driver.call(
        "click_target", name=opened, role="apply_entry", reason="start the application"
    )
    if not entered.get("clicked"):
        entry["outcome"] = "skipped"
        entry["reason"] = f"could not click {opened!r}: {entered.get('error')}"
        return entry
    await asyncio.sleep(3.0)

    last_key: tuple | None = None
    stall = 0
    for step in range(10):
        state = await driver.call("browser_state")
        entry["steps"] = step + 1
        fields = state.get("fields") or []
        buttons = state.get("buttons") or []
        names = [b.get("name") or "" for b in buttons]

        # If a click "succeeded" but nothing moved, pressing on just burns
        # quota and looks like a hung run. Two identical pages in a row is
        # enough to call it.
        page_key = (state.get("url"), tuple(sorted(names)))
        if page_key == last_key:
            stall += 1
            if stall >= 2:
                entry["outcome"] = "skipped"
                entry["reason"] = "form stopped responding; page did not change"
                return entry
        else:
            stall = 0
            last_key = page_key

        # LinkedIn sometimes interposes a "job search safety" dialog.
        safety = pick(buttons, SAFETY_WORDS)
        if safety:
            res = await driver.call(
                "click_target",
                name=safety,
                role="safety_dialog_continue",
                reason="dismiss the safety reminder",
            )
            if not res.get("clicked"):
                entry["outcome"] = "skipped"
                entry["reason"] = f"could not dismiss the safety dialog: {res.get('error')}"
                return entry
            await asyncio.sleep(2.5)
            continue

        if pick(buttons, SUBMIT_WORDS):
            break

        # Resume page: LinkedIn keeps the last upload selected, so only upload
        # when nothing is selected.
        labels = [(f.get("label") or "").lower() for f in fields]
        if any("resume" in l for l in labels) and not any(
            "deselect resume" in l for l in labels
        ):
                up = await driver.call(
                    "upload_file",
                    ref="css=input[type=file]",
                    file_path=str(resume_path),
                    role="resume_upload",
                    reason="no resume selected on this posting",
                )
        entry["notes"].append(
            f"resume attached: {up.get('verification')} "
            f"({up.get('attachments') or 'no file reported'})"
        )
        if up.get("resume_match") is False:
            entry["notes"].append(
                f"resume mismatch: {up.get('resume_detail') or 'unknown reason'}"
            )
            await asyncio.sleep(3.0)
            continue

        # Anything required and still unanswered needs an answer we can defend.
        blockers = [f for f in fields if f.get("required") and is_blank(f)]
        radios = unanswered_radio_groups(fields)
        if radios:
            first = radios[0]
            entry["outcome"] = "skipped"
            entry["reason"] = (
                f"required radio group unanswered ({first.get('options')}); "
                "`browser_state` does not expose the question text for a radio "
                "option, so there is nothing to ask the human about"
            )
            return entry

        if blockers:
            first = blockers[0]
            label = (first.get("label") or "").strip()
            ftype = (first.get("field_type") or "").lower()
            answer = await answer_or_bail(driver, label, pending, job, field=first)
            if answer is None:
                entry["outcome"] = "skipped"
                entry["reason"] = f"unanswered required question: {label!r}"
                entry["notes"].append(
                    "left for the human rather than guessed; no submission made"
                )
                return entry
            ok, used = await answer_field(driver, first, answer)
            if not ok:
                entry["outcome"] = "skipped"
                entry["reason"] = (
                    f"could not answer {label!r}: {used} "
                    f"(field_type={ftype}, options={first.get('options')})"
                )
                return entry
            entry["answers_used"].append(label)
            entry["notes"].append(f"{label} -> {used}")
            await asyncio.sleep(1.0)
            continue

        advance = pick(buttons, NEXT_WORDS)
        if not advance:
            entry["outcome"] = "skipped"
            entry["reason"] = f"no next/submit control found; saw {names[:6]}"
            return entry
        moved = await driver.call(
            "click_target", name=advance, role="modal_next", reason="advance the form"
        )
        if not moved.get("clicked"):
            entry["outcome"] = "skipped"
            entry["reason"] = f"could not click {advance!r}: {moved.get('error')}"
            return entry
        await asyncio.sleep(3.0)
    else:
        entry["outcome"] = "skipped"
        entry["reason"] = "form did not reach a submit control"
        return entry

    # ── the final submit ─────────────────────────────────────────────
    # This used to be `click_target(name="Submit application")` followed by
    # `submit_application(acknowledged=True)`. The click was the real submission
    # and it was completely ungated: the token only gated the *bookkeeping* after
    # the external side effect had already happened. A batch runner additionally
    # has nobody watching it, so "the user approved" was a constant True.
    #
    # Now: open a request bound to the live form, wait for a human to approve it
    # out-of-band (`python -m applyops.approve <id>`), then hand the resulting
    # grant to submit_final -- which is the only thing that ever clicks.
    req = await driver.call(
        "request_submission_grant", job_url=job_url, job_id=job_id, route="easy_apply"
    )
    request_id = req.get("request_id")
    if not request_id:
        entry["outcome"] = "skipped"
        entry["reason"] = f"no submission request created: {req.get('error') or req}"
        return entry

    summary = req.get("summary_to_show", "")
    print("\n" + summary + "\n", flush=True)
    entry["request_id"] = request_id
    log_line(f"  waiting for human approval of request {request_id}")

    grant_id = await wait_for_grant(request_id, timeout=GRANT_WAIT_SECONDS)
    if not grant_id:
        entry["outcome"] = "awaiting_approval"
        entry["reason"] = (
            "no approval within the wait window; nothing was submitted. Approve "
            f"with `python -m applyops.approve {request_id}` and retry this posting."
        )
        log_line(f"  skip {job.get('company')}: {entry['reason']}")
        return entry

    outcome = await driver.call(
        "submit_final",
        grant_id=grant_id,
        job_url=job_url,
        job_id=job_id,
        route="easy_apply",
    )
    status = outcome.get("status") or "unverified"
    entry["confirmation_id"] = grant_id
    entry["page_verified"] = status == "verified"
    entry["notes"].append(f"submit_final status: {status}")
    if outcome.get("error") or outcome.get("reason"):
        entry["notes"].append(f"submit_final: {outcome.get('error') or outcome.get('reason')}")
    entry["outcome"] = {
        "verified": "submitted",
        "unverified": "submitted_unverified",
        "failed": "failed",
    }.get(status, "submitted_unverified")
    return entry


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------


async def phase_discover(keywords: list[str], location: str, reuse: bool = False) -> None:
    server = build_server()
    ledger = Ledger()
    jobs: list[dict] = []

    if reuse:
        jobs = _load(CANDIDATES_PATH, {"jobs": []}).get("jobs", [])
        log_line(f"re-filtering {len(jobs)} already-discovered card(s); no new search")
    else:
        async with session(9222) as controller:
            RUNTIME._browser = controller  # noqa: SLF001
            try:
                jobs = await discover(Driver(server), keywords, location)
            finally:
                RUNTIME._browser = None  # noqa: SLF001

    if not jobs:
        log_line("nothing discovered")
        return

    ranked: list[dict] = []
    seen_prints: dict[str, dict] = {}
    # Newest first, so the within-batch duplicate check keeps the earliest
    # sighting of a reposted listing rather than an arbitrary one.
    for j in sorted(jobs, key=lambda x: (x.get("listed") or "")):
        why = ledger.known(j)
        if not why:
            fp = fingerprint(j)
            twin = seen_prints.get(fp)
            if twin is not None:
                why = f"same posting as job {twin['job_id']} (reposted with a new id)"
            else:
                seen_prints[fp] = j
        if not why:
            _ok, why = relevance(j)
        ranked.append({**j, "_skip_reason": why, "_score": relevance_score(j)})

    ranked.sort(key=lambda j: (-j["_score"], j.get("listed", "")))
    _write(
        CANDIDATES_PATH,
        {
            "discovered_at": _now(),
            "keywords": keywords,
            "location": location,
            "max_hours": MAX_HOURS,
            "count": len(ranked),
            "jobs": ranked,
        },
    )
    queued = [j for j in ranked if not j["_skip_reason"]]
    log_line(
        f"{len(ranked)} card(s): {len(queued)} queued, "
        f"{len(ranked) - len(queued)} filtered -> {CANDIDATES_PATH}"
    )


async def phase_apply(limit: int) -> None:
    server = build_server()
    async with session(9222) as controller:
        RUNTIME._browser = controller  # noqa: SLF001
        try:
            driver = Driver(server)
            ledger = Ledger()
            pending = _load(PENDING_PATH, [])
            cand = _load(CANDIDATES_PATH, {"jobs": []})
            queue = [j for j in cand.get("jobs", []) if not j.get("_skip_reason")]
            log_line(f"queue: {len(queue)} fresh candidate(s)")

            done = 0
            errs = 0
            for job in queue:
                if limit and done >= limit:
                    break
                log_line(
                    f"-> {job.get('company')} | {job.get('title')} "
                    f"| {job.get('listed')}"
                )
                try:
                    entry = await apply_one(driver, job, ledger, pending)
                except Exception as exc:  # noqa: BLE001 - one bad form must not kill the run
                    entry = {
                        "attempted_at": _now(),
                        "job_id": str(job.get("job_id", "")),
                        "job_url": job.get("url"),
                        "job_title": job.get("title"),
                        "company": job.get("company"),
                        "fingerprint": fingerprint(job),
                        "outcome": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                    log_line(f"  error: {entry['reason']}")

                (ledger.record_application if entry["outcome"].startswith("submitted")
                 else ledger.record_skip)(entry)
                _write(PENDING_PATH, pending)
                log_line(f"  outcome: {entry['outcome']}  ({entry.get('reason','')})")

                if entry["outcome"].startswith("submitted"):
                    done += 1
                errs = errs + 1 if entry["outcome"] == "error" else 0
                if errs >= MAX_CONSECUTIVE_ERRORS:
                    log_line(
                        f"{errs} consecutive tool failures - the harness is broken, not the "
                        "postings. Stopping."
                    )
                    break
                if entry.get("reason") and "daily cap" in str(entry.get("reason")):
                    log_line("daily cap reached - stopping")
                    break
                if entry.get("reason") and "halted" in str(entry.get("reason")):
                    log_line("guardrails halted the run - stopping")
                    break
                await asyncio.sleep(2.0)

            log_line(
                f"done: {done} submitted, "
                f"{len(ledger.data['skipped'])} skipped -> {LOG_PATH}"
            )
        finally:
            RUNTIME._browser = None  # noqa: SLF001


async def phase_retry(limit: int) -> None:
    """Re-attempt the postings skipped for a reason this run can fix.

    The first batch skipped 18 postings: 12 for a question the flywheel could not
    answer yet (it can now) and 6 because the form would not advance -- and the
    form would not advance because a required dropdown was still showing its
    placeholder, which passed for an answer. Nothing was sent, so none of those
    postings has been applied to; `MAX_RETRIES` keeps a genuinely broken one from
    being retried forever.
    """
    server = build_server()
    async with session(9222) as controller:
        RUNTIME._browser = controller  # noqa: SLF001
        try:
            driver = Driver(server)
            ledger = Ledger()
            pending = _load(PENDING_PATH, [])
            queue = ledger.retry_queue()
            log_line(f"retry queue: {len(queue)} unsettled skip(s)")

            done = 0
            errs = 0
            for skip in queue:
                if limit and done >= limit:
                    break
                job = job_from_skip(skip)
                log_line(f"-> retry {job.get('company')} | {job.get('title')}")
                try:
                    entry = await apply_one(driver, job, ledger, pending)
                except Exception as exc:  # noqa: BLE001 - one bad form must not kill the run
                    entry = {
                        "attempted_at": _now(),
                        "job_id": str(job.get("job_id", "")),
                        "job_url": job.get("url"),
                        "job_title": job.get("title"),
                        "company": job.get("company"),
                        "fingerprint": fingerprint(job),
                        "outcome": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                    log_line(f"  error: {entry['reason']}")

                prior_retries = int(skip.get("retries") or 0)
                failed_harness = entry["outcome"] == "error"
                # A harness failure is not an attempt at the posting, so it must
                # not consume the posting's retry budget -- otherwise one browser
                # outage permanently discards everything that was queued.
                entry["retries"] = prior_retries if failed_harness else prior_retries + 1
                if failed_harness:
                    entry["error_count"] = int(skip.get("error_count") or 0) + 1
                entry["previous_reason"] = skip.get("reason")
                # One line per posting: drop the superseded skip before recording
                # the new outcome, or the ledger accumulates contradictory rows
                # for the same job id.
                ledger.data["skipped"] = [s for s in ledger.data["skipped"] if s is not skip]
                if entry["outcome"].startswith("submitted"):
                    ledger.record_application(entry)
                    done += 1
                else:
                    ledger.record_skip(entry)
                _write(PENDING_PATH, pending)
                log_line(f"  outcome: {entry['outcome']}  ({entry.get('reason','')})")

                stop, why = _should_stop(entry)
                if stop:
                    log_line(why)
                    break
                errs = errs + 1 if entry["outcome"] == "error" else 0
                if errs >= MAX_CONSECUTIVE_ERRORS:
                    log_line(
                        f"{errs} consecutive tool failures - the harness is broken, not the "
                        "postings. Stopping."
                    )
                    break
                await asyncio.sleep(2.0)

            log_line(f"retry done: {done} submitted -> {LOG_PATH}")
        finally:
            RUNTIME._browser = None  # noqa: SLF001


def _should_stop(entry: dict) -> tuple[bool, str]:
    reason = str(entry.get("reason") or "")
    if "daily cap" in reason:
        return True, "daily cap reached - stopping"
    if "halted" in reason:
        return True, "guardrails halted the run - stopping"
    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("discover", "apply", "retry"))
    parser.add_argument("--keywords", default="", help="comma-separated; default: curated set")
    parser.add_argument("--location", default=LOCATION)
    parser.add_argument("--limit", type=int, default=0, help="stop after N submissions")
    parser.add_argument(
        "--refilter",
        action="store_true",
        help="re-apply filters to data/candidates.json without searching again",
    )
    opts = parser.parse_args()

    keywords = [k.strip() for k in opts.keywords.split(",") if k.strip()] or KEYWORDS

    # The scheduled loop drives the same tab, and the same Chrome profile.
    # Whichever of the two starts first wins and the other is told who it lost
    # to, rather than both clicking through one page and writing an outcome
    # that belongs to neither.
    lock = singlewriter.acquire(purpose=f"auto_apply {opts.phase}")
    if lock is None:
        log_line(
            f"another driver holds the browser ({singlewriter.describe()}). "
            "Nothing was touched. Retry in a minute, or stop the loop with "
            "tools/cron_apply.py --stop."
        )
        return 2
    try:
        if opts.phase == "discover":
            asyncio.run(phase_discover(keywords, opts.location, reuse=opts.refilter))
        elif opts.phase == "retry":
            asyncio.run(phase_retry(opts.limit))
        else:
            asyncio.run(phase_apply(opts.limit))
    finally:
        singlewriter.release(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
