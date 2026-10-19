#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Unit tests for the top-level normalize()/Normalizer API."""

from __future__ import annotations

import json

from norm import Normalizer, StreamDelta, configure, normalize


def test_normalize_invokes_configured_callback() -> None:
    calls = []
    configure(on_normalized=lambda spans, nodes, edges: calls.append((spans, nodes, edges)))
    try:
        spans = [_agent_span()]
        nodes, edges = normalize(spans)
    finally:
        configure(on_normalized=None)
    assert calls == [(spans, nodes, edges)]


def _agent_span(session_id: str = "sess-1") -> dict:
    return {
        "Timestamp": "2026-07-09 12:00:00.000000000",
        "SpanId": "span-1",
        "ParentSpanId": "",
        "SpanName": "concierge.agent",
        "Duration": 1_000_000_000,
        "SpanAttributes": {
            "application_id": "app",
            "session.id": f"app_{session_id}",
            "agent_id": "concierge",
        },
    }


def test_normalize_returns_nodes_and_edges() -> None:
    nodes, edges = normalize([_agent_span()])
    assert any(node.get("node_type") == "AgentCall" for node in nodes)
    assert edges


def test_success_propagates_from_execution_success_attribute() -> None:
    span = _agent_span()
    span["SpanAttributes"]["execution.success"] = "true"
    nodes, _edges = normalize([span])
    agent_call = next(n for n in nodes if n["node_type"] == "AgentCall")
    assert agent_call["success"] is True


def test_success_defaults_true_when_span_carries_no_signal() -> None:
    nodes, _edges = normalize([_agent_span()])
    agent_call = next(n for n in nodes if n["node_type"] == "AgentCall")
    assert agent_call["success"] is True


def test_success_is_false_on_explicit_error_status_code() -> None:
    span = _agent_span()
    span["StatusCode"] = "Error"
    nodes, _edges = normalize([span])
    agent_call = next(n for n in nodes if n["node_type"] == "AgentCall")
    assert agent_call["success"] is False


def test_normalizer_process_file_matches_normalize(tmp_path) -> None:
    path = tmp_path / "spans.jsonl"
    path.write_text(json.dumps(_agent_span()) + "\n", encoding="utf-8")

    nodes, edges = Normalizer().process_file(path)
    expected_nodes, expected_edges = normalize([_agent_span()])
    assert len(nodes) == len(expected_nodes)
    assert len(edges) == len(expected_edges)


def test_normalizer_process_file_json_returns_flat_list(tmp_path) -> None:
    path = tmp_path / "spans.jsonl"
    path.write_text(json.dumps(_agent_span()) + "\n", encoding="utf-8")

    flat = Normalizer().process_file_json(path)
    nodes, edges = normalize([_agent_span()])
    assert len(flat) == len(nodes) + len(edges)


def test_stream_normalizer_converges_to_batch_result() -> None:
    from pathlib import Path

    from norm import InMemoryGraph, StreamNormalizer
    from norm.normalizer import load_json

    fixture = Path(__file__).resolve().parent / "fixtures" / "noa_trip_planner.otel_export.jsonl"
    spans = load_json(fixture)

    graph = InMemoryGraph()
    for span in spans:
        # A brand-new normalizer per span: nothing is kept in memory between spans.
        graph.apply(StreamNormalizer(graph).process(span))

    batch_nodes, batch_edges = normalize(spans)
    assert {i for _, i in graph.nodes} == {n["id"] for n in batch_nodes}
    assert set(graph.edges) == {(e["edge_type"], e["from_id"], e["to_id"]) for e in batch_edges}


def test_stream_normalizer_ignores_already_processed_span() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    graph.apply(StreamNormalizer(graph).process(_agent_span()))
    assert StreamNormalizer(graph).process(_agent_span()) == StreamDelta()


def test_stream_normalizer_kind_event_dispatches_by_name(capsys) -> None:
    import pytest

    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    delta = StreamNormalizer(graph).process(
        {"LogAttributes": {"event.name": "llm.started"}}, kind="event"
    )
    assert delta == StreamDelta()
    capsys.readouterr()
    assert not graph.nodes

    # "span" is the default and still normalizes the span
    delta = StreamNormalizer(graph).process(_agent_span())
    assert delta.nodes
    assert "llm.started" not in capsys.readouterr().out

    with pytest.raises(ValueError):
        StreamNormalizer(graph).process(_agent_span(), kind="metric")  # type: ignore[arg-type]


def _session_started_event() -> dict:
    return {
        "Timestamp": "2026-10-15 14:50:46.939986688",
        "SpanId": "211fdd62f03f330c",
        "ServiceName": "noa-trip-planner-mas",
        "Body": "topology.session.started",
        "LogAttributes": {
            "application_id": "noa-trip-planner-mas",
            "event.name": "topology.session.started",
            "event.time": "2026-10-15T14:50:46.938750+00:00",
            "session.id": "noa-trip-planner-mas_213dad2f",
            "snapshot.version": "1",
        },
    }


def test_topology_session_started_creates_session_and_mas() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    delta = normalizer.process(_session_started_event(), kind="event")
    graph.apply(delta)

    assert {(n["node_type"], n["id"]) for n in delta.nodes} == {
        ("Session", "213dad2f"),
        ("MAS", "noa-trip-planner-mas"),
    }
    session = graph.nodes[("Session", "213dad2f")]
    assert session["startTime"] == 1792075846.93875
    assert session["spanId"] == "211fdd62f03f330c"
    assert [(e["edge_type"], e["from_id"], e["to_id"]) for e in delta.edges] == [
        ("executesSession", "213dad2f", "noa-trip-planner-mas")
    ]

    # re-delivery changes nothing
    assert normalizer.process(_session_started_event(), kind="event") == StreamDelta()


def test_topology_session_started_without_session_id_is_noop() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    event = _session_started_event()
    del event["LogAttributes"]["session.id"]
    assert StreamNormalizer(InMemoryGraph()).process(event, kind="event") == StreamDelta()



def _node_event(name: str, **extra: str) -> dict:
    return {
        "Timestamp": "2026-10-15 14:50:46.959707000",
        "SpanId": "427bddeaf7b098f0",
        "ServiceName": "noa-trip-planner-mas",
        "LogAttributes": {
            "application_id": "noa-trip-planner-mas",
            "event.name": name,
            "event.time": "2026-10-15T14:50:46.959707+00:00" if name.endswith("started") else "2026-10-15T14:50:48.500603+00:00",
            "session.id": "noa-trip-planner-mas_213dad2f",
            "agent.name": "moderator",
            **extra,
        },
    }


def test_topology_node_events_build_agent_call_states_and_transition() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    for event in (
        _node_event("topology.node.started", **{"agent.input": "hello"}),
        _node_event("topology.node.completed", **{"agent.output": "bye"}),
    ):
        graph.apply(normalizer.process(event, kind="event"))

    types = sorted(t for t, _ in graph.nodes)
    assert types == ["Agent", "AgentCall", "MAS", "MASCall", "Session", "State", "State", "Transition"]
    contents = sorted(n["content"] for (t, _), n in graph.nodes.items() if t == "State")
    assert contents == ["bye", "hello"]
    edge_types = {e["edge_type"] for e in graph.edges.values()}
    assert {"hasInitialState", "hasFinalState", "inputTo", "leadsTo", "executesAgent", "hasAgentCall"} <= edge_types

    call = next(n for (t, _), n in graph.nodes.items() if t == "AgentCall")
    assert call["endTime"] > call["startTime"]
    assert call["duration"] > 0

    # re-delivery changes nothing
    assert normalizer.process(_node_event("topology.node.completed", **{"agent.output": "bye"}), kind="event") == StreamDelta()


def test_topology_node_completed_alone_creates_everything() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    delta = StreamNormalizer(graph).process(
        _node_event("topology.node.completed", **{"agent.output": "bye"}), kind="event"
    )
    graph.apply(delta)
    states = sorted(n["content"] for (t, _), n in graph.nodes.items() if t == "State")
    assert states == ["(no content recorded)", "bye"]
    assert ("Transition", next(i for t, i in graph.nodes if t == "Transition")) in graph.nodes


def test_topology_session_completed_creates_missing_nodes_and_final_state() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    started = _session_started_event()
    completed = {
        "Timestamp": "2026-10-15 14:56:13.324206336",
        "ServiceName": "noa-trip-planner-mas",
        "LogAttributes": {
            **started["LogAttributes"],
            "event.name": "topology.session.completed",
            "event.time": "2026-10-15T14:56:13.322348+00:00",
        },
    }
    # completed alone creates everything started does, plus the states/transition
    graph.apply(normalizer.process(completed, kind="event"))
    types = sorted(t for t, _ in graph.nodes)
    assert types == ["MAS", "Session", "State", "State", "Transition"]
    edge_types = {e["edge_type"] for e in graph.edges.values()}
    assert {"executesSession", "hasInitialState", "hasFinalState", "inputTo", "leadsTo"} <= edge_types
    assert normalizer.process(completed, kind="event") == StreamDelta()

    # started afterwards adds nothing; started-then-completed ends the session later
    assert normalizer.process(started, kind="event") == StreamDelta()
    graph2 = InMemoryGraph()
    n2 = StreamNormalizer(graph2)
    graph2.apply(n2.process(started, kind="event"))
    graph2.apply(n2.process(completed, kind="event"))
    session = graph2.nodes[("Session", "213dad2f")]
    assert session["endTime"] > session["startTime"] and session["duration"] > 0
    assert sorted(t for t, _ in graph2.nodes) == types



def test_topology_node_completed_uses_agent_input_when_present() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    graph.apply(
        StreamNormalizer(graph).process(
            _node_event("topology.node.completed", **{"agent.input": "hello", "agent.output": "bye"}),
            kind="event",
        )
    )
    states = sorted(n["content"] for (t, _), n in graph.nodes.items() if t == "State")
    assert states == ["bye", "hello"]


def test_llm_started_creates_chain_down_to_llm_call() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    event = {
        "Timestamp": "2026-10-15 14:50:48.493184000",
        "SpanId": "0fafd3f43fd76292",
        "ServiceName": "noa-trip-planner-mas",
        "LogAttributes": {
            "application_id": "noa-trip-planner-mas",
            "event.name": "llm.started",
            "event.time": "2026-10-15T14:50:48.493184+00:00",
            "session.id": "noa-trip-planner-mas_213dad2f",
            "agent.name": "moderator",
            "ioa_observe.agent.span_id": "427bddeaf7b098f0",
            "llm.call.id": "0fafd3f43fd76292",
            "llm.name": "azure/gpt-4o",
            "llm.input": "hi",
        },
    }
    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    graph.apply(normalizer.process(event, kind="event"))

    assert sorted(t for t, _ in graph.nodes) == [
        "Agent", "AgentCall", "LLM", "LLMCall", "MAS", "MASCall", "Session", "State",
    ]
    assert graph.nodes[("LLM", "llm-azure-gpt-4o")]["provider"] == "azure"
    agent_call = next(n for (t, _), n in graph.nodes.items() if t == "AgentCall")
    assert agent_call["spanId"] == "427bddeaf7b098f0"
    edge_types = {e["edge_type"] for e in graph.edges.values()}
    assert {"usesLLM", "executesLLM", "hasLLMCall", "hasAgentCall", "hasInitialState"} <= edge_types
    assert normalizer.process(event, kind="event") == StreamDelta()


def _llm_event(name: str, **extra: str) -> dict:
    started = name.endswith("started")
    return {
        "SpanId": "0fafd3f43fd76292",
        "ServiceName": "noa-trip-planner-mas",
        "LogAttributes": {
            "application_id": "noa-trip-planner-mas",
            "event.name": name,
            "event.time": "2026-10-15T14:50:48.493184+00:00" if started else "2026-10-15T14:50:48.496038+00:00",
            "session.id": "noa-trip-planner-mas_213dad2f",
            "agent.name": "moderator",
            "ioa_observe.agent.span_id": "427bddeaf7b098f0",
            "llm.call.id": "0fafd3f43fd76292",
            "llm.name": "azure/gpt-4o",
            "llm.input": "hi",
            **extra,
        },
    }


def test_llm_started_then_completed_builds_states_and_transition() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    completed = _llm_event("llm.completed", **{"llm.output": "yo"})
    for event in (_llm_event("llm.started"), completed):
        graph.apply(normalizer.process(event, kind="event"))

    assert sorted(t for t, _ in graph.nodes) == [
        "Agent", "AgentCall", "LLM", "LLMCall", "MAS", "MASCall", "Session", "State", "State", "Transition",
    ]
    assert sorted(n["content"] for (t, _), n in graph.nodes.items() if t == "State") == ["hi", "yo"]
    call = next(n for (t, _), n in graph.nodes.items() if t == "LLMCall")
    assert call["endTime"] > call["startTime"]
    edge_types = {e["edge_type"] for e in graph.edges.values()}
    assert {"hasFinalState", "inputTo", "leadsTo", "correspondsToTransition"} <= edge_types
    assert normalizer.process(completed, kind="event") == StreamDelta()


def test_llm_completed_alone_creates_everything() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    graph.apply(
        StreamNormalizer(graph).process(_llm_event("llm.completed", **{"llm.output": "yo"}), kind="event")
    )
    assert {"LLM", "LLMCall", "AgentCall", "Transition"} <= {t for t, _ in graph.nodes}


def _tool_event(name: str, **extra: str) -> dict:
    return {
        "SpanId": "abb8ef27ca74034a",
        "ServiceName": "noa-trip-planner-mas",
        "LogAttributes": {
            "application_id": "noa-trip-planner-mas",
            "event.name": name,
            "event.time": "2026-10-15T14:50:49.664363+00:00" if name.endswith("started") else "2026-10-15T14:50:49.666468+00:00",
            "session.id": "noa-trip-planner-mas_213dad2f",
            "ioa_observe.agent.span_id": "5371d7b3072ac5c7",
            "tool.name": "read_document",
            "tool.input": "in",
            **extra,
        },
    }


def _edges(graph) -> set[tuple[str, str, str]]:
    return {(e["edge_type"], e["from_id"], e["to_id"]) for e in graph.edges.values()}


def test_tool_events_link_to_existing_agent_call() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    node_started = _node_event("topology.node.started", **{"agent.name": "schedule_agent", "agent.input": "q"})
    node_started["SpanId"] = "5371d7b3072ac5c7"
    completed = _tool_event("tool.completed", **{"tool.output": "out", "tool.status": "success"})
    for event in (node_started, _tool_event("tool.started"), completed):
        graph.apply(normalizer.process(event, kind="event"))

    assert {"Tool", "ToolCall", "AgentCall", "Agent"} <= {t for t, _ in graph.nodes}
    edges = _edges(graph)
    assert ("usesTool", "schedule_agent", "read_document") in edges
    assert any(e[0] == "hasToolCall" for e in edges) and any(e[0] == "executesTool" for e in edges)
    call = next(n for (t, _), n in graph.nodes.items() if t == "ToolCall")
    assert call["success"] is True and call["endTime"] > call["startTime"]
    assert sorted(n["content"] for (t, _), n in graph.nodes.items() if t == "State" and n["content"] in ("in", "out")) == ["in", "out"]
    assert normalizer.process(completed, kind="event") == StreamDelta()


def test_tool_event_before_agent_is_linked_when_agent_arrives() -> None:
    from norm import InMemoryGraph, StreamNormalizer

    graph = InMemoryGraph()
    normalizer = StreamNormalizer(graph)
    graph.apply(normalizer.process(_tool_event("tool.started"), kind="event"))
    assert not {t for t, _ in graph.nodes} & {"Agent", "AgentCall"}
    assert not any(e[0] in ("hasToolCall", "usesTool") for e in _edges(graph))

    node_started = _node_event("topology.node.started", **{"agent.name": "schedule_agent", "agent.input": "q"})
    node_started["SpanId"] = "5371d7b3072ac5c7"
    graph.apply(normalizer.process(node_started, kind="event"))
    edges = _edges(graph)
    assert ("usesTool", "schedule_agent", "read_document") in edges
    assert any(e[0] == "hasToolCall" for e in edges)
