"""Findings of the detectors -> detections, the input of ``scope.client.failures.write_cognitive_failure_reports``.

Every finding is its own ``CognitiveFailureReport``: ``level`` (``detection`` or ``risk``) and ``rule`` are
fields of the node and the reasoning is the finding's message. The node id is keyed by the session, the
failure, the call and the finding's identity (rule, level, evidence), so re-running a session overwrites.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from failure_detection.finding import Finding
from failure_detection.graph import SymbolicGraph

#: The ``level`` of a report, from the finding's.
LEVEL = {"detect": "detection", "risk": "risk"}
#: ``confidence`` is required by the KG (scope's UI ranks failures by it) but carries no information of its
#: own here: the rules are deterministic, so it is derived from the level (a consequence seen / a warning).
CONFIDENCE = {"detect": "high", "risk": "low"}
MAX_REASONING = 4000


def call_of(g: SymbolicGraph, finding: Finding) -> str | None:
    """The call a finding happened at: that of its first element that belongs to one."""
    for e in finding.evidence:
        if e in g.calls:
            return e
        if e in g.claims:
            return g.claims[e].generated_by or None
        if e in g.facts:
            return g.facts[e].grounded_in or None
        if e in g.tasks and g.tasks[e].owned_by:
            return g.tasks[e].owned_by
    return None


def detections(findings: Iterable[Finding], g: SymbolicGraph) -> list[dict[str, Any]]:
    """One detection per finding: ``code``, ``level``, ``rule``, ``confidence``, ``reasoning``, ``call_id``, ``key``."""
    unique = {f.key: f for f in findings}.values()
    out = []
    for f in unique:
        out.append(
            {
                "code": f.failure,
                "level": LEVEL[f.level],
                "rule": f.rule,
                "confidence": CONFIDENCE[f.level],
                "reasoning": f.message[:MAX_REASONING] or f.rule,
                "call_id": call_of(g, f),
                # identity of the finding, so two findings of one failure at one call are two nodes
                "key": repr((f.rule, f.level, f.evidence)),
            }
        )
    return sorted(
        out, key=lambda d: (d["code"], d["call_id"] or "", d["level"], d["rule"], d["key"])
    )
