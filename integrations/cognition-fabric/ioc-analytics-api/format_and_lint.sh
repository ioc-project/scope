#!/bin/bash
# SPDX-License-Identifier: Apache-2.0

# format_and_lint.sh
# Formats and lints all code

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
FAILED_PACKAGES=()

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Formatting and linting oxp-lib${NC}"
echo -e "${BLUE}========================================${NC}\n"

# Function to run format and lint for a package
format_and_lint_package() {
    local package_name=$1
    local package_dir=$2

    echo -e "${YELLOW}>>> Processing ${package_name}...${NC}"

    cd "${SCRIPT_DIR}/${package_dir}"

    # Run ruff format
    echo -e "  Running ruff format..."
    if ! uv run ruff format .; then
        echo -e "${RED}✗ ${package_name} formatting failed${NC}\n"
        FAILED_PACKAGES+=("${package_name}")
        return 1
    fi

    # Run ruff check with fix
    echo -e "  Running ruff check --fix..."
    if ! uv run ruff check --fix --unsafe-fixes .; then
        echo -e "${RED}✗ ${package_name} linting failed${NC}\n"
        FAILED_PACKAGES+=("${package_name}")
        return 1
    fi

    # Run pre-commit if .pre-commit-config.yaml exists
    if [ -f ".pre-commit-config.yaml" ]; then
        echo -e "  Running pre-commit..."
        if ! uv run pre-commit run --all-files; then
            echo -e "${RED}✗ ${package_name} pre-commit failed${NC}\n"
            FAILED_PACKAGES+=("${package_name}")
            return 1
        fi
    else
        echo -e "  Skipping pre-commit (no .pre-commit-config.yaml)"
    fi

    echo -e "${GREEN}✓ ${package_name} formatted and linted${NC}\n"
    return 0
}

# Process each package in order
format_and_lint_package "dem" "dem" || true
format_and_lint_package "dal" "dal" || true
format_and_lint_package "norm" "norm" || true
format_and_lint_package "ontology-hub" "ontology-hub" || true

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Format & Lint Summary${NC}"
echo -e "${BLUE}========================================${NC}"

if [ ${#FAILED_PACKAGES[@]} -eq 0 ]; then
    echo -e "${GREEN}✓ All packages formatted and linted successfully!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some packages failed:${NC}"
    for package in "${FAILED_PACKAGES[@]}"; do
        echo -e "  - ${RED}${package}${NC}"
    done
    exit 1
fi
