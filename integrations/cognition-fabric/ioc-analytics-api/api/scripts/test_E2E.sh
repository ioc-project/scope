#!/usr/bin/env bash

set -euo pipefail

# Run the same API E2E test list used by CI locally.
# Run this script from the repository root: ./api/scripts/test_E2E.sh

uv sync --frozen --project . --group dev

TEST_E2E_LIST="test_sessions test_agentic_protocols_metrics test_applications test_waste_estimation test_symbolic_api test_symbolic_api_mocks test_metrics_provider_persistence test_metrics_provider_persistence_mocks test_application_charts test_application_sessions test_caching test_dal test_kg test_mcev2 test_metrics"

for t in $TEST_E2E_LIST; do
  echo "Running $t"
  uv run --project . --group dev --no-sync python -m pytest --cache-clear "tests/${t}.py" -v -s --log-cli-level=DEBUG 2>&1 | tee "tests/logs/${t}.log"
done
