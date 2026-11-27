#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Deterministic span/trace IDs and W3C traceparent parsing.

Hooks and the rollout reconciler run in different processes at different
times. Deriving every ID from (harness, session, kind, native id) lets both
paths name the same entity identically, which is what makes dedupe possible.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Tuple

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def _hex(length: int, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:length]
    # All-zero IDs are invalid in OTLP; vanishingly unlikely, but never emit one.
    return digest if digest.strip("0") else "1".rjust(length, "0")


def span_id(*parts: str) -> str:
    return _hex(16, *parts)


def trace_id(*parts: str) -> str:
    return _hex(32, *parts)


def parse_traceparent(value: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (trace_id, parent_span_id) from a W3C traceparent, or None."""
    if not value:
        return None
    match = _TRACEPARENT.match(value.strip().lower())
    if not match or not match.group(1).strip("0") or not match.group(2).strip("0"):
        return None
    return match.group(1), match.group(2)
