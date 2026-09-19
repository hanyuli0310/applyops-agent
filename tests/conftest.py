"""Lightweight collection markers used by the fast/smoke/full test commands."""

from __future__ import annotations

from pathlib import Path

BROWSER_FILES = {
    "test_acceptance_fixes.py",
    "test_blocker_choice_groups.py",
    "test_blocker_click_phases.py",
    "test_blocker_concurrency.py",
    "test_blocker_mcp_answers.py",
    "test_blocker_mcp_flow.py",
    "test_blocker_reconcile_binding.py",
    "test_m1_trusted_execution.py",
    "test_m2_unified_core.py",
    "test_m3_local_ui.py",
    "test_m4_supervised_automation.py",
    "test_m5_productization.py",
    "test_pass_budget.py",
}

CONCURRENCY_FILES = {"test_blocker_concurrency.py", "test_concurrency.py"}
INTEGRATION_FILES = BROWSER_FILES | {"test_company_policy_api.py"}
SLOW_FILES = BROWSER_FILES | CONCURRENCY_FILES


def pytest_collection_modifyitems(config, items):
    for item in items:
        filename = Path(str(item.fspath)).name
        if filename in BROWSER_FILES:
            item.add_marker("browser")
        if filename in SLOW_FILES:
            item.add_marker("slow")
        if filename in INTEGRATION_FILES:
            item.add_marker("integration")
        if filename in CONCURRENCY_FILES:
            item.add_marker("concurrency")
