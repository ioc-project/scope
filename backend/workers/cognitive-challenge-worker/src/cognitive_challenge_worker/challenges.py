#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The cognitive challenges the worker can look for, from the challenges taxonomy."""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent / "data" / "challenges.json"


@lru_cache(maxsize=1)
def all_challenges() -> dict[str, dict[str, Any]]:
    """Every challenge of the taxonomy by code (regenerate with scripts/build_challenges.py)."""
    return {c["code"]: c for c in json.loads(_DATA.read_text(encoding="utf-8"))["challenges"]}


def select_challenges(codes: Iterable[str]) -> list[dict[str, Any]]:
    """The challenges with these codes, in the given order. Raises ``ValueError`` on an unknown code.

    Codes are matched ignoring case, so ``ic-2`` is ``IC-2``.
    """
    known = all_challenges()
    selected: dict[str, dict[str, Any]] = {}
    unknown = []
    for code in codes:
        challenge = known.get(str(code).strip().upper())
        if challenge is None:
            unknown.append(str(code))
        else:
            selected[challenge["code"]] = challenge
    if unknown:
        raise ValueError(f"Unknown cognitive challenge(s): {', '.join(unknown)}. Available: {', '.join(known)}")
    return list(selected.values())
