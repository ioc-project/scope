#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Handlers for the "a2a.*" events (OTel log records)."""

from __future__ import annotations

import json
from typing import Any

from scope_ontology.models.edges import belongsToMAS, hasState
from scope_ontology.models.nodes import Agent, AgentCall, State

from ..fields import _hash_id, get_event_application_id, get_event_session_id, get_event_start_time
from ..registry import Registry
from .agent import _agent_id_for, _ensure_agent
from .topology import _ensure_agent_call, _ensure_session_and_mas


def _ensure_message_parties(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> tuple[AgentCall, str, str, str, str, str] | None:
    """What ``a2a.message.sent`` and ``a2a.message.received`` have in common: the sender's
    AgentCall (found by ``source.agent.span.id``) with everything above it -- MAS, Session,
    MASCall, Agent -- and the recipient Agent (``target.agent``), each created if missing.

    Returns ``(sender_call, session_id, source_span_id, target_name, message_id, content)``,
    or None if the event names no sender, recipient, sender span or session."""
    source_name = str(event_attrs.get("source.agent") or "")
    target_name = str(event_attrs.get("target.agent") or "")
    source_span_id = str(
        event_attrs.get("source.agent.span.id")
        or event_attrs.get("ioa_observe.agent.span_id")
        or ""
    )
    if not source_name or not target_name or not source_span_id:
        return None
    ensured = _ensure_agent_call(
        event, {**event_attrs, "agent.name": source_name}, registry, span_id=source_span_id
    )
    if ensured is None:
        return None
    agent_call, session_id, _, _ = ensured
    if registry.find(Agent, lambda agent: agent.name == target_name) is None:
        registry.add(Agent(id=target_name, name=target_name, description=""))
    message_id = str(event_attrs.get("message.id") or event_attrs.get("event.id") or "")
    content = str(event_attrs.get("message.input") or "")
    return agent_call, session_id, source_span_id, target_name, message_id, content


def handle_a2a_message_sent(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """Ensure the sender's call and recipient Agent, and retain the outgoing message
    as a State of the sender without changing its initial or final State."""
    parties = _ensure_message_parties(event, event_attrs, registry)
    if parties is None:
        return
    agent_call, session_id, source_span_id, target_name, message_id, content = parties
    if not content:
        return
    state = registry.add(
        State(
            id=_hash_id(
                "state",
                "a2a.message.sent",
                session_id,
                source_span_id,
                target_name,
                message_id,
                content,
            ),
            sessionId=session_id,
            content=content,
        )
    )
    registry.add_edge(hasState(source_id=agent_call.id, target_id=state.id))


def handle_a2a_message_received(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    _handle_a2a_message(event, event_attrs, registry)


def _agent_card(event_attrs: dict[str, Any]) -> dict[str, Any]:
    """The agent card in ``capability.content``, or {} if absent or malformed."""
    content = event_attrs.get("capability.content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return {}
    return content if isinstance(content, dict) else {}


def handle_a2a_agent_card(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """Add the Agent the card describes -- ``capability.content``'s ``name`` and
    ``description`` -- to the MAS and Session named by ``session.id``.

    Skipped without a ``session.id``. The MAS is the first part of ``session.id``
    (``<masname>_<sessionid>``) and the Session the rest; both are created if missing. The
    card has no span id of the described agent (its own ``ioa_observe.agent.span_id`` is the
    span of the agent that fetched it). An Agent is keyed on its name, so the card's Agent is
    the one the agent's own events later reuse. It is attached to the MAS with ``belongsToMAS``;
    one of that name already there is reused and only gets its description."""
    card = _agent_card(event_attrs)
    name = str(card.get("name") or "")
    mas_name = get_event_application_id(event, event_attrs)
    if not name or not mas_name:
        return
    if (
        _ensure_session_and_mas(event, {**event_attrs, "application_id": mas_name}, registry)
        is None
    ):
        return
    description = str(card.get("description") or "")
    agent = _ensure_agent(registry, _agent_id_for(registry, name, (mas_name,)), name, (mas_name,))
    if description and agent.description != description:
        agent.description = description  # type: ignore[attr-defined]
    registry.add_edge(belongsToMAS(source_id=agent.id, target_id=mas_name))
    # TODO: version  (card["version"])
    # TODO: supportedInterfaces  (how to reach the agent: url, protocolBinding, protocolVersion)
    # TODO: provider  (card["provider"], capability.provider.id)
    # TODO: capabilities  (card["capabilities"]: streaming, pushNotifications, ...)
    # TODO: defaultInputModes / defaultOutputModes
    # TODO: skills  (card["skills"])
    # TODO: securitySchemes / security


def handle_a2a_tool_started(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    """A ``tool.started`` of type ``a2a``: the agent whose span is
    ``ioa_observe.agent.span_id`` hands off to the agent named ``tool.name``.

    The target Agent is made sure to exist: an Agent of that name, or else an unattached
    one keyed on the name, which the target's own events later reuse (an Agent is keyed on
    its name)."""
    source_span_id = str(event_attrs.get("ioa_observe.agent.span_id") or "")
    target_name = str(event_attrs.get("tool.name") or "")
    if not source_span_id or not target_name:
        return
    if not any(agent.name == target_name for agent in registry.all_of(Agent)):
        registry.add(Agent(id=target_name, name=target_name, description=""))
    # TODO: record the handoff itself, from the Agent of the AgentCall of ``source_span_id`` to the
    # target. The ontology has no Agent -> Agent relationship; a handoff is otherwise a
    # bridge ProcessingCall between the two AgentCalls (heuristics/handoff.py), and the
    # target's AgentCall does not exist yet when this event arrives.


def _handle_a2a_message(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    record, attrs = event, event_attrs
    meta = registry.metadata_for
    name = str(attrs.get("event.name") or "")
    source = str(attrs.get("source.agent.span.id") or "")
    call = registry.find(AgentCall, lambda n: n.spanId == source)
    content = str(
        attrs.get("message.output" if name.endswith("completed") else "message.input") or ""
    )
    session_id = get_event_session_id(record, attrs)
    if session_id and content:
        _ensure_session_and_mas(record, attrs, registry)
        state = registry.add(
            State(
                id=_hash_id(
                    "state",
                    name,
                    session_id,
                    str(attrs.get("message.id") or ""),
                    content,
                ),
                sessionId=session_id,
                content=content,
            )
        )
        if name.endswith("received"):
            meta(state)["receiverName"] = str(attrs.get("target.agent") or "")
            meta(state)["eventTime"] = get_event_start_time(record, attrs)
        else:
            meta(state)["ownerSpanId"] = source
        if call is not None and name.endswith("completed"):
            registry.add_edge(hasState(source_id=call.id, target_id=state.id))
    return


def handle_a2a_message_completed(
    event: dict[str, Any], event_attrs: dict[str, Any], registry: Registry
) -> None:
    _handle_a2a_message(event, event_attrs, registry)
