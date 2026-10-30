#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Semantic analysis workers in one process: `--workers` (or `SEMANTIC_ANALYSIS_WORKERS`) picks some of them."""

import sys

from worker_base.runner import Member, main as run_group

GROUP = "semantic-analysis"

MEMBERS = (
    Member("grouping", "GROUPING", "grouping_worker.cli:main"),
    Member("hierarchical-grouping", "HIERARCHICAL_GROUPING", "hierarchical_grouping_worker.cli:main"),
    Member("analysis", "ANALYSIS", "analysis_worker.cli:main", isolated=True),
    Member("intelligence", "INTELLIGENCE", "intelligence_worker.cli:main"),
)


def main() -> None:
    sys.exit(run_group(GROUP, MEMBERS))


if __name__ == "__main__":
    main()
