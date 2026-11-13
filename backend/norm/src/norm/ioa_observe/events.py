#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Event (OTel log record) dispatch, keyed on the ``event.name`` attribute."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from scope_ontology.models.edges import executesTool
from scope_ontology.models.nodes import AgentCall, LLMCall, ToolCall

from .fields import get_event_start_time, normalize_record
from .handlers import (
    handle_a2a_agent_card,
    handle_a2a_message_completed,
    handle_a2a_message_received,
    handle_a2a_message_sent,
    handle_llm_completed,
    handle_llm_started,
    handle_llm_tools_presented,
    handle_mcp_message_completed,
    handle_mcp_message_received,
    handle_mcp_message_sent,
    handle_mcp_tools_discovered,
    handle_tool_completed,
    handle_tool_started,
    handle_topology_edge_updated,
    handle_topology_node_completed,
    handle_topology_node_started,
    handle_topology_session_completed,
    handle_topology_session_started,
)
from .reconciliation import event_context, merge_span_context
from .registry import Registry
from .trajectory import (
    _clear_boundaries,
    _remember_trajectory,
    add_final_state_transition,
    add_initial_state,
)

logger = logging.getLogger(__name__)
_CALLS = (AgentCall, LLMCall, ToolCall)


def dispatch_event(event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry) -> bool:
    """Apply a lifecycle record without overwriting an authoritative completed span."""
    event, event_attrs = normalize_record({**event, "LogAttributes": event_attrs}, "event")
    context = event_context(event, event_attrs)
    if context is not None:
        key = (context.trace_id, context.span_id)
        registry.span_contexts[key] = merge_span_context(context, registry.span_contexts.get(key))
    return apply_event(event, event_attrs, registry)


def _dispatch_event(event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry) -> bool:
    """Run the case for the event's ``event.name``. False if the name is unknown."""
    match str(event_attrs.get("event.name") or ""):
        case "topology.session.started":
            handle_topology_session_started(event, event_attrs, registry)
        case "topology.session.completed":
            handle_topology_session_completed(event, event_attrs, registry)
        case "topology.node.started":
            handle_topology_node_started(event, event_attrs, registry)
        case "topology.node.completed":
            handle_topology_node_completed(event, event_attrs, registry)
        case "topology.edge.updated":
            handle_topology_edge_updated(event, event_attrs, registry)
        case "llm.started":
            handle_llm_started(event, event_attrs, registry)
        case "llm.completed":
            handle_llm_completed(event, event_attrs, registry)
        case "llm.tools.presented":
            handle_llm_tools_presented(event, event_attrs, registry)
        case "tool.started":
            handle_tool_started(event, event_attrs, registry)
        case "tool.completed":
            handle_tool_completed(event, event_attrs, registry)
        case "a2a.message.sent":
            handle_a2a_message_sent(event, event_attrs, registry)
        case "a2a.message.received":
            handle_a2a_message_received(event, event_attrs, registry)
        case "a2a.message.completed":
            handle_a2a_message_completed(event, event_attrs, registry)
        case "a2a.agent_card":
            handle_a2a_agent_card(event, event_attrs, registry)
        case "slim.message.sent":
            # TODO: handle slim.message.sent
            pass
        case "slim.message.received":
            # TODO: handle slim.message.received
            pass
        case "mcp.message.sent":
            handle_mcp_message_sent(event, event_attrs, registry)
        case "mcp.message.received":
            handle_mcp_message_received(event, event_attrs, registry)
        case "mcp.message.completed":
            handle_mcp_message_completed(event, event_attrs, registry)
        case "mcp.tools.discovered":
            handle_mcp_tools_discovered(event, event_attrs, registry)
        case "mcp.tools.published":
            # TODO: handle mcp.tools.published
            pass
        case _:
            return False
    return True


def apply_event(record: dict[str, Any], attrs: dict[str, Any], registry: Registry) -> bool:

    meta = registry.metadata_for
    name = str(attrs.get("event.name") or "")
    tool_call_id = str(attrs.get("tool.call.id") or attrs.get("message.id") or "")
    event_span_id = str(record.get("SpanId") or "")
    existing_tool = registry.find(ToolCall, lambda n: n.spanId == event_span_id)
    if (
        tool_call_id
        and existing_tool is not None
        and existing_tool.id != tool_call_id
        and registry.get(ToolCall, tool_call_id) is None
    ):
        old_metadata = deepcopy(meta(existing_tool))
        registry.merge_node_into(ToolCall, existing_tool.id, tool_call_id)
        renamed = existing_tool.model_copy(update={"id": tool_call_id})
        registry.add(renamed)
        meta(renamed).update(old_metadata)
    authoritative = [
        n.model_copy(deep=True)
        for cls in _CALLS
        for n in registry.all_of(cls)
        if meta(n).get("spanComplete")
    ]
    if not _dispatch_event(record, attrs, registry):
        logger.debug("Unsupported runtime event %s", name)
        return False
    for node in authoritative:
        registry.upsert(node)
    event_time = get_event_start_time(record, attrs)
    span_id = str(attrs.get("llm.call.id") or record.get("SpanId") or "")
    prefix = name.rsplit(".", 1)[0]
    cls = {
        "topology.node": AgentCall,
        "llm": LLMCall,
        "tool": ToolCall,
        "mcp.message": ToolCall,
    }.get(prefix)
    if cls is not None:
        call_id = str(attrs.get("tool.call.id") or attrs.get("message.id") or "")
        call = registry.get(cls, call_id) if call_id else None
        call = call or registry.find(cls, lambda n: n.spanId == span_id)
        if call is not None and not meta(call).get("spanComplete"):
            start = min(call.startTime, event_time)
            end = max(call.endTime, event_time)
            call_meta = meta(call)
            call_meta["spanIds"] = sorted(set(call_meta.get("spanIds", []) + [span_id]))
            priority = 2 if prefix != "mcp.message" else (0 if name.endswith(".received") else 1)
            previous = call_meta.get("spanPriority", -1)
            chosen_span = call.spanId
            if priority > previous or (priority == previous and span_id < chosen_span):
                chosen_span = span_id
                call_meta["spanPriority"] = priority
            call = call.model_copy(
                update={
                    "startTime": start,
                    "endTime": end,
                    "duration": (end - start) * 1000,
                    "spanId": chosen_span,
                }
            )
            if isinstance(call, ToolCall):
                tool = registry.edge_target(executesTool, call.id) or ""
                call = call.model_copy(update={"name": f"Tool call ({tool}) {chosen_span}"})
            registry.upsert(call)
            field_prefix = {
                "topology.node": "agent",
                "mcp.message": "message",
            }.get(prefix, prefix)
            priority = 1 if prefix == "mcp.message" else 2
            for field in ("input", "output"):
                key = f"{field_prefix}.{field}"
                if key in attrs and priority >= call_meta.get(f"{field}Priority", 0):
                    call_meta[field] = str(attrs[key] or "")
                    call_meta[f"{field}Priority"] = priority
            if name.endswith(".completed"):
                call_meta["complete"] = True
            _clear_boundaries(registry, call.id)
            input_text = call_meta.get("input", "")
            initial = add_initial_state(
                registry,
                call.id,
                call.sessionId,
                call.spanId,
                input_text,
            )
            if call_meta.get("complete"):
                add_final_state_transition(
                    registry,
                    call.id,
                    call.sessionId,
                    call.spanId,
                    initial.id,
                    input_text,
                    call_meta.get("output", ""),
                )
            _remember_trajectory(registry, call, meta)

    return True
