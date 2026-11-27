#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The neurosymbolic layer in the knowledge graph: writing the symbolic state of a session, and reading what is
needed to extract it (handoffs, final call, MAS)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from scope.connectors.base import Connector
from scope.core.exceptions import DatabaseError, NotFoundError
from scope.query_builders import neurosymbolic as ns
from scope_ontology.models import edges as ontology_edges
from scope_ontology.models import nodes as ontology_nodes

logger = logging.getLogger(__name__)

BATCH_SIZE = 200


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _written(rows: Any) -> int:
    return int(rows[0]["written"]) if rows else 0


def write_symbolic_state(
    db: Connector,
    session_id: str,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
    *,
    replace: bool = False,
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """Write the symbolic state of a session: nodes of the neurosymbolic ontology and the edges between them.

    A node is ``{"label", "id", "props"}`` (``label`` one of ``SYMBOLIC_LABELS``, ``props`` the data properties of
    the ontology's model of that label, which validates them; ``sessionId`` is added here); an edge is ``{"type",
    "from_id", "from_label", "to_id", "to_label", "props"?}`` whose ends are symbolic state nodes or original
    nodes of the KG (``KG_LABELS``). Nodes are MERGEd on ``id``, so writing the same state again changes
    nothing. The ends of an edge are matched, never created: an edge towards a node that is not in the KG is
    not written and is reported. With ``replace`` the symbolic state already written for the session is
    deleted first (a new extraction can word claims differently, so their ids differ); the original nodes are
    never touched. Every label, relationship type and set of properties is checked against the ontology's generated models
    before anything is written.

    Returns ``{"nodes": {label: written}, "edges": {type: written}, "missing_edges": {type: not written},
    "missing_nodes": {label: [ids not in the KG]}, "deleted": n}``.
    """
    if not session_id or not session_id.strip():
        raise ValueError("session_id must not be empty")
    node_list, edge_list = list(nodes), list(edges)
    for node in node_list:
        if node["label"] not in ns.SYMBOLIC_LABELS:
            raise ValueError(
                f"{node['label']!r} is not a label of the neurosymbolic layer"
            )
        # the properties must be those of the ontology's model of the label (pydantic ValidationError otherwise)
        getattr(ontology_nodes, node["label"])(id=node["id"], **node.get("props", {}))
    allowed_ends = set(ns.SYMBOLIC_LABELS) | set(ns.KG_LABELS)
    for edge in edge_list:
        if not ns.is_safe_identifier(edge["type"]) or not hasattr(
            ontology_edges, edge["type"]
        ):
            raise ValueError(f"{edge['type']!r} is not a relationship of the ontology")
        getattr(ontology_edges, edge["type"])(
            source_id=edge["from_id"], target_id=edge["to_id"]
        )
        for end in (edge["from_label"], edge["to_label"]):
            if end not in allowed_ends:
                raise ValueError(
                    f"{end!r} cannot be the end of a relationship of the neurosymbolic layer"
                )

    report: dict[str, Any] = {
        "nodes": {},
        "edges": {},
        "missing_edges": {},
        "missing_nodes": {},
        "deleted": 0,
    }
    try:
        if replace:
            query, params = ns.clean_session_query(
                sorted(ns.SYMBOLIC_LABELS), session_id
            )
            rows = db.execute(query, params)
            report["deleted"] = int(rows[0]["deleted"]) if rows else 0

        by_label: dict[str, list[dict[str, Any]]] = {}
        for node in node_list:
            props = {
                **node.get("props", {}),
                "sessionId": session_id,
            }  # to clean a session
            by_label.setdefault(node["label"], []).append(
                {"id": node["id"], "props": props}
            )
        for label, rows in by_label.items():
            db.execute_command(ns.ensure_index_query(label))
            for batch in _chunks(rows, batch_size):
                query, params = ns.merge_nodes_query(label, batch)
                report["nodes"][label] = report["nodes"].get(label, 0) + _written(
                    db.execute(query, params)
                )

        report["missing_nodes"] = _missing_kg_nodes(db, edge_list, batch_size)
        by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for edge in edge_list:
            by_key.setdefault(
                (edge["type"], edge["from_label"], edge["to_label"]), []
            ).append(
                {
                    "from_id": edge["from_id"],
                    "to_id": edge["to_id"],
                    "props": edge.get("props", {}),
                }
            )
        for (edge_type, from_label, to_label), rows in by_key.items():
            written = 0
            for batch in _chunks(rows, batch_size):
                query, params = ns.merge_edges_query(
                    edge_type, from_label, to_label, batch
                )
                written += _written(db.execute(query, params))
            report["edges"][edge_type] = report["edges"].get(edge_type, 0) + written
            if written < len(rows):
                report["missing_edges"][edge_type] = (
                    report["missing_edges"].get(edge_type, 0) + len(rows) - written
                )
    except Exception as exc:
        raise DatabaseError(
            f"Failed to write the symbolic state of session '{session_id}': {exc}"
        ) from exc
    return report


def _missing_kg_nodes(
    db: Connector, edges: list[Mapping[str, Any]], batch_size: int
) -> dict[str, list[str]]:
    """Ids the edges point at, among the original KG nodes, that the KG does not have."""
    wanted: dict[str, set[str]] = {}
    for edge in edges:
        for node_id, label in (
            (edge["from_id"], edge["from_label"]),
            (edge["to_id"], edge["to_label"]),
        ):
            if label in ns.KG_LABELS:
                wanted.setdefault(label, set()).add(node_id)
    missing: dict[str, list[str]] = {}
    for label, ids in wanted.items():
        found: set[str] = set()
        for batch in _chunks(sorted(ids), batch_size):
            query, params = ns.existing_ids_query(label, batch)
            found |= {row["id"] for row in db.execute(query, params) or []}
        if ids - found:
            missing[label] = sorted(ids - found)
    return missing


def get_session_handoffs(db: Connector, session_id: str) -> list[dict[str, Any]]:
    """The handoffs between the AgentCalls of a session in the KG, as ``{"from_call", "to_call", "kind"}``,
    each pair once (the first kind found), sorted for a stable output."""
    rows = db.execute(*ns.session_handoffs_query(session_id)) or []
    found: dict[tuple[str, str], str] = {}
    for row in sorted(rows, key=lambda r: (r["source"], r["target"], r["kind"])):
        found.setdefault((row["source"], row["target"]), row["kind"])
    return [
        {"from_call": a, "to_call": b, "kind": kind} for (a, b), kind in found.items()
    ]


def get_final_agent_call(db: Connector, session_id: str) -> str | None:
    """The id of the AgentCall whose final State is the final State of the session, None when the KG has none
    (no final State yet, or no AgentCall shares it). With several, the first by id."""
    rows = db.execute(*ns.final_agent_call_query(session_id)) or []
    ids = sorted({row["id"] for row in rows if row.get("id")})
    return ids[0] if ids else None


def get_mas_name(db: Connector, session_id: str) -> str | None:
    """The name of the MAS that ran the session, None when the KG has none."""
    rows = db.execute(*ns.mas_name_query(session_id)) or []
    names = sorted({row["name"] for row in rows if row.get("name")})
    return names[0] if names else None


def get_symbolic_extraction_input(db: Connector, session_id: str) -> dict[str, Any]:
    """Everything the symbolic extraction reads about a session, as plain dicts.

    Returns ``{"session": payload, "trajectory": trajectory}``. ``payload`` is the full session (Session node,
    Agent/LLM/Tool calls with their input and output, and the conversation), ``trajectory`` is
    ``get_session_trajectory`` (the calls in execution order, each with the AgentCall that owns it) with two keys
    added: ``handoffs`` (``get_session_handoffs``) and ``final_agent_call`` (``get_final_agent_call``). A failing
    handoff or final-call query is logged and leaves the key out: the extraction then infers the order of the
    agent calls from the trajectory alone, and treats the final call as unknown.

    Raises ``NotFoundError`` when the KG has no data for the session.
    """
    from scope.client.dal import get_session_trajectory
    from scope.providers import SCOPEKGProvider

    payload = SCOPEKGProvider(db=db).fetch(session_id, None)
    if not payload:
        raise NotFoundError(f"No data found in the KG for session '{session_id}'")
    trajectory = dict(get_session_trajectory(db, session_id))
    try:
        trajectory["handoffs"] = get_session_handoffs(db, session_id)
    except Exception:
        logger.warning(
            "Could not read the handoffs of session %s", session_id, exc_info=True
        )
    try:
        trajectory["final_agent_call"] = get_final_agent_call(db, session_id)
    except Exception:
        logger.warning(
            "Could not read the final agent call of session %s",
            session_id,
            exc_info=True,
        )
    return {"session": payload, "trajectory": trajectory}
