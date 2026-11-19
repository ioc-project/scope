# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Persisted lifecycle and cross-record relationship reconciliation."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from scope_ontology.models.edges import (
    belongsToMASCall,
    correspondsToTransition,
    executesAgent,
    executesLLM,
    executesSession,
    executesTool,
    hasAgentCall,
    hasFinalState,
    hasInitialState,
    hasLLMCall,
    hasMASCall,
    hasState,
    hasToolCall,
    usesLLM,
    usesTool,
)
from scope_ontology.models.nodes import (
    Agent,
    AgentCall,
    LLMCall,
    MASCall,
    ProcessingCall,
    Session,
    State,
    Tool,
    ToolCall,
    Transition,
)

from .fields import _hash_id, get_span_session_id
from .handlers.agent import _agent_id_for, _own_agent_id
from .handlers.graph import reconcile_graph_agents
from .handlers.mcp import _message_tool_name
from .heuristics.container_boundary_states import update_container_times
from .registry import HANDOFF_SOURCES_PROP, Registry, SpanContext

logger = logging.getLogger(__name__)
_CALLS = (AgentCall, LLMCall, ToolCall)


def span_context(record: dict[str, Any], attrs: dict[str, Any]) -> SpanContext | None:
    session_id = get_span_session_id(record, attrs)
    span_id = str(record.get("SpanId") or "")
    trace_id = str(record.get("TraceId") or (f"session:{session_id}" if session_id else ""))
    if not trace_id or not span_id:
        return None
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=str(record.get("ParentSpanId") or ""),
        owner_span_id=str(attrs.get("ioa_observe.agent.span_id") or ""),
        session_id=session_id,
    )


def event_context(record: dict[str, Any], attrs: dict[str, Any]) -> SpanContext | None:
    if attrs.get("event.name") not in (
        "mcp.message.sent",
        "mcp.message.received",
        "mcp.message.completed",
    ):
        return None
    operation = str(attrs.get("operation.name") or "")
    if operation and operation != "tools/call":
        return None
    if not operation and not _message_tool_name(attrs.get("message.input")):
        return None
    source_trace = str(attrs.get("source.agent.trace.id") or "")
    source_owner = (
        attrs.get("source.agent.span.id")
        if not source_trace or source_trace == record.get("TraceId")
        else ""
    )
    owner = attrs.get("ioa_observe.agent.span_id") or source_owner
    if not owner:
        return None
    return span_context(
        record,
        {
            **attrs,
            "ioa_observe.agent.span_id": owner,
        },
    )


def merge_span_context(context: SpanContext, previous: SpanContext | None) -> SpanContext:
    if previous is None:
        return context
    return replace(
        context,
        parent_span_id=context.parent_span_id or previous.parent_span_id,
        owner_span_id=context.owner_span_id or previous.owner_span_id,
        session_id=context.session_id or previous.session_id,
    )


def remember_span_ancestry(
    record: dict[str, Any], attrs: dict[str, Any], registry: Registry
) -> None:
    context = span_context(record, attrs)
    if context is not None:
        key = (context.trace_id, context.span_id)
        registry.span_contexts[key] = merge_span_context(context, registry.span_contexts.get(key))


def _owning_agent_call(
    registry: Registry, child: LLMCall | ToolCall, agents: list[AgentCall]
) -> AgentCall | None:
    meta = registry.metadata_for
    owner = str(meta(child).get("ownerSpanId") or child.parentSpanId or "")
    trace_id = str(meta(child).get("traceId") or "")
    context_trace = trace_id or f"session:{child.sessionId}"
    candidates = {
        agent.spanId: agent
        for agent in agents
        if agent.sessionId == child.sessionId
        and not (trace_id and meta(agent).get("traceId") and meta(agent)["traceId"] != trace_id)
    }
    visited: set[str] = set()
    while owner and owner not in visited:
        visited.add(owner)
        ancestor = registry.span_contexts.get((context_trace, owner))
        if ancestor is not None and ancestor.session_id not in ("", child.sessionId):
            return None
        if owner in candidates:
            return candidates[owner]
        owner = (ancestor.owner_span_id or ancestor.parent_span_id) if ancestor is not None else ""
    return None


def handoff_sources(registry: Registry) -> dict[str, list[str]]:
    return {
        call.id: list(registry.extras.get(("AgentCall", call.id), {}).get(HANDOFF_SOURCES_PROP, []))
        for call in registry.all_of(AgentCall)
    }


def reconcile_relationships(registry: Registry) -> None:
    meta = registry.metadata_for
    signals = handoff_sources(registry)
    agents = registry.all_of(AgentCall)
    for cls, contains, execution_edge, uses in (
        (LLMCall, hasLLMCall, executesLLM, usesLLM),
        (ToolCall, hasToolCall, executesTool, usesTool),
    ):
        for child in registry.all_of(cls):
            parent = _owning_agent_call(registry, child, agents)
            if parent is None:
                application = registry.edge_target(executesSession, child.sessionId) or ""
                owner_agent = str(meta(child).get("ownerAgentId") or "")
                agent_id = (
                    _agent_id_for(registry, owner_agent, (application,)) if owner_agent else ""
                )
                target = registry.edge_target(execution_edge, child.id)
                if target and registry.get(Agent, agent_id) is not None:
                    registry.add_edge(uses(source_id=agent_id, target_id=target))
                continue
            registry.add_edge(contains(source_id=parent.id, target_id=child.id))
            agent = _own_agent_id(registry, parent)
            target = registry.edge_target(execution_edge, child.id)
            if agent and target:
                registry.add_edge(uses(source_id=agent, target_id=target))
    registry.remove_edges_where(
        lambda edge, source, _: edge in (usesTool, usesLLM) and registry.get(Agent, source) is None
    )
    for tool in registry.all_of(Tool):
        for owner in meta(tool).get("ownerSpanIds", []):
            for call in agents:
                agent = _own_agent_id(registry, call)
                if call.spanId == owner and agent:
                    registry.add_edge(usesTool(source_id=agent, target_id=tool.id))
    reconcile_graph_agents(registry)
    for state in registry.all_of(State):
        receiver = meta(state).get("receiverName")
        owner = meta(state).get("ownerSpanId")
        candidates = []
        for call in agents:
            agent = registry.get(Agent, _own_agent_id(registry, call) or "")
            if call.sessionId == state.sessionId and (
                (owner and call.spanId == owner)
                or (receiver and agent is not None and agent.name == receiver)
            ):
                candidates.append(call)
        if candidates:
            event_time = meta(state).get("eventTime", 0)
            call = min(
                candidates,
                key=lambda a: (
                    a.startTime < event_time,
                    abs(a.startTime - event_time),
                    a.id,
                ),
            )
            registry.remove_edges_where(
                lambda edge, _, target: edge is hasState and target == state.id
            )
            registry.add_edge(hasState(source_id=call.id, target_id=state.id))
    for session in registry.all_of(Session):
        for hint in meta(session).get("handoffs", []):
            source = next(
                (a for a in agents if a.spanId == hint["source"] and a.sessionId == session.id),
                None,
            )
            if source is None:
                continue
            targets = [
                a
                for a in agents
                if a.sessionId == session.id
                and a.id != source.id
                and (agent := registry.get(Agent, _own_agent_id(registry, a) or "")) is not None
                and agent.name == hint["target"]
            ]
            if targets:
                target = min(
                    targets,
                    key=lambda a: (
                        a.startTime < source.startTime,
                        abs(a.startTime - source.startTime),
                        a.id,
                    ),
                )
                signals[target.id] = sorted(set(signals.get(target.id, []) + [source.spanId]))
    for edge, source, target in list(registry.edges):
        if edge is hasAgentCall:
            registry.add_edge(belongsToMASCall(source_id=target, target_id=source))

    for call_id, sources in signals.items():
        if sources:
            registry.extras.setdefault(("AgentCall", call_id), {})[HANDOFF_SOURCES_PROP] = sources
    update_container_times(registry)


def strip_derived(registry: Registry) -> None:
    """Drop everything ``apply_heuristics`` derives, so it can be rebuilt."""
    # Session events own native states until complete child boundaries can replace them.
    containers = {
        n.id
        for cls, edge in ((Session, hasMASCall), (MASCall, hasAgentCall))
        for n in registry.all_of(cls)
        if registry.edges_from(edge, n.id)
        and not (
            cls is Session
            and registry.edge_target(hasInitialState, n.id)
            == _hash_id("state", n.id, "initial", "")
        )
    }
    bridges = {n.id for n in registry.all_of(ProcessingCall)}
    owners = containers | bridges
    transitions = {
        target
        for etype, source, target in registry.edges
        if etype is correspondsToTransition and source in owners
    }
    for bridge_id in bridges:
        registry.remove_node(ProcessingCall, bridge_id)
    for transition_id in transitions:
        registry.remove_node(Transition, transition_id)
    gone = bridges | transitions
    boundary_edges = (hasInitialState, hasFinalState, hasState)
    registry.remove_edges_where(
        lambda etype, source, target: (
            source in gone or target in gone or (etype in boundary_edges and source in containers)
        )
    )


def handoff_spans(registry: Registry) -> list[dict[str, Any]]:
    """Minimal ``*.agent`` spans carrying only the handoff signal, rebuilt from stored state."""
    signals = handoff_sources(registry)
    spans: list[dict[str, Any]] = []
    for call in registry.all_of(AgentCall):
        sources = signals.get(call.id)
        agent_id = registry.edge_target(executesAgent, call.id)
        if not sources or agent_id is None:
            continue
        spans.append(
            {
                "SpanName": "handoff.agent",
                "SpanId": call.spanId,
                "SpanAttributes": {
                    "agent_id": agent_id,
                    "ioa_observe.handoff.source.span_ids": json.dumps(sources),
                },
            }
        )
    return spans
