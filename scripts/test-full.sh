#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q -p no:cacheprovider "$@"
(cd frontend && npx vitest run && npx tsc --noEmit && npm run build)
# Build the wheel directly from the checkout.  The project intentionally ships
# the already-built frontend in the wheel; building an intermediate sdist first
# can omit that ignored build output before Hatch applies force-include.
/Users/hanyuli/.local/bin/uv build --wheel
