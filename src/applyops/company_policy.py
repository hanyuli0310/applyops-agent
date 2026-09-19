"""Small, deterministic company routing policy for application delivery.

Company names are user configuration, not an AI classification problem.  The
policy therefore does only a little normalization (case, punctuation, common
legal suffixes and a short alias table) and applies one explicit precedence:
never, then review, then the default.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .concurrency import FileLock, atomic_write_json, data_lock_path, read_json

DEFAULT_REVIEW_COMPANIES = (
    "Google",
    "Meta",
    "Microsoft",
    "Amazon",
    "Apple",
    "NVIDIA",
    "OpenAI",
    "Anthropic",
    "Stripe",
    "Databricks",
    "Tesla",
    "Adobe",
    "Qualcomm",
    "ByteDance",
)

_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corporation",
    "corp",
    "company",
    "co",
    "plc",
    "pte",
    "sa",
    "ag",
}

# Values are the display/canonical names used in the built-in list.  Keys are
# passed through the same suffix stripping as user-entered company names.
_ALIASES = {
    "aws": "Amazon",
    "amazon web services": "Amazon",
    "amazon": "Amazon",
    "facebook": "Meta",
    "meta platforms": "Meta",
    "meta": "Meta",
    "alphabet": "Google",
    "google": "Google",
    "tiktok": "ByteDance",
    "bytedance": "ByteDance",
}


class CompanyDecision(str, Enum):
    AUTO = "auto"
    REVIEW = "review"
    NEVER = "never"


def _clean_name(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())
    tokens = [token for token in text.split() if token]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_company(value: str) -> str:
    """Return a stable canonical company name for policy comparisons."""
    cleaned = _clean_name(value)
    return _ALIASES.get(cleaned, cleaned)


def _dedupe_names(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        display = " ".join(str(value).strip().split())
        if not display:
            continue
        key = normalize_company(display)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(display)
    return result


@dataclass
class CompanyPolicy:
    default_policy: str = "auto"
    review_companies: list[str] = field(
        default_factory=lambda: list(DEFAULT_REVIEW_COMPANIES)
    )
    never_companies: list[str] = field(default_factory=list)
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.default_policy not in {CompanyDecision.AUTO.value}:
            self.default_policy = CompanyDecision.AUTO.value
        self.review_companies = _dedupe_names(self.review_companies)
        self.never_companies = _dedupe_names(self.never_companies)

    def to_dict(self) -> dict:
        return {
            "default_policy": self.default_policy,
            "review_companies": list(self.review_companies),
            "never_companies": list(self.never_companies),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> CompanyPolicy:
        return cls(
            default_policy=str(payload.get("default_policy", "auto")),
            review_companies=list(
                payload.get("review_companies", DEFAULT_REVIEW_COMPANIES)
            ),
            never_companies=list(payload.get("never_companies", [])),
            updated_at=str(payload.get("updated_at", "")),
        )

    def decision(self, company: str) -> CompanyDecision:
        key = normalize_company(company)
        if key in {normalize_company(item) for item in self.never_companies}:
            return CompanyDecision.NEVER
        if key in {normalize_company(item) for item in self.review_companies}:
            return CompanyDecision.REVIEW
        return CompanyDecision(self.default_policy)


class CompanyPolicyStore:
    """File-backed policy with safe defaults and a single writer lock."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "company_policy.json"
        self._lock_path = data_lock_path(self.data_dir, "company_policy")

    def get(self) -> CompanyPolicy:
        payload = read_json(self.path, default={}) or {}
        return CompanyPolicy.from_dict(payload)

    def set(self, policy: CompanyPolicy) -> CompanyPolicy:
        policy = CompanyPolicy.from_dict(policy.to_dict())
        policy.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with FileLock(self._lock_path):
            atomic_write_json(self.path, policy.to_dict())
        return policy

    def ensure_defaults(self) -> CompanyPolicy:
        if self.path.exists():
            return self.get()
        return self.set(CompanyPolicy())
