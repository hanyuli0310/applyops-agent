"""The target-title pool, wired in.

`AGENTS.md` §12 defines, in writing, which job titles this project is for. Until
now nothing read it: the unattended runners searched six hard-coded LinkedIn
query strings in `tools/auto_apply.py`, and the console's filter knew only what
the user had typed into their preferences. The document was intent; the system
was something else.

These tests hold the two together:

1. the shipped pool **is** what the document says (drift fails here, not in
   production);
2. every search keyword the runners use is backed by a title in the pool, so a
   query can never outlive the intent it came from;
3. adopting the pool actually changes what the filter keeps, and says why;
4. adopting it twice does not duplicate anything.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from applyops.api.app import create_app
from applyops.preferences import PreferenceStore, evaluate
from applyops.target_titles import (
    SEARCH_KEYWORDS,
    TARGET_TITLE_GROUPS,
    all_titles,
    from_agents_md,
    search_keywords,
)

REPO_ROOT = Path(__file__).parent.parent


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="applyops-titles-"))


# ── the document and the code cannot drift ──────────────────────────


def test_the_shipped_pool_is_what_the_document_says():
    """AGENTS.md is the written definition; this is the same list, checked."""
    document = from_agents_md(REPO_ROOT / "AGENTS.md")
    assert document, "AGENTS.md should define the target titles"
    assert document == TARGET_TITLE_GROUPS, (
        "the shipped pool and AGENTS.md §12 have drifted apart: "
        f"only in the document: {set(document) ^ set(TARGET_TITLE_GROUPS)}"
    )
    assert len(all_titles()) == 32


def test_the_pool_covers_the_scope_the_document_draws():
    """A few landmarks, so a rewrite cannot quietly widen or narrow the intent."""
    titles = {t.casefold() for t in all_titles()}
    for expected in (
        "software engineer",
        "new grad software engineer",
        "backend engineer",
        "machine learning engineer",
        "llm engineer",
        "forward deployed engineer",
        "data scientist",
    ):
        assert expected in titles, expected

    # The document draws the line at engineering and applied research: these are
    # explicitly out of scope, and the pool must not smuggle them in.
    for excluded in ("financial analyst", "accountant", "product manager", "qa engineer"):
        assert excluded not in titles, excluded


def test_every_search_keyword_is_backed_by_a_title_in_the_pool():
    """The runners search short query strings; the pool is the intent behind them.

    This is the check that keeps `KEYWORDS` honest: a keyword with no title
    behind it is a search for something the project does not want.
    """
    titles = " | ".join(all_titles()).casefold()
    for keyword in search_keywords():
        tokens = [t for t in keyword.casefold().split() if t not in {"and", "of"}]
        covered = any(
            all(token in title.casefold() for token in tokens) for title in all_titles()
        )
        assert covered, f"search keyword {keyword!r} matches no title in the pool: {titles[:0]}"


def test_the_runners_search_what_the_pool_defines():
    """`tools/auto_apply.py` used to carry its own hard-coded list."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "auto_apply_probe", REPO_ROOT / "tools" / "auto_apply.py"
    )
    assert spec is not None
    source = (REPO_ROOT / "tools" / "auto_apply.py").read_text(encoding="utf-8")
    # The list is derived, not re-typed: a literal list would be a second source
    # of truth for the same decision.
    assert "search_keywords()" in source, "auto_apply should derive KEYWORDS from the pool"
    assert SEARCH_KEYWORDS, "the pool must offer search keywords"


# ── adopting the pool changes what the filter does ──────────────────


def test_seeding_the_preferences_makes_the_pool_filter_for_real():
    root = _tmp()
    store = PreferenceStore(root)
    assert store.get().target_titles == []

    seeded = store.seed_target_titles()
    assert seeded.target_titles == all_titles()

    # And now the rules actually decide: a target title is kept, with a reason.
    kept = evaluate(
        title="Senior Machine Learning Engineer", location="Remote, US", prefs=seeded
    )
    assert kept["keep"] is True, kept
    assert kept["reasons"], kept

    filtered = evaluate(title="Financial Analyst", location="Remote, US", prefs=seeded)
    assert filtered["keep"] is False
    assert "target title" in filtered["reasons"][0], filtered


def test_seeding_twice_does_not_duplicate_and_keeps_the_users_own_titles():
    root = _tmp()
    store = PreferenceStore(root)
    store.set(store.get().__class__(target_titles=["Staff Engineer"], locations=["Austin"]))

    first = store.seed_target_titles()
    second = store.seed_target_titles()

    assert first.target_titles == second.target_titles
    assert len(second.target_titles) == len(set(second.target_titles))
    # The user's own entry survives, and their other settings are untouched.
    assert "Staff Engineer" in second.target_titles
    assert second.locations == ["Austin"]


def test_the_console_can_adopt_the_pool_with_one_call():
    root = _tmp()
    app = create_app(root, frontend_dist=None, headless=True)
    token = app.state.applyops.session_token

    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Writes need the page's session token, like every other state change.
        assert client.post("/api/preferences/target-titles/seed").status_code == 403

        # From here on, act as the served page does.
        client.headers.update({"X-ApplyOps-Token": token})

        response = client.post("/api/preferences/target-titles/seed")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["target_titles"] == all_titles()
        assert body["seeded"] == len(all_titles())

        # It is the stored preference now, so the filter and the preview use it.
        assert client.get("/api/preferences").json()["target_titles"] == all_titles()
        preview = client.post(
            "/api/preferences/preview",
            json={"title": "Software Engineer", "location": "Remote"},
        ).json()
        assert preview["keep"] is True, preview
        assert "Software Engineer" in json.dumps(preview)
