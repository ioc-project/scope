#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Stateless, record-at-a-time normalization against a graph database.

``build_kg`` needs the whole span array. ``StreamNormalizer.process`` takes a
single span and keeps nothing between calls: the graph already stored in the
database *is* the state. For each span it

1. loads what it needs from the database through a ``GraphReader`` (the
   span's session subgraph plus the structural nodes the span touches),
2. adds the span to that graph with the usual per-span handler,
3. recomputes the cross-span heuristics (handoffs, capability chains,
   container boundary states) over the loaded graph, and
4. returns only the difference against what was loaded, as a ``StreamDelta``,
   for the caller to write back.

Transport ancestry is loaded separately, including spans without session ids.
It is persisted through ``StreamDelta.span_contexts``, never as ontology executions.

The heuristics' derived items (bridge ProcessingCalls, container-boundary
Transitions and States) are dropped from the loaded graph and rebuilt every
time, so a late-arriving span replaces stale ones instead of piling up.

Reconciliation evidence is persisted as ``rtNormalization`` extras because
ontology models forbid extra fields. Legacy ``handoffSourceSpanIds`` on
AgentCall payloads remain supported.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import ValidationError
from scope_ontology.models import edges as edge_models
from scope_ontology.models import nodes as node_models
from scope_ontology.models.nodes import (
    Agent,
    Session,
)

from ..verifier import PROPERTY_DOMAIN_RANGE
from .build import apply_heuristics, dispatch_span, serialize_registry
from .events import dispatch_event
from .fields import (
    _hash_id,
    get_event_session_id,
    get_span_session_id,
    normalize_record,
    parse_json_object,
)
from .handlers.topology import _ensure_session_and_mas
from .reconciliation import span_context
from .registry import HANDOFF_SOURCES_PROP, NORMALIZATION_PROP, Registry, SpanContext

Kind = Literal["span", "event"]
KINDS = ("span", "event")


@dataclass
class StreamDelta:
    """What a single ``StreamNormalizer.process`` call changed in the database.

    ``nodes``/``edges`` are new or modified items; ``removed_*`` are items the
    database holds that the recomputed graph no longer contains. Edges carry
    ``from_type``/``to_type`` so they can be written without their endpoint
    nodes being part of the same delta. ``span_contexts`` is private ancestry
    evidence that adapters must persist separately from ontology nodes.
    """

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    removed_edges: list[dict[str, Any]] = field(default_factory=list)
    removed_nodes: list[dict[str, Any]] = field(default_factory=list)
    span_contexts: list[SpanContext] = field(default_factory=list)


class GraphReader(Protocol):
    def load_span_contexts(self, trace_id: str) -> list[SpanContext]:
        """Load private ancestry evidence, including sessionless transport spans."""
        ...

    def load(
        self, session_id: str, node_ids: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(nodes, edges)`` as stored.

        ``nodes``: every node with ``sessionId == session_id`` or ``id`` in
        ``node_ids``, every Agent whose ``name`` is in ``node_ids`` (so ``node_ids``
        may also carry agent names), plus the endpoints of the edges below, each a
        dict with ``node_type`` and its properties. ``edges``: every edge touching a
        node of that session or one of those Agents, plus those among ``node_ids``,
        each a dict with ``edge_type``, ``from_id`` and ``to_id``.
        """
        ...


def _node_key(node: dict[str, Any]) -> tuple[str, str, str]:
    return ("node", str(node["node_type"]), str(node["id"]))


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return ("edge", str(edge["edge_type"]), str(edge["from_id"]), str(edge["to_id"]))


def _rehydrate(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> Registry:
    registry = Registry()
    for raw in nodes:
        cls = getattr(node_models, str(raw["node_type"]), None)
        if cls is None:
            continue
        data = {k: v for k, v in raw.items() if k in cls.model_fields}
        try:
            node = cls.model_validate(data)
        except ValidationError:
            # A partial node written ahead of its span will be filled in by that span.
            continue
        registry.upsert(node)
        extra = {
            key: deepcopy(raw[key])
            for key in (NORMALIZATION_PROP, HANDOFF_SOURCES_PROP)
            if raw.get(key)
        }
        if extra:
            registry.extras[(type(node).__name__, node.id)] = extra
    for raw in edges:
        cls = getattr(edge_models, str(raw["edge_type"]), None)
        if cls is None:
            continue
        registry.add_edge(cls(source_id=str(raw["from_id"]), target_id=str(raw["to_id"])))
    return registry


def _endpoint_type(edge_type: str, side: int, node_id: str, node_types: dict[str, set[str]]) -> str:
    """Type of an edge endpoint: its node's own type, else the one class the
    ontology allows there (``side`` 0 = domain, 1 = range). "" if ambiguous.

    An endpoint node can legitimately not exist yet -- e.g. ``usesLLM`` is
    emitted by a chat span before the agent span that creates the Agent --
    in which case the database holds a partial (id-only) node until then.
    """
    candidates = PROPERTY_DOMAIN_RANGE.get(edge_type, ([], []))[side]
    actual = node_types.get(node_id, set())
    compatible = {
        name
        for name in actual
        if any(base.__name__ in candidates for base in getattr(node_models, name).__mro__)
    }
    if len(compatible) == 1:
        return next(iter(compatible))
    if len(actual) == 1:
        return next(iter(actual))
    if len(actual) > 1:
        raise ValueError(f"Ambiguous {edge_type} endpoint {node_id!r}: {sorted(actual)}")
    return str(candidates[0]) if len(candidates) == 1 else ""


def _snapshot(registry: Registry) -> dict[tuple[str, ...], dict[str, Any]]:
    nodes, edges = serialize_registry(registry)
    node_types: dict[str, set[str]] = {}
    for node in nodes:
        node_types.setdefault(str(node["id"]), set()).add(str(node["node_type"]))
    execution_types = {
        (source, target): cls.__name__
        for cls, source, target in registry.edges
        if cls.__name__.startswith("executes") and cls.__name__ != "executes"
    }
    items: dict[tuple[str, ...], dict[str, Any]] = {}
    for node in nodes:
        items[_node_key(node)] = node
    for edge in edges:
        edge_type = edge["edge_type"]
        if edge_type == "executes":
            edge_type = execution_types.get((edge["from_id"], edge["to_id"]), edge_type)
        edge["from_type"] = _endpoint_type(edge_type, 0, str(edge["from_id"]), node_types)
        edge["to_type"] = _endpoint_type(edge_type, 1, str(edge["to_id"]), node_types)
        items[_edge_key(edge)] = edge
    return items


def _diff(
    before: dict[tuple[str, ...], dict[str, Any]], after: dict[tuple[str, ...], dict[str, Any]]
) -> StreamDelta:
    delta = StreamDelta()
    for key, item in after.items():
        if before.get(key) != item:
            (delta.nodes if key[0] == "node" else delta.edges).append(item)
    for key, item in before.items():
        if key not in after:
            (delta.removed_nodes if key[0] == "node" else delta.removed_edges).append(item)
    return delta


class StreamNormalizer:
    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    def process(self, record: dict[str, Any], kind: Kind = "span") -> StreamDelta:
        if kind not in ("span", "event"):
            raise ValueError(f"Unknown kind {kind!r}")
        record, attrs = normalize_record(record, kind)
        session_id = (
            get_span_session_id(record, attrs)
            if kind == "span"
            else get_event_session_id(record, attrs)
        )
        context = span_context(record, attrs) if kind == "span" else None
        trace_id = str(record.get("TraceId") or (f"session:{session_id}" if session_id else ""))
        contexts = (
            {
                (item.trace_id, item.span_id): item
                for item in self._reader.load_span_contexts(trace_id)
            }
            if trace_id
            else {}
        )
        delta = StreamDelta()
        if context is not None:
            key = (context.trace_id, context.span_id)
            if contexts.get(key) != context:
                delta.span_contexts.append(context)
            contexts[key] = context
        sessions = (
            {session_id}
            if session_id
            else {item.session_id for item in contexts.values() if item.session_id}
        )
        # A sessionless transport arriving last may complete ancestry in several sessions.
        for current_session in sorted(sessions or {""}):
            change = self._process(record, attrs, kind, current_session, contexts)
            delta.nodes.extend(change.nodes)
            delta.edges.extend(change.edges)
            delta.removed_nodes.extend(change.removed_nodes)
            delta.removed_edges.extend(change.removed_edges)
        return delta

    def _process(
        self,
        record: dict[str, Any],
        attrs: dict[str, Any],
        kind: Kind,
        session_id: str,
        contexts: dict[tuple[str, str], SpanContext],
    ) -> StreamDelta:
        application = str(attrs.get("application_id") or "")
        touched = {session_id, application, str(record.get("SpanId") or "")}
        touched.update(
            str(attrs.get(k) or "")
            for k in (
                "agent_id",
                "agent.name",
                "source.agent",
                "target.agent",
                "ioa_observe.entity.name",
            )
        )
        graph_data = (
            parse_json_object(attrs["gen_ai.ioa.graph"], "gen_ai.ioa.graph")
            if attrs.get("gen_ai.ioa.graph")
            else {}
        )
        if not isinstance(graph_data.get("nodes", {}), dict):
            raise ValueError("gen_ai.ioa.graph.nodes must be an object")
        for node_id, node in graph_data.get("nodes", {}).items():
            touched.add(str(node_id))
            if isinstance(node, dict):
                touched.add(str(node.get("name") or node_id))
        # Structural ids (tools, models, cards) need loading even before a session exists.
        scratch = Registry()
        recognized = False
        if kind == "span":
            dispatch_span(
                {
                    **record,
                    "SpanAttributes": {k: v for k, v in attrs.items() if k != "gen_ai.ioa.graph"},
                },
                scratch,
            )
        else:
            recognized = dispatch_event(record, attrs, scratch)
            if not recognized or not scratch.nodes:
                return StreamDelta()
        touched.update(n.id for n in scratch.nodes.values())
        touched.update(n.name for n in scratch.all_of(Agent))
        for _, source, target in scratch.edges:
            touched.update((source, target))
        stored_nodes, stored_edges = self._reader.load(session_id, sorted(touched - {""}))
        pending_tools = {
            tool_id
            for node in stored_nodes
            if node["node_type"] == "Session" and node["id"] == session_id
            for tool_id in (node.get(NORMALIZATION_PROP) or {}).get("presentedToolIds", [])
        }
        structural_ids = {str(node["id"]) for node in stored_nodes if not node.get("sessionId")}
        related_ids = pending_tools | structural_ids
        if not related_ids <= touched:
            touched.update(related_ids)
            stored_nodes, stored_edges = self._reader.load(session_id, sorted(touched - {""}))
        stored_nodes = [
            n for n in stored_nodes if not n.get("sessionId") or n["sessionId"] == session_id
        ]
        ids = {n["id"] for n in stored_nodes}
        stored_edges = [e for e in stored_edges if e["from_id"] in ids and e["to_id"] in ids]
        registry = _rehydrate(stored_nodes, stored_edges)
        registry.span_contexts.update(contexts)
        meta = registry.metadata_for
        before = deepcopy(_snapshot(registry))
        event_id = str(
            attrs.get("event.id") or _hash_id("event", json.dumps(record, sort_keys=True))
        )
        session = registry.get(Session, session_id)
        if (
            kind == "event"
            and session is not None
            and event_id in meta(session).get("eventIds", [])
        ):
            return StreamDelta()
        if kind == "span":
            dispatch_span(record, registry)
        else:
            if recognized or attrs.get("event.name") in (
                "llm.tools.presented",
                "a2a.message.completed",
            ):
                _ensure_session_and_mas(record, attrs, registry)
            dispatch_event(record, attrs, registry)
            session = registry.get(Session, session_id)
            if session is not None:
                meta(session)["eventIds"] = sorted(
                    set(meta(session).get("eventIds", []) + [event_id])
                )
        apply_heuristics([], registry)
        return _diff(before, _snapshot(registry))


class InMemoryGraph:
    """A ``GraphReader`` plus writer over plain dicts; for tests and dry runs."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.span_contexts: dict[tuple[str, str], SpanContext] = {}

    def load_span_contexts(self, trace_id: str) -> list[SpanContext]:
        return [item for (trace, _), item in self.span_contexts.items() if trace == trace_id]

    def load(
        self, session_id: str, node_ids: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ids = set(node_ids)
        in_session = {
            n["id"] for n in self.nodes.values() if session_id and n.get("sessionId") == session_id
        }
        named = {
            n["id"]
            for n in self.nodes.values()
            if n["node_type"] == "Agent" and n.get("name") in ids
        }
        edges = [
            e
            for e in self.edges.values()
            if e["from_id"] in in_session
            or e["to_id"] in in_session
            or e["from_id"] in named
            or e["to_id"] in named
            or (e["from_id"] in ids and e["to_id"] in ids)
        ]
        wanted = (
            in_session | named | ids | {e["from_id"] for e in edges} | {e["to_id"] for e in edges}
        )
        nodes = [dict(node) for node in self.nodes.values() if node["id"] in wanted]
        return nodes, [dict(e) for e in edges]

    def apply(self, delta: StreamDelta) -> None:
        for context in delta.span_contexts:
            self.span_contexts[(context.trace_id, context.span_id)] = context
        for edge in delta.removed_edges:
            self.edges.pop((edge["edge_type"], edge["from_id"], edge["to_id"]), None)
        for node in delta.removed_nodes:
            self.nodes.pop((node["node_type"], node["id"]), None)
            self.edges = {
                k: e for k, e in self.edges.items() if node["id"] not in (e["from_id"], e["to_id"])
            }
        for node in delta.nodes:
            key = (node["node_type"], node["id"])
            existing = self.nodes.get(key)
            if existing is None:
                self.nodes[key] = dict(node)
            else:
                existing.update(node)
        for edge in delta.edges:
            self.edges[(edge["edge_type"], edge["from_id"], edge["to_id"])] = dict(edge)
