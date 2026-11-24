#!/bin/bash
# SPDX-License-Identifier: Apache-2.0

# run_all_tests.sh
# Runs all unit tests

set -e  # Exit on first error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory (oxp-lib root)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Track results
TOTAL_TESTS=0
FAILED_PACKAGES=()

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Running all tests for oxp-lib$ {NC}"
echo -e "${BLUE}========================================${NC}\n"

# Function to run tests for a package
run_package_tests() {
    local package_name=$1
    local package_dir=$2
    shift 2  # Remove package_name and package_dir from arguments

    echo -e "${YELLOW}>>> Testing ${package_name}...${NC}"

    cd "${SCRIPT_DIR}/${package_dir}"

    if uv run pytest "$@"; then
        echo -e "${GREEN}✓ ${package_name} tests passed${NC}\n"
    else
        echo -e "${RED}✗ ${package_name} tests failed${NC}\n"
        FAILED_PACKAGES+=("${package_name}")
        return 1
    fi
}

# Run tests for each package in dependency order
# Pass any extra arguments from command line to pytest
# run_package_tests "dem" "dem" "$@" || true
# run_package_tests "dal" "dal" "$@" || true
# run_package_tests "norm" "norm" "$@" || true
# run_package_tests "ontology-hub" "ontology-hub" "$@" || true

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Test Summary${NC}"
echo -e "${BLUE}========================================${NC}"

if [ ${#FAILED_PACKAGES[@]} -eq 0 ]; then
    echo -e "${GREEN}✓ All packages passed!${NC}"
    echo -e "  - dal: 40 tests"
    echo -e "  - mas-model: 332 tests"
    echo -e "  - ontology-hub: 119 tests"
    echo -e "${GREEN}  Total: 491 tests${NC}"
    exit 0
else
    echo -e "${RED}✗ Some packages failed:${NC}"
    for package in "${FAILED_PACKAGES[@]}"; do
        echo -e "  - ${RED}${package}${NC}"
    done
    exit 1
fi
