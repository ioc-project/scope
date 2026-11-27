#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""How cognitive failure reports are stored in the knowledge graph.

::

    (r:CognitiveFailureReport {id, name, code, confidence,
                                 reasoning, level, rule})
        -[:reportsOn]-> (s:Session)
    (r) -[:atCall]-> (c:LLMCall | ToolCall | AgentCall | ...)      only when it happened in one call

A report is one detection of one failure of the challenges taxonomy in a
session. Its id comes from the session, the failure and the call (and, for the
rule-based detectors, the rule, the level and the evidence: one node per finding),
so writing the same detection again overwrites it. ``level`` (``detection`` or
``risk``) and ``rule`` are only set by the rule-based detectors. The node properties are exactly the
fields of the model generated from the ontology
(``scope_ontology.models.nodes.CognitiveFailureReport``); every write is
validated against it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from scope_ontology.models.nodes import CognitiveFailureReport as ReportModel

QueryPair = tuple[str, dict[str, Any]]

CONFIDENCES = ("low", "medium", "high")
LEVELS = ("detection", "risk")

# Execution elements (calls) a report can point at.
CALL_LABELS = [
    "LLMCall",
    "ToolCall",
    "AgentCall",
    "CapabilityCall",
    "MASCall",
    "ProcessingCall",
]


def _failure_report_id(
    session_id: str, code: str, call_id: str | None, key: str | None = None
) -> str:
    """Id of the report of failure ``code`` in a session, at a call or (``None``) for the whole session.

    ``key`` tells apart several reports of one failure at one call (the rule-based detectors write one per finding).
    """
    parts = (session_id, code, call_id or "") + ((key,) if key else ())
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8"))
    return f"failurereport:{digest.hexdigest()[:12]}"


def failure_report_properties(
    session_id: str,
    code: str,
    confidence: str,
    reasoning: str,
    call_id: str | None = None,
    name: str | None = None,
    *,
    level: str | None = None,
    rule: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Properties of the ontology ``CognitiveFailureReport`` (validated against the model).

    ``name`` is the failure's name in the taxonomy (for example "Incomplete Synthesis"); it defaults to the code.

    ``level`` (``detection`` or ``risk``) and ``rule`` say what a rule-based detector found; ``key`` makes the id
    one per finding (see ``_failure_report_id``).

    Raises ``ValueError`` if ``confidence`` is not ``low``, ``medium`` or ``high`` or ``level`` is not
    ``detection`` or ``risk``, and
    ``pydantic.ValidationError`` if the report does not fit the model (for example an empty code).
    """
    if confidence not in CONFIDENCES:
        raise ValueError(f"confidence must be one of {CONFIDENCES}, not {confidence!r}")
    if level is not None and level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, not {level!r}")
    if not code.strip() or not reasoning.strip():
        raise ValueError("a failure report needs a code and a reasoning")
    props = ReportModel(
        id=_failure_report_id(session_id, code, call_id, key),
        name=(name or "").strip() or code,
        code=code,
        confidence=confidence,
        reasoning=reasoning,
        level=level or "",
        rule=rule or "",
    ).model_dump()
    for optional in ("level", "rule"):  # only the rule-based detectors set them
        if not props[optional]:
            del props[optional]
    return props


def write_failure_report_query(
    session_id: str, props: dict[str, Any], call_id: str | None = None
) -> QueryPair:
    """MERGE a report and link it to the session and, when ``call_id`` names one of its calls, to that call.

    Nothing is written if the session is missing. A ``call_id`` that is not a call of the session
    leaves the report on the session only.
    """
    query = """
    MATCH (s:Session {sessionId: $session_id})
    MERGE (r:CognitiveFailureReport {id: $props.id})
    SET r = $props
    MERGE (r)-[:reportsOn]->(s)
    WITH r
    OPTIONAL MATCH (c {id: $call_id})
    WHERE c.sessionId = $session_id AND any(label IN labels(c) WHERE label IN $call_labels)
    FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | MERGE (r)-[:atCall]->(c))
    RETURN r.id AS id, c IS NOT NULL AS atCall
    """
    return query, {
        "session_id": session_id,
        "props": props,
        "call_id": call_id,
        "call_labels": CALL_LABELS,
    }
