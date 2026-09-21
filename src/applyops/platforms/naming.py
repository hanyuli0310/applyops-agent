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


#: Routes this project has a verified submission path for. Anything else is
#: `external`, which means "read and prepare by hand" -- never silently driven as
#: something it is not.
DEMO_ROUTE = "demo"
EASY_APPLY_ROUTE = "easy_apply"
EXTERNAL_ROUTE = "external"
#: The employer's own system, reached by following an apply control. Set on an
#: application once the hop has been walked (see `prepare._walk_the_hop`).
EXTERNAL_ATS_ROUTE = "external_ats"

DEMO_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def resolve_route(job_url: str, platform: str = "") -> str:
    """The single route vocabulary, resolved in one place.

    Web, MCP and the runner all call this. Before it existed each caller had its
    own default -- the console guessed `demo` for anything on localhost, the MCP
    tools defaulted to `easy_apply`, the runner took whatever the row held -- so
    the same posting could be filed under different routes depending on who
    looked at it, and an unknown ATS could end up labelled as the local demo.

    The order matters: the demo ATS is identified by *host*, because it is the
    only thing we serve ourselves; LinkedIn is a platform fact; everything else
    is external and stays that way.
    """
    lowered = (job_url or "").strip().lower()
    host_part = lowered.split("//", 1)[-1].split("/", 1)[0]
    if any(host_part.startswith(host) or f"@{host}" in host_part for host in DEMO_HOSTS):
        return DEMO_ROUTE
    if platform_for_url(job_url) == "LinkedIn" or "linkedin.com" in lowered:
        return EASY_APPLY_ROUTE
    return EXTERNAL_ROUTE


def is_drivable_route(route: str) -> bool:
    """Whether this project has a verified submission path for the route.

    `external` is a real answer, not a failure: the posting can be opened, read
    and prepared, and the final action belongs to a person.

    `external_ats` is the employer's own system reached by following the apply
    control -- a route we have already walked once, which is why it is drivable
    while a plain `external` posting is not. Leaving it out made the second
    attempt on the same application refuse with "route cannot be driven": the
    first walk recorded the route, and the recorded route disqualified itself.
    """
    return route in {DEMO_ROUTE, EASY_APPLY_ROUTE, EXTERNAL_ATS_ROUTE}
