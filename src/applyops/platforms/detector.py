"""Platform detection from URL."""

from enum import Enum
from urllib.parse import urlparse


class Platform(Enum):
    """Supported job platforms / ATS systems."""

    LINKEDIN = "LinkedIn"
    INDEED = "Indeed"
    # Amazon runs its own system rather than a bought-in ATS: postings live on
    # amazon.jobs and the application is gated behind passport.amazon.jobs.
    # Matching the `amazon.jobs` host covers the passport subdomain too, and
    # deliberately does *not* match `amazon.com` -- that is the storefront.
    AMAZON = "Amazon"
    WORKDAY = "Workday"
    GREENHOUSE = "Greenhouse"
    LEVER = "Lever"
    ICIMS = "iCIMS"
    TALEO = "Taleo"
    SMARTRECRUITERS = "SmartRecruiters"
    UNKNOWN = "Unknown"


# URL patterns → Platform
_PATTERNS: list[tuple[list[str], Platform]] = [
    (["linkedin.com"], Platform.LINKEDIN),
    (["indeed.com"], Platform.INDEED),
    (["amazon.jobs"], Platform.AMAZON),
    (["myworkdayjobs.com", "wd1.myworkdaysite.com", "wd3.myworkdaysite.com", "wd5.myworkdaysite.com", "workday.com"], Platform.WORKDAY),
    (["greenhouse.io", "boards.greenhouse.io"], Platform.GREENHOUSE),
    (["lever.co", "jobs.lever.co"], Platform.LEVER),
    (["icims.com"], Platform.ICIMS),
    (["taleo.net"], Platform.TALEO),
    (["smartrecruiters.com"], Platform.SMARTRECRUITERS),
]


def detect_platform(url: str) -> Platform:
    """Detect which platform/ATS a job URL belongs to.

    Args:
        url: The job posting URL.

    Returns:
        The detected Platform enum value.
    """
    try:
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower()
    except Exception:
        return Platform.UNKNOWN

    for patterns, platform in _PATTERNS:
        for pattern in patterns:
            if hostname == pattern or hostname.endswith(f".{pattern}"):
                return platform

    return Platform.UNKNOWN


def is_supported(url: str) -> bool:
    """Check if a URL is from a supported platform."""
    return detect_platform(url) != Platform.UNKNOWN


def get_platform_name(url: str) -> str:
    """Get the human-readable platform name for a URL."""
    return detect_platform(url).value
