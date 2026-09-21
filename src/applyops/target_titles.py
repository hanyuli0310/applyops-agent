"""The job titles this project is for -- read from the document, kept in code.

`AGENTS.md` §12 is the written definition of intent: which postings are the kind
of job we are looking for, and where the line is drawn (engineering and applied
research in software, AI and ML; not analyst, finance, accounting, product, QA,
IT support or sales). This module is that definition in a form the code can use,
so the runners and the console stop disagreeing with it.

Two things are deliberate here:

- **`from_agents_md` exists only to be compared.** It parses the document so a
  test can assert that the shipped pool still says what the document says: drift
  becomes a failing test rather than a search that quietly hunts the wrong jobs.
  Nothing at runtime reads the markdown -- a policy that depends on a prose file
  being present next to the installed package would break the moment the package
  is installed on its own.

- **Search keywords are a declared subset, not a generated list.** The document's
  pool is *semantic* ("a posting matches when its title means one of these jobs"),
  while a LinkedIn query is a blunt string. Turning 32 titles into 32 queries
  would be inventing policy. Instead the queries are listed explicitly and a test
  requires each one to be backed by a title in the pool, so a query cannot
  outlive the intent it came from.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Exactly the groups and titles of `AGENTS.md` §12, in the document's order.
TARGET_TITLE_GROUPS: dict[str, tuple[str, ...]] = {
    "Software engineering — general and entry level": (
        "Software Engineer",
        "Software Development Engineer",
        "Software Engineer I",
        "Associate Software Engineer",
        "Entry Level Software Engineer",
        "Early Career Software Engineer",
        "New Grad Software Engineer",
        "University Graduate Software Engineer",
    ),
    "Backend and full stack": (
        "Backend Software Engineer",
        "Backend Engineer",
        "Full Stack Software Engineer",
        "Full Stack Engineer",
    ),
    "Product and platform": (
        "Product Engineer",
        "Platform Engineer",
    ),
    "AI and machine learning": (
        "AI Engineer",
        "AI Software Engineer",
        "Applied AI Engineer",
        "AI Application Engineer",
        "AI/ML Engineer",
        "Machine Learning Engineer",
        "ML Engineer",
        "Machine Learning Software Engineer",
        "Applied Machine Learning Engineer",
        "Generative AI Engineer",
        "LLM Engineer",
        "AI Agent Engineer",
        "AI Product Engineer",
    ),
    "Applied research and data": (
        "Forward Deployed Engineer",
        "Research Engineer",
        "AI Research Engineer",
        "Computer Vision Engineer",
        "Data Scientist",
    ),
}

#: The query strings the unattended runners search with. Every one of them has to
#: be backed by a title in the pool above (asserted in the tests): a keyword with
#: no title behind it is a search for something the project does not want.
SEARCH_KEYWORDS: tuple[str, ...] = (
    "software engineer",
    "backend engineer",
    "machine learning engineer",
    "applied AI engineer",
    "LLM engineer",
    "AI agent engineer",
)

_HEADING = re.compile(r"^##\s+\d+\.\s*(?P<title>.+?)\s*$")
_SECTION_TITLE = "target job titles"


def all_titles() -> list[str]:
    """Every target title, in document order, without duplicates."""
    seen: dict[str, None] = {}
    for titles in TARGET_TITLE_GROUPS.values():
        for title in titles:
            seen.setdefault(title, None)
    return list(seen)


def search_keywords() -> list[str]:
    """The query strings the runners should search with."""
    return list(SEARCH_KEYWORDS)


def from_agents_md(path: str | Path) -> dict[str, tuple[str, ...]]:
    """Parse the title pool out of the document, for comparison in tests.

    Returns an empty mapping when the file is absent or the section is missing,
    because "there is no document here" is a legitimate state for an installed
    package -- and a caller that wants to compare has to decide what that means,
    rather than getting an exception from a docstring helper.
    """
    file = Path(path)
    if not file.exists():
        return {}
    text = file.read_text(encoding="utf-8")

    start = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group("title").casefold() == _SECTION_TITLE:
            start = index + 1
            break
    if start is None:
        return {}

    groups: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start:]:
        if _HEADING.match(line):
            break
        if line.startswith("### "):
            current = line[4:].strip()
            groups[current] = []
        elif current and line.startswith("- "):
            groups[current].append(line[2:].strip())
    return {name: tuple(titles) for name, titles in groups.items() if titles}


#: Titles that read as a more senior job than this installation is for.
#: `AGENTS.md` §12 records who the applicant is — a recent graduate starting a
#: first engineering job — so a posting that says senior, staff, principal, lead
#: or manager is out of scope **even when the work matches the pool below**.
#: Seniority is a separate question from "what kind of work", which is why it is
#: a separate pattern rather than more entries in the pool.
SENIORITY_PATTERN = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|manager|head\s+of|director|"
    r"architect|distinguished|fellow|vp|vice\s+president)\b",
    re.IGNORECASE,
)

#: Titles that say, in words, that the posting is for someone starting out.
ENTRY_LEVEL_PATTERN = re.compile(
    r"\b(new\s+grad(uate)?|entry[\s-]?level|early\s+career|associate|"
    r"junior|jr\.?|graduate|intern|apprentice|i{1,2}\b)\b",
    re.IGNORECASE,
)


def is_entry_level(title: str) -> bool:
    """Whether a posting's title fits the applicant described in §12.

    A title that says "senior" or "staff" is out of scope for a new grad even
    when the work would otherwise match; a title that says "new grad" or "entry
    level" is in scope even when it also says something else. Anything the
    pattern cannot classify is *kept* -- the cost of reading one extra posting is
    a minute, and silently dropping a job someone could have applied for is not a
    cost this function is allowed to impose.
    """
    text = title or ""
    if ENTRY_LEVEL_PATTERN.search(text):
        return True
    return not SENIORITY_PATTERN.search(text)
