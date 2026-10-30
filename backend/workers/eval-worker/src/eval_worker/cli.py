#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Evaluation workers in one process: `--workers` (or `EVAL_WORKERS`) picks some of them."""

import sys

from worker_base.runner import Member, main as run_group

GROUP = "eval"

MEMBERS = (
    Member("mce", "MCE", "mce_worker.cli:main"),
    Member("stateful-eval", "STATEFUL_EVAL", "stateful_eval_worker.cli:main"),
)


def main() -> None:
    sys.exit(run_group(GROUP, MEMBERS))


if __name__ == "__main__":
    main()
