#!/usr/bin/env bash
# Run the sandbox test suite INSIDE the production image, as container-root on
# Linux. That is the only environment where the per-session uid layer, the
# seccomp network block, and the Linux-only rlimits are all live; on a macOS
# dev box those tests skip. Docker's default seccomp profile also denies
# `unshare -rn`, mirroring Railway.
#
#   sandbox/scripts/test-in-image.sh            # from the repo root
#   sandbox/scripts/test-in-image.sh -k uid     # extra args go to pytest
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${SANDBOX_TEST_IMAGE:-hypebase-sandbox:test}"

docker build -t "${image}" "${repo_root}/sandbox/"
docker run --rm \
  -v "${repo_root}/sandbox/tests:/app/tests:ro" \
  "${image}" \
  sh -c 'pip install -q "pytest>=8" "pytest-asyncio>=0.24" "httpx>=0.27" \
    && python -m pytest -q -p no:cacheprovider tests "$@"' -- "$@"
