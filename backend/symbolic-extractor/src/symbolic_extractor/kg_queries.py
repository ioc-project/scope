"""Cypher for writing the symbolic state to the KG.

Pure functions that return ``(query, params)``, the way ``scope.query_builders`` does, so this module can
move to ``scope/backend/api/scope/query_builders/neurosymbolic.py`` unchanged. Labels and relationship
types cannot be Cypher parameters: callers must pass names checked by ``is_safe_identifier``.

Conventions copied from scope: nodes are matched on ``id`` and MERGEd, so a re-run overwrites; every
node carries ``sessionId`` so a session can be cleaned (``clean_session_query``, like
``clean_metrics_query``); a write returns the number of rows that matched, so the caller can report what
was not found instead of writing nothing silently.
"""

from __future__ import annotations

import re
from typing import Any

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_safe_identifier(name: str) -> bool:
    """Whether ``name`` can be used as a label or relationship type."""
    return bool(_SAFE_IDENTIFIER.match(name))


def _check(name: str) -> str:
    if not is_safe_identifier(name):
        raise ValueError(f"Unsafe Cypher identifier: {name!r}")
    return name


def merge_nodes_query(label: str, rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
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


def clean_session_query(labels: list[str], session_id: str) -> tuple[str, dict[str, Any]]:
    """Delete the symbolic state nodes of a session (never the original KG nodes: pass only our labels)."""
    match = "|".join(_check(label) for label in labels)
    query = f"""
    MATCH (n:{match})
    WHERE n.sessionId = $session_id
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    return query, {"session_id": session_id}
