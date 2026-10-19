#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Handlers for the "topology.*" events (OTel log records)."""

from __future__ import annotations

from typing import Any

from oxp_ontology.models.edges import (
    belongsToMAS,
    executesAgent,
    executesTool,
    executesSession,
    hasAgentCall,
    hasFinalState,
    hasInitialState,
    hasToolCall,
    usesTool,
)
from oxp_ontology.models.nodes import MAS, Agent, AgentCall, Session, State, ToolCall

from ..fields import (
    _hash_id,
    get_event_application_id,
    get_event_session_id,
    get_event_start_time,
)
from ..registry import Registry
from ..trajectory import add_final_state_transition, add_initial_state
from .agent import _ensure_mas_call


def _ensure_session_and_mas(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> Session | None:
    """Ensure the Session and its MAS exist, linked by ``executesSession``, creating
    whichever is missing. None if the event has no session id."""
    session_id = get_event_session_id(event, event_attrs)
    application_id = get_event_application_id(event, event_attrs)
    if not session_id:
        return None
    start_time = get_event_start_time(event, event_attrs)

    session, _ = registry.add_if_not_exists(
        Session(
            id=session_id,
            name=f"Session {session_id}",
            sessionId=session_id,
            startTime=start_time,
            endTime=start_time,
            duration=0.0,
            spanId=str(event.get("SpanId") or event_attrs.get("span.id") or ""),
        )
    )
    if application_id:
        mas, _ = registry.add_if_not_exists(
            MAS(id=application_id, name=application_id, description=application_id)
        )
        registry.add_edge(executesSession(source_id=session.id, target_id=mas.id))
    return session


def handle_topology_session_started(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    _ensure_session_and_mas(event, event_attrs, registry)


def handle_topology_session_completed(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """Same nodes as session.started, then close the Session and give it its final
    State and Transition. The event carries no input/output, so the States have no
    content; the initial State is created here if there is none yet."""
    session = _ensure_session_and_mas(event, event_attrs, registry)
    if session is None:
        return
    end_time = get_event_start_time(event, event_attrs)
    if end_time > session.endTime:
        session.endTime = end_time  # type: ignore[attr-defined]
        session.duration = (end_time - session.startTime) * 1000.0  # type: ignore[attr-defined]

    # Keyed on the session id: the two events don't share a span id.
    initial_id = registry.edge_target(hasInitialState, session.id)
    if initial_id is None:
        initial_id = add_initial_state(registry, session.id, session.sessionId, session.id, "").id
    if registry.edge_target(hasFinalState, session.id) is None:
        add_final_state_transition(
            registry, session.id, session.sessionId, session.id, initial_id, "", ""
        )


def _ensure_agent_call(
    event: dict[str, Any],
    event_attrs: dict[str, Any],
    registry: Registry,
    span_id: str | None = None,
) -> tuple[AgentCall, str, str, float] | None:
    """Agent-side counterpart of handle_agent: make sure the MAS, Session, MASCall,
    Agent and AgentCall for this event's node exist, creating whichever are missing.

    ``span_id`` is the AgentCall's span (default: the event's own, as for ``node.*``
    events; ``llm.*``/``tool.*`` events pass their owning agent span).

    Returns ``(agent_call, session_id, span_id, event_time)``, or None if the event
    names no agent or session.
    """
    agent_id = str(event_attrs.get("agent.name") or "")
    session_id = get_event_session_id(event, event_attrs)
    if not agent_id or not session_id:
        return None
    application_id = get_event_application_id(event, event_attrs)
    if span_id is None:
        span_id = str(event.get("SpanId") or event_attrs.get("span.id") or "")
    event_time = get_event_start_time(event, event_attrs)

    agent, created = registry.add_if_not_exists(Agent(id=agent_id, name=agent_id, description=""))
    if created and application_id:
        registry.add_edge(belongsToMAS(source_id=agent.id, target_id=application_id))

    agent_call, _ = registry.add_if_not_exists(
        AgentCall(
            id=_hash_id("agentcall", agent_id, span_id),
            name=f"{agent_id} call {span_id}",
            sessionId=session_id,
            startTime=event_time,
            endTime=event_time,
            duration=0.0,
            spanId=span_id,
        )
    )
    registry.add_edge(executesAgent(source_id=agent_call.id, target_id=agent.id))
    if application_id:
        mas_call = _ensure_mas_call(registry, session_id, application_id, agent_call.startTime)
        registry.add_edge(hasAgentCall(source_id=mas_call.id, target_id=agent_call.id))
    # Tool events name only their owning agent *span*, so a ToolCall can arrive before its
    # AgentCall exists: attach the ones already waiting for this span.
    for tool_call in registry.all_of(ToolCall):
        if span_id and tool_call.parentSpanId == span_id:
            registry.add_edge(hasToolCall(source_id=agent_call.id, target_id=tool_call.id))
            tool_id = registry.edge_target(executesTool, tool_call.id)
            if tool_id:
                registry.add_edge(usesTool(source_id=agent.id, target_id=tool_id))
    return agent_call, session_id, span_id, event_time


def handle_topology_node_started(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """Ensure the agent's nodes exist and give the AgentCall its initial State
    (from ``agent.input``) -- once; if it already has one, it is left alone."""
    ensured = _ensure_agent_call(event, event_attrs, registry)
    if ensured is None:
        return
    agent_call, session_id, span_id, _ = ensured
    if registry.edge_target(hasInitialState, agent_call.id) is None:
        add_initial_state(
            registry, agent_call.id, session_id, span_id, str(event_attrs.get("agent.input") or "")
        )


def handle_topology_node_completed(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """Ensure the agent's nodes exist, close the AgentCall and give it its final
    State (from ``agent.output``) and Transition. The initial State is whatever
    ``node.started`` stored; only if there is none is one created, from ``agent.input``."""
    ensured = _ensure_agent_call(event, event_attrs, registry)
    if ensured is None:
        return
    agent_call, session_id, span_id, event_time = ensured

    if event_time > agent_call.endTime:
        agent_call.endTime = event_time  # type: ignore[attr-defined]
        agent_call.duration = (event_time - agent_call.startTime) * 1000.0  # type: ignore[attr-defined]

    # node.completed carries agent.input as well as agent.output. If it is ever missing,
    # the stored initial State (from node.started) supplies the input below.
    input_text = str(event_attrs.get("agent.input") or "")

    initial_id = registry.edge_target(hasInitialState, agent_call.id)
    if initial_id is None:
        initial_id = add_initial_state(registry, agent_call.id, session_id, span_id, input_text).id
    if registry.edge_target(hasFinalState, agent_call.id) is not None:
        return
    if not input_text:
        initial = registry.get(State, initial_id)
        stored = initial.content if initial is not None else ""
        input_text = "" if stored == "(no content recorded)" else stored
    add_final_state_transition(
        registry,
        agent_call.id,
        session_id,
        span_id,
        initial_id,
        input_text,
        str(event_attrs.get("agent.output") or ""),
    )
