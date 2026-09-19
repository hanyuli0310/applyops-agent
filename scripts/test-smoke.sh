#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_blocker_mcp_flow.py::test_public_mcp_flow_reaches_a_verified_submission \
  tests/test_m3_local_ui.py::test_four_pages_end_to_end_in_a_real_browser \
  tests/test_m1_trusted_execution.py::test_unconfirmed_submission_is_never_retried_and_never_confirmed \
  "$@"
