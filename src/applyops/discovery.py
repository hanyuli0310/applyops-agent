"""Finding postings, rather than being handed one.

M1 assumed a URL given by a human. This is the other half: turn a search intent
into a list of postings. Two deliberate constraints.

**Easy Apply only by default.** The tool layer can drive the LinkedIn modal and
knows when it has finished. "Apply on company site" hands off to an ATS we have
never seen, so a search that returns those produces jobs the loop will stall
on. Better to exclude them at the query than to discover it mid-form.

**No scoring here.** The result is what LinkedIn returned, in LinkedIn's order.
Ranking is a judgement call about someone's career; keeping it out of this
module means the caller can see the raw list and disagree with it.

Two properties of LinkedIn's results list shape this module, both learned
against the live page rather than assumed:

* it is **virtualised** -- an ``<li data-occludable-job-id>`` exists for every
  result, but only those near the viewport have content. Reading the DOM once
  yields a handful of real cards and dozens of empty shells. Worse, the list is
  its own scroll container, so scrolling the window changes nothing;
* the title is **rendered twice** inside its link (a visually hidden copy for
  screen readers beside the visible one), so raw extraction returns
  ``"Data EngineerData Engineer"``.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from .browser import BrowserController

SEARCH_ENDPOINT = "https://www.linkedin.com/jobs/search/"
JOB_VIEW_TEMPLATE = "https://www.linkedin.com/jobs/view/{job_id}/"

# `f_AL` is LinkedIn's "Easy Apply" facet. `sortBy=DD` is newest-first, which
# matters because a stale posting is often already filled.
FACET_EASY_APPLY = "f_AL"
#: LinkedIn's experience-level facet. Asking the site to filter is more honest
#: than guessing from a title: `f_E=2` is "Entry level", and the applicant this
#: installation serves is a recent graduate (AGENTS.md §12).
FACET_EXPERIENCE_LEVEL = "f_E"
FACET_ENTRY_LEVEL = "2"
SORT_NEWEST = "DD"

# Scroll-and-re-read passes. The list unloads cards that leave the viewport, so
# this accumulates across passes rather than doing one read.
DEFAULT_PASSES = 12

_VERIFICATION = re.compile(r"\s*\bwith\s+verification\b\s*$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


class JobCard(BaseModel):
    """One posting as it appeared in the results list."""

    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    listed: str = ""
    easy_apply: bool = False
    url: str = ""

    @property
    def label(self) -> str:
        bits = [self.title or "(untitled)"]
        if self.company:
            bits.append(f"@ {self.company}")
        if self.location:
            bits.append(f"-- {self.location}")
        return " ".join(bits)


class SearchResult(BaseModel):
    jobs: list[JobCard] = Field(default_factory=list)
    search_url: str = ""
    rendered_cards: int = 0
    unrendered: int = 0
    notes: list[str] = Field(default_factory=list)
    error: str = ""


def clean_title(text: str) -> str:
    """Undo the two artefacts of LinkedIn's card markup.

    The visible title sits beside a visually hidden duplicate, so the extracted
    text comes out doubled -- sometimes glued together, sometimes spaced. An
    exact doubling is always an artefact: no real posting is titled "X X".
    """
    title = _WHITESPACE.sub(" ", text or "").strip()
    title = _VERIFICATION.sub("", title).strip()
    if not title:
        return ""

    compact = title.replace(" ", "")
    if len(compact) < 2 or len(compact) % 2:
        return title
    if compact[: len(compact) // 2] != compact[len(compact) // 2:]:
        return title

    # It is a doubling. Prefer halving on a word boundary, which keeps the
    # words themselves intact.
    words = title.split(" ")
    n = len(words)
    if n % 2 == 0 and words[: n // 2] == words[n // 2:]:
        return " ".join(words[: n // 2]).strip()
    if len(title) % 2 == 0:
        half = title[: len(title) // 2].strip()
        if half and half == title[len(title) // 2:].strip():
            return half
    return title


# Asking LinkedIn for entry level is the default, not an option: who is applying
# is a fact (AGENTS.md §12 -- a new grad), so the search should be built for him
# rather than for whoever calls it. Left as a switch because the facet has to be
# droppable, but a default of False meant every search came back full of senior
# postings that then had to be filtered and skipped by hand.
ENTRY_LEVEL_BY_DEFAULT = True


def build_search_url(
    keywords: str,
    location: str = "",
    easy_apply_only: bool = True,
    recent_days: int = 0,
    start: int = 0,
    entry_level_only: bool = ENTRY_LEVEL_BY_DEFAULT,
) -> str:
    """Compose a jobs search URL.

    Kept as a pure function so the query is inspectable and reproducible --
    when a search returns nothing, the first thing worth knowing is exactly
    what was asked for.
    """
    params: dict[str, str] = {"keywords": keywords, "sortBy": SORT_NEWEST}
    if location:
        params["location"] = location
    if easy_apply_only:
        params[FACET_EASY_APPLY] = "true"
    if recent_days:
        # `f_TPR` is a relative time window in seconds: r604800 = past 7 days.
        params["f_TPR"] = f"r{int(recent_days) * 86400}"
    if entry_level_only:
        # `f_E` is LinkedIn's own experience-level facet (1 Internship,
        # 2 Entry level, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive).
        # Asking the site to filter beats guessing from the title: the title
        # pattern in `target_titles.is_entry_level` stays as a second net for
        # postings the facet lets through and for everything not searched here.
        params[FACET_EXPERIENCE_LEVEL] = FACET_ENTRY_LEVEL
    if start:
        params["start"] = str(start)
    return f"{SEARCH_ENDPOINT}?{urlencode(params)}"


# Extraction runs in the page, so it must be defensive: LinkedIn's markup
# changes and several layouts are live at once. Every field is optional and
# every selector list is a fallback chain. An empty root is counted as a
# placeholder rather than being mistaken for a posting.
SEARCH_CARDS_JS = r"""
() => {
  const textOf = (root, selectors) => {
    for (const sel of selectors) {
      const node = root.querySelector(sel);
      if (node && node.textContent && node.textContent.trim()) {
        return node.textContent.trim().replace(/\s+/g, ' ');
      }
    }
    return '';
  };

  const roots = new Set();
  for (const sel of [
    'li[data-occludable-job-id]',
    'div.job-card-container[data-job-id]',
    'div[data-job-id]',
    'li.scaffold-layout__list-item',
  ]) {
    document.querySelectorAll(sel).forEach((el) => roots.add(el));
  }

  const cards = [];
  const seen = new Set();
  let placeholders = 0;

  for (const el of roots) {
    let id = el.getAttribute('data-occludable-job-id')
          || el.getAttribute('data-job-id')
          || '';
    const anchor = el.querySelector('a[href*="/jobs/view/"]');
    const href = anchor ? anchor.getAttribute('href') : '';
    if (!id && href) {
      const m = href.match(/\/jobs\/view\/(\d+)/);
      if (m) id = m[1];
    }
    if (!id || seen.has(id)) continue;

    // The page reserves a slot for every result but only fills the ones near
    // the viewport. An empty slot is not a posting.
    const body = (el.innerText || '').trim();
    if (!anchor || !body) {
      seen.add(id);
      placeholders += 1;
      continue;
    }
    seen.add(id);

    // The anchor wraps only the title (and carries a hidden duplicate of it),
    // so take its text and let the caller de-duplicate it.
    const titleNode = el.querySelector('[class*="job-card-list__title"]');
    const title = titleNode
      ? (titleNode.textContent || '')
      : (anchor.textContent || anchor.getAttribute('aria-label') || '');

    const metaItems = Array.from(
      el.querySelectorAll('[class*="job-card-container__metadata-item"]')
    ).map((n) => (n.textContent || '').trim()).filter(Boolean);

    const company = textOf(el, [
      '.artdeco-entity-lockup__subtitle',
      '[class*="job-card-container__primary-description"]',
      '[class*="job-card-container__company-name"]',
    ]);
    let location = textOf(el, [
      '.artdeco-entity-lockup__caption',
      '[class*="job-card-container__metadata-item"]',
    ]);
    let salary = textOf(el, [
      '[class*="job-card-container__salary-info"]',
      '[class*="salary-info"]',
    ]);
    if (metaItems.length >= 2) {
      if (!location) location = metaItems[0];
      if (!salary) salary = metaItems[1];
    }

    const timeNode = el.querySelector('time');
    const listed = timeNode
      ? (timeNode.textContent || timeNode.getAttribute('datetime') || '').trim()
      : '';

    // The footer states the apply method in words, which is more stable than
    // the icon class it sits beside.
    const easyApply = /easy\s*apply/i.test(body);

    cards.push({
      job_id: id,
      title: title,
      company: company,
      location: location,
      salary: salary,
      listed: listed,
      easy_apply: easyApply,
      url: 'https://www.linkedin.com/jobs/view/' + id + '/',
    });
  }
  return { cards: cards, placeholders: placeholders, roots: roots.size };
}
"""

# The results pane is its own scroll container, and scrolling the window or the
# named layout wrappers does nothing: `div.jobs-search-results-list` does not
# exist on the current page, and `.scaffold-layout__list` has scrollHeight equal
# to clientHeight. The element that really scrolls carries a generated class
# (`ozWJyFzBCBEQQTqlBayfLKNAxwlNVdrXZw`), which is exactly the sort of name that
# changes without warning.
#
# So walk *up* from the one thing that is stable -- the result items themselves
# -- and take the first ancestor that can actually scroll. Measured against the
# live page, this is the difference between 7 cards and the full list.
SCROLL_LIST_JS = r"""
() => {
  const li = document.querySelector('li[data-occludable-job-id]');
  if (!li) return { moved: false, where: 'no-results' };

  let el = li.parentElement;
  while (el && el.scrollHeight <= el.clientHeight + 40) {
    el = el.parentElement;
  }

  if (!el) {
    const before = window.scrollY;
    window.scrollBy(0, 800);
    return { moved: window.scrollY !== before, where: 'window' };
  }

  const before = el.scrollTop;
  el.scrollTop = before + Math.max(600, el.clientHeight * 0.85);
  return { moved: el.scrollTop !== before, where: 'ancestor', top: el.scrollTop };
}
"""


def parse_cards(raw: list[dict]) -> list[JobCard]:
    """Normalise raw page output into cards, dropping anything unusable."""
    jobs: list[JobCard] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id") or "").strip()
        if not job_id or job_id in seen:
            continue
        title = clean_title(str(item.get("title") or ""))
        if not title:
            # Nothing identifiable -- a skeleton, not a posting.
            continue
        seen.add(job_id)
        jobs.append(
            JobCard(
                job_id=job_id,
                title=title,
                company=str(item.get("company") or "").strip(),
                location=str(item.get("location") or "").strip(),
                salary=str(item.get("salary") or "").strip(),
                listed=str(item.get("listed") or "").strip(),
                easy_apply=bool(item.get("easy_apply")),
                url=str(item.get("url") or JOB_VIEW_TEMPLATE.format(job_id=job_id)),
            )
        )
    return jobs


async def _dismiss_overlays(page) -> None:
    """Close the sign-in wall and cookie banner if either is showing.

    Both cover the list on a first visit and make the page look empty. Failing
    to dismiss them is the difference between "no jobs matched" and "the results
    were behind a dialog".
    """
    for label in ("Dismiss", "Close", "Not now", "Got it"):
        try:
            button = page.get_by_role("button", name=label, exact=False).first
            if await button.is_visible(timeout=600):
                await button.click(timeout=1500)
                await asyncio.sleep(0.4)
        except Exception:
            continue


async def search(
    controller: "BrowserController",
    keywords: str,
    location: str = "",
    limit: int = 25,
    easy_apply_only: bool = True,
    recent_days: int = 0,
    passes: int = DEFAULT_PASSES,
    entry_level_only: bool = ENTRY_LEVEL_BY_DEFAULT,
) -> SearchResult:
    """Run a search and return the postings found.

    Accumulates across scroll passes, because the list unmounts cards that
    leave the viewport -- a single read would silently return only whatever
    happened to be on screen.
    """
    url = build_search_url(
        keywords, location, easy_apply_only, recent_days, entry_level_only=entry_level_only
    )
    result = SearchResult(search_url=url)

    try:
        await controller.goto(url, settle=2.5)
    except Exception as exc:  # navigational failure is reportable, not fatal
        result.error = f"navigation failed: {type(exc).__name__}: {exc}"
        return result

    await _dismiss_overlays(controller.page)

    found: dict[str, JobCard] = {}
    placeholders = 0
    stall = 0

    for _ in range(max(1, passes)):
        try:
            payload = await controller.page.evaluate(SEARCH_CARDS_JS)
        except Exception as exc:
            result.error = f"extraction failed: {type(exc).__name__}: {exc}"
            break

        if isinstance(payload, dict):
            raw = payload.get("cards") or []
            placeholders = max(placeholders, int(payload.get("placeholders") or 0))
        else:  # tolerate an older page shape
            raw = payload or []

        for card in parse_cards(raw):
            found.setdefault(card.job_id, card)

        if len(found) >= limit:
            break

        try:
            moved = await controller.page.evaluate(SCROLL_LIST_JS)
        except Exception:
            break
        await asyncio.sleep(1.2)

        if not (moved or {}).get("moved"):
            stall += 1
            if stall >= 2:
                break

    jobs = list(found.values())
    result.rendered_cards = len(jobs)
    result.unrendered = placeholders

    if easy_apply_only:
        before = len(jobs)
        jobs = [j for j in jobs if j.easy_apply]
        dropped = before - len(jobs)
        if dropped:
            result.notes.append(
                f"{dropped} posting(s) dropped: the Easy Apply facet is a "
                f"request, not a guarantee, and the list is tagged in-page."
            )

    result.jobs = jobs[:limit] if limit else jobs

    if not result.jobs:
        if result.rendered_cards == 0:
            result.notes.append(
                "No job cards rendered. Either the session is logged out, the "
                "results are behind a dialog, or the markup changed."
            )
        else:
            result.notes.append(
                f"{result.rendered_cards} card(s) parsed but none survived "
                f"filtering."
            )
    if result.unrendered:
        result.notes.append(
            f"{result.unrendered} result slot(s) were never rendered "
            f"(virtualised list); scroll further to reach them."
        )
    return result
