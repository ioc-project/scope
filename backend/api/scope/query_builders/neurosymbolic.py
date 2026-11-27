#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Cypher for the neurosymbolic layer: the symbolic state of a session and what is read to extract it.

Pure functions that return ``(query, params)``, like the other modules of ``scope.query_builders``. Labels and
relationship types cannot be Cypher parameters: callers pass names checked by ``is_safe_identifier``.

Conventions: nodes are matched on ``id`` and MERGEd, so a re-run overwrites; every node carries ``sessionId``
so a session can be cleaned (``clean_session_query``, like ``clean_metrics_query``); a write returns the
number of rows that matched, so the caller can report what was not found instead of writing nothing silently.

The nodes are the classes of ``neurosymbolic-ontology.ttl``; the original nodes of the execution KG
(``Agent``, ``AgentCall``, ``LLMCall``, ``ToolCall``) are only ever matched.
"""

from __future__ import annotations

import re
from typing import Any

#: Labels of the symbolic state nodes (classes of the neurosymbolic ontology): the only ones written and cleaned.
SYMBOLIC_LABELS = (
    "Claim",
    "Fact",
    "Goal",
    "Task",
    "Constraint",
    "AgentCapability",
    "SystemPrompt",
)
#: Labels of the original KG nodes a symbolic state node points at: only ever matched.
KG_LABELS = ("Agent", "AgentCall", "LLMCall", "ToolCall")

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_safe_identifier(name: str) -> bool:
    """Whether ``name`` can be used as a label or relationship type."""
    return bool(_SAFE_IDENTIFIER.match(name))


def _check(name: str) -> str:
    if not is_safe_identifier(name):
        raise ValueError(f"Unsafe Cypher identifier: {name!r}")
    return name


def merge_nodes_query(
    label: str, rows: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """MERGE nodes of one label on ``id``. Each row is ``{"id": ..., "props": {...}}``."""
    query = f"""
    UNWIND $rows AS row
    MERGE (n:{_check(label)} {{id: row.id}})
    SET n += row.props
    RETURN count(n) AS written
    """
    return query, {"rows": rows}


def merge_edges_query(
    edge_type: str, from_label: str, to_label: str, rows: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """MERGE one relationship type between two labelled ends. Each row is ``{"from_id", "to_id", "props"}``.

    Ends are matched, never created: a row whose end does not exist is not written, and the returned
    ``written`` is lower than ``len(rows)``.
    """
    query = f"""
    UNWIND $rows AS row
    MATCH (a:{_check(from_label)} {{id: row.from_id}})
    MATCH (b:{_check(to_label)} {{id: row.to_id}})
    MERGE (a)-[r:{_check(edge_type)}]->(b)
    SET r += row.props
    RETURN count(r) AS written
    """
    return query, {"rows": rows}


def existing_ids_query(label: str, ids: list[str]) -> tuple[str, dict[str, Any]]:
    """The ids, among ``ids``, of nodes of ``label`` that exist (to find the KG nodes a write points at)."""
    query = f"""
    MATCH (n:{_check(label)})
    WHERE n.id IN $ids
    RETURN n.id AS id
    """
    return query, {"ids": ids}


def ensure_index_query(label: str) -> str:
    """DDL for the ``id`` index of a label (idempotent)."""
    return f"CREATE INDEX {_check(label).lower()}_id IF NOT EXISTS FOR (n:{label}) ON (n.id)"


def clean_session_query(
    labels: list[str], session_id: str
) -> tuple[str, dict[str, Any]]:
    """Delete the symbolic state nodes of a session (never the original KG nodes: pass only our labels)."""
    match = "|".join(_check(label) for label in labels)
    query = f"""
    MATCH (n:{match})
    WHERE n.sessionId = $session_id
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    return query, {"session_id": session_id}


#: Handoffs between the AgentCalls of a session, as the normalisation (``norm``) wrote them. When an
#: agent span names its predecessor (``ioa_observe.handoff.source.span_ids``) and the predecessor's
#: final State differs from the successor's initial State, ``norm`` adds a ``ProcessingCall`` (``Processing``
#: named ``handoff``) between the two States; with no such attribute it chains sibling calls by start time.
#: When the two States are identical they are merged into one, so the handoff is a State shared by the two
#: calls. There is no AgentCall -> AgentCall edge: the link is read through the States.
HANDOFFS_QUERY = """
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(:State)<-[:hasInitialState]-(p:ProcessingCall)
MATCH (p)-[:executesProcessing]->(pr:Processing)
MATCH (p)-[:hasFinalState]->(:State)<-[:hasInitialState]-(b:AgentCall)
WHERE a <> b AND pr.name STARTS WITH 'handoff'
RETURN a.id AS source, b.id AS target, pr.name AS kind
UNION
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(:State)<-[:hasInitialState]-(b:AgentCall)
WHERE a <> b
RETURN a.id AS source, b.id AS target, 'shared_state' AS kind
"""

#: The AgentCall that produced the final answer of a session. The Session's ``hasFinalState`` State is the
#: very same node as the ``hasFinalState`` of one AgentCall, so it is found through that shared State: no
#: timestamp or text matching.
FINAL_AGENT_CALL_QUERY = """
MATCH (:Session {sessionId: $session_id})-[:hasFinalState]->(fs:State)
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(fs)
RETURN DISTINCT a.id AS id
"""

#: The MAS (multi-agent system) a session belongs to; the edge is matched in either direction.
MAS_NAME_QUERY = """
MATCH (m:MAS)-[:executesMAS]-(:MASCall {sessionId: $session_id})
RETURN DISTINCT m.name AS name
"""


def session_handoffs_query(session_id: str) -> tuple[str, dict[str, Any]]:
    """The handoffs between the AgentCalls of a session: rows ``source``, ``target``, ``kind``."""
    return HANDOFFS_QUERY, {"session_id": session_id}


def final_agent_call_query(session_id: str) -> tuple[str, dict[str, Any]]:
    """The AgentCall(s) that share the final State of a session: rows ``id``."""
    return FINAL_AGENT_CALL_QUERY, {"session_id": session_id}


def mas_name_query(session_id: str) -> tuple[str, dict[str, Any]]:
    """The MAS that ran a session: rows ``name``."""
    return MAS_NAME_QUERY, {"session_id": session_id}
