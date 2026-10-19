#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Event (OTel log record) dispatch, keyed on the ``event.name`` attribute."""

from __future__ import annotations

from typing import Any

from .handlers import (
    handle_llm_completed,
    handle_llm_started,
    handle_tool_completed,
    handle_tool_started,
    handle_topology_node_completed,
    handle_topology_node_started,
    handle_topology_session_completed,
    handle_topology_session_started,
)
from .registry import Registry


def dispatch_event(event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry) -> bool:
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
            # TODO: handle topology.edge.updated
            pass
        case "llm.started":
            handle_llm_started(event, event_attrs, registry)
        case "llm.completed":
            handle_llm_completed(event, event_attrs, registry)
        case "tool.started":
            handle_tool_started(event, event_attrs, registry)
        case "tool.completed":
            handle_tool_completed(event, event_attrs, registry)
        case "a2a.message.sent":
            # TODO: handle a2a.message.sent
            pass
        case "a2a.message.received":
            # TODO: handle a2a.message.received
            pass
        case "a2a.agent_card":
            # TODO: handle a2a.agent_card
            pass
        case "slim.message.sent":
            # TODO: handle slim.message.sent
            pass
        case "slim.message.received":
            # TODO: handle slim.message.received
            pass
        case "mcp.message.sent":
            # TODO: handle mcp.message.sent
            pass
        case "mcp.message.received":
            # TODO: handle mcp.message.received
            pass
        case "mcp.tools.discovered":
            # TODO: handle mcp.tools.discovered
            pass
        case "mcp.tools.published":
            # TODO: handle mcp.tools.published
            pass
        case _:
            return False
    return True
