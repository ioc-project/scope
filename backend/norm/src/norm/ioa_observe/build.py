#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Dispatch: one handler per ``SpanName`` shape, accumulated into a Registry.

"session.start"  -> Session, MAS (placeholder)
"session.end"    -> update Session end time/duration
"*.graph"        -> MAS (declared), Agent/Tool (declared, from graph data)
"*.agent"        -> Agent (undeclared), AgentCall, initial/final State
"*.chat"         -> LLM, LLMCall, initial/final State, Transition
"*.tool"         -> Tool, ToolCall, initial/final State, Transition
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from scope_ontology.models.edges import (
    executes,
    executesAgent,
    executesLLM,
    executesMAS,
    executesProcessing,
    executesSession,
    executesTool,
)
from scope_ontology.models.nodes import (
    Agent,
    AgentCall,
    LLMCall,
    Session,
    Tool,
    ToolCall,
)

from .fields import (
    _to_float,
    get_span_attrs,
    get_span_session_id,
    get_span_start_time,
    normalize_record,
)
from .handlers import (
    handle_agent,
    handle_chat,
    handle_graph,
    handle_session_end,
    handle_session_start,
    handle_tool,
)
from .handlers.agent import _agent_id_for
from .heuristics import (
    assign_container_boundary_states,
    chain_agent_handoffs_fallback,
    chain_capability_calls,
    link_agent_handoffs,
)
from .heuristics.handoff import _source_span_ids
from .reconciliation import (
    handoff_spans,
    reconcile_relationships,
    remember_span_ancestry,
    strip_derived,
)
from .registry import HANDOFF_SOURCES_PROP, Registry
from .trajectory import (
    _clear_boundaries,
    _remember_trajectory,
    prune_trajectories,
    restore_trajectories,
)

_CALLS = (AgentCall, LLMCall, ToolCall)


_HANDLERS: dict[str, Any] = {
    "session.start": handle_session_start,
    "session.end": handle_session_end,
}


def _dispatch_span(span: dict[str, Any], registry: Registry) -> None:
    """Run the one per-span handler matching ``span``'s ``SpanName`` (if any)."""
    span_name = str(span.get("SpanName") or "")
    span_attrs = get_span_attrs(span)

    handler = _HANDLERS.get(span_name)
    if handler is not None:
        handler(span, span_attrs, registry)
    elif span_name.endswith(".graph"):
        handle_graph(span, span_attrs, registry)
    elif span_name.endswith(".agent"):
        handle_agent(span, span_attrs, registry)
    elif span_name.endswith(".chat"):
        handle_chat(span, span_attrs, registry)
    elif span_name.endswith(".tool"):
        handle_tool(span, span_attrs, registry)


def apply_heuristics(spans: list[dict[str, Any]], registry: Registry) -> None:
    """Cross-span clean-up of the trajectory, once ``spans`` are all handled."""
    reconcile_relationships(registry)
    strip_derived(registry)
    restore_trajectories(registry, registry.metadata_for)
    registry.nodes = dict(sorted(registry.nodes.items()))
    registry.edges = dict(
        sorted(registry.edges.items(), key=lambda item: (item[0][0].__name__, *item[0][1:]))
    )
    # Agent handoffs
    explicit_handoff_targets = link_agent_handoffs(handoff_spans(registry), registry)
    # Fallback: timestamp-order sibling AgentCalls the above left unlinked
    chain_agent_handoffs_fallback(registry, explicit_handoff_targets)
    # Capability calls within agent call
    chain_capability_calls(registry)
    # Session and MAS call i/o
    assign_container_boundary_states(registry)
    prune_trajectories(registry, registry.metadata_for)
    typed_execution = (
        executesAgent,
        executesLLM,
        executesTool,
        executesProcessing,
        executesMAS,
        executesSession,
    )
    registry.remove_edges_where(
        lambda edge, source, target: (
            edge is executes
            and not any((typed, source, target) in registry.edges for typed in typed_execution)
        )
    )
    for edge, source, target in list(registry.edges):
        if edge in typed_execution:
            registry.add_edge(executes(source_id=source, target_id=target))


def serialize_registry(
    registry: Registry,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Registry -> (nodes, edges) JSON dicts."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for item in registry.all_items():
        payload = item.to_json()
        if "node_type" not in payload:
            edges.append(payload)
            continue
        # hierarchyLevel/layer are ClassVar on the ontology models (a fixed
        # constant per concrete class, not per-instance data), so pydantic
        # never serializes them -- read them straight off the class and
        # denormalize them into the output JSON instead.
        for attr in ("hierarchyLevel", "layer"):
            value = getattr(item, attr, None)
            if value is not None:
                payload[attr] = value
        extra = registry.extras.get((type(item).__name__, item.id), {})
        payload.update({key: value for key, value in extra.items() if value})
        nodes.append(payload)
    return nodes, edges


def build_kg(
    spans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build (nodes, edges) JSON dicts from raw ClickHouse-shaped OTel spans."""
    registry = Registry()
    for span in spans:
        dispatch_span(span, registry)
    apply_heuristics(spans, registry)
    return serialize_registry(registry)


def dispatch_span(record: dict[str, Any], registry: Registry) -> None:
    record, attrs = normalize_record(record, "span")
    remember_span_ancestry(record, attrs, registry)
    meta = registry.metadata_for
    name = str(record.get("SpanName") or "")
    if name in ("session.start", "session.end"):
        session = registry.get(Session, get_span_session_id(record, attrs))
        if session is None:
            initial_attrs = attrs
            if name == "session.end":
                initial_attrs = {
                    **attrs,
                    "ioa_start_time": _to_float(attrs.get("session.ended_at"))
                    or get_span_start_time(record, attrs),
                }
            handle_session_start(record, initial_attrs, registry)
            session = registry.get(Session, get_span_session_id(record, attrs))
        if session is not None:
            _dispatch_span(record, registry)
            if name == "session.start":
                meta(session)["sessionSpanId"] = str(record.get("SpanId") or "")
                start = min(session.startTime, get_span_start_time(record, attrs))
                registry.upsert(
                    session.model_copy(
                        update={
                            "startTime": start,
                            "duration": (session.endTime - start) * 1000,
                        }
                    )
                )
        return
    if name.endswith(".graph"):
        handle_graph(record, attrs, registry)
        return
    if name.endswith(".agent"):
        display = str(
            attrs.get("ioa_observe.entity.name")
            or attrs.get("agent_id")
            or record.get("AgentId")
            or ""
        )
        application = str(attrs.get("application_id") or "")
        runtime_id = str(record.get("AgentId") or attrs.get("agent_id") or "")
        attrs = {
            **attrs,
            "agent_id": _agent_id_for(registry, display, (application,)),
        }
    else:
        runtime_id = ""
    scratch = Registry()
    _dispatch_span(
        {
            **record,
            "SpanAttributes": attrs,
            **({"AgentId": attrs["agent_id"]} if name.endswith(".agent") else {}),
        },
        scratch,
    )
    for cls in _CALLS:
        for call in scratch.all_of(cls):
            existing = registry.find(
                cls,
                lambda n: (
                    n.sessionId == call.sessionId
                    and (n.spanId == call.spanId or call.spanId in meta(n).get("spanIds", []))
                ),
            )
            tool_call_id = str(attrs.get("gen_ai.tool.call.id") or "") if cls is ToolCall else ""
            if existing is None and tool_call_id:
                existing = registry.get(ToolCall, tool_call_id)
            canonical_id = tool_call_id or (existing.id if existing is not None else call.id)
            if existing is not None and existing.id != canonical_id:
                old_metadata = deepcopy(meta(existing))
                registry.merge_node_into(cls, existing.id, canonical_id)
            else:
                old_metadata = {}
            if call.id != canonical_id:
                scratch.merge_node_into(cls, call.id, canonical_id)
                call = call.model_copy(update={"id": canonical_id})
                scratch.add(call)
            meta(call).update(old_metadata)
            if record.get("TraceId"):
                meta(call)["traceId"] = str(record["TraceId"])
            if meta(call).get("spanComplete") and registry.get(cls, call.id) is not None:
                return
            # The completed span replaces provisional event boundaries, not message States.
            _clear_boundaries(registry, call.id)
            registry.upsert(call)
            _remember_trajectory(scratch, call, meta)
            for key in (
                "spanIds",
                "spanPriority",
                "input",
                "inputPriority",
                "output",
                "outputPriority",
                "complete",
            ):
                meta(call).pop(key, None)
            meta(call)["spanComplete"] = True
            owner = str(attrs.get("ioa_observe.agent.span_id") or call.parentSpanId or "")
            if owner:
                meta(call)["ownerSpanId"] = owner
            owner_agent = str(record.get("AgentId") or attrs.get("agent_id") or "")
            if owner_agent and cls in (LLMCall, ToolCall):
                meta(call)["ownerAgentId"] = owner_agent
            if cls is AgentCall:
                sources = _source_span_ids(attrs)
                if sources:
                    registry.extras.setdefault(("AgentCall", call.id), {})[HANDOFF_SOURCES_PROP] = (
                        sources
                    )
    for node in scratch.nodes.values():
        existing_node = registry.add(node)
        if (
            isinstance(node, (Agent, Tool))
            and isinstance(existing_node, (Agent, Tool))
            and node.description
            and not existing_node.description
        ):
            registry.upsert(existing_node.model_copy(update={"description": node.description}))
        if isinstance(node, Agent) and runtime_id:
            meta(node)["aliases"] = sorted(set(meta(node).get("aliases", []) + [runtime_id]))
    for edge in scratch.edges.values():
        registry.add_edge(edge)
