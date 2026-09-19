"""Platform naming for the flywheel.

One source of truth for "which bucket does this URL's knowledge live in". A
route is keyed `<platform>/<route>`, so naming the same site "Amazon" in one
layer and "Unknown" in another would file the same lesson under two keys and
make both look unlearned -- which is why this map exists separately from the
richer platform *detector*: detection answers "how do I drive this site",
naming answers "where do I file what I learned".
"""

from __future__ import annotations

_PLATFORM_BY_DOMAIN = (
    ("linkedin.com", "LinkedIn"),
    ("indeed.com", "Indeed"),
    ("amazon.jobs", "Amazon"),
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("myworkdayjobs.com", "Workday"),
    ("workday.com", "Workday"),
    ("ashbyhq.com", "Ashby"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("icims.com", "iCIMS"),
    ("workable.com", "Workable"),
    ("bamboohr.com", "BambooHR"),
)


def platform_for_url(url: str) -> str:
    """Best-effort platform label for a URL, used to bucket flywheel knowledge."""
    lowered = (url or "").lower()
    for domain, label in _PLATFORM_BY_DOMAIN:
        if domain in lowered:
            return label
    return "Unknown"
