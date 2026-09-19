from __future__ import annotations

from pathlib import Path

from applyops.company_policy import (
    DEFAULT_REVIEW_COMPANIES,
    CompanyDecision,
    CompanyPolicy,
    CompanyPolicyStore,
    normalize_company,
)


def test_default_policy_is_auto_with_built_in_review_list():
    policy = CompanyPolicy()

    assert policy.default_policy == "auto"
    assert "Google" in policy.review_companies
    assert "OpenAI" in policy.review_companies
    assert policy.never_companies == []
    assert len(DEFAULT_REVIEW_COMPANIES) >= 10


def test_company_names_normalize_suffixes_and_aliases():
    assert normalize_company("Google, Inc.") == "Google"
    assert normalize_company("Meta Platforms, Inc.") == "Meta"
    assert normalize_company("AWS") == "Amazon"
    assert normalize_company("Amazon Web Services LLC") == "Amazon"
    assert normalize_company("Alphabet Corporation") == "Google"
    assert normalize_company("TikTok Pte. Ltd.") == "ByteDance"


def test_never_has_priority_over_review_and_unknown_is_auto():
    policy = CompanyPolicy(
        review_companies=["Google", "AWS"],
        never_companies=["Alphabet Inc."],
    )

    # Alphabet aliases to Google, so the NEVER entry wins over REVIEW.
    assert policy.decision("Google LLC") is CompanyDecision.NEVER
    assert policy.decision("Alphabet") is CompanyDecision.NEVER
    assert policy.decision("Amazon Web Services") is CompanyDecision.REVIEW
    assert policy.decision("Small Co") is CompanyDecision.AUTO


def test_policy_store_round_trips_and_deduplicates_names(tmp_path: Path):
    store = CompanyPolicyStore(tmp_path)
    saved = store.set(
        CompanyPolicy(
            default_policy="auto",
            review_companies=["Google", "google", " AWS "],
            never_companies=["Never Co", "never co"],
        )
    )

    assert saved.review_companies == ["Google", "AWS"]
    assert saved.never_companies == ["Never Co"]
    assert store.get().to_dict() == saved.to_dict()
