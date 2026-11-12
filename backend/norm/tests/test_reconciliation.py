# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

import itertools
import json
import random

import pytest

from norm import InMemoryGraph, StreamDelta, StreamNormalizer, normalize


def process_record(graph, record, kind):
    delta = StreamNormalizer(graph).process(record, kind)
    graph.apply(delta)
    return delta


def span(name, span_id, **attrs):
    return {
        "SpanName": name,
        "SpanId": span_id,
        "Timestamp": "2026-11-10 00:00:00.000000000",
        "Duration": 2_000_000_000,
        "SpanAttributes": {"application_id": "app", "session.id": "app_s1", **attrs},
    }


def event(name, span_id="", time="2026-11-10T00:00:01+00:00", service="app", **attrs):
    return {
        "ServiceName": service,
        "SpanId": span_id,
        "LogAttributes": {
            "event.name": name,
            "event.time": time,
            "application_id": service,
            "session.id": "app_s1",
            **attrs,
        },
    }


def replay(records):
    graph = InMemoryGraph()
    for record, kind in records:
        process_record(graph, record, kind)
    return graph


def nodes(graph, kind):
    return [n for (t, _), n in graph.nodes.items() if t == kind]


def stored_copy(graph):
    restored = InMemoryGraph()
    restored.nodes = {key: json.loads(json.dumps(value)) for key, value in graph.nodes.items()}
    restored.edges = {key: json.loads(json.dumps(value)) for key, value in graph.edges.items()}
    restored.span_contexts = dict(graph.span_contexts)
    return restored


@pytest.mark.parametrize("explicit_owner", [False, True])
def test_mcp_transport_ancestry_backfills_tool_ownership_in_every_order(explicit_owner):
    agent = span("planner.agent", "agent", agent_id="planner")
    client = span(
        "MCP send tools/call search",
        "client",
        **({"ioa_observe.agent.span_id": "agent"} if explicit_owner else {}),
    )
    client["ParentSpanId"] = "framework" if explicit_owner else "agent"
    server = span("tools/call search", "server")
    server["SpanAttributes"] = {}
    server["ParentSpanId"] = "client"
    tool = span(
        "search.tool",
        "tool",
        **{"application_id": "tool-service", "ioa_observe.entity.name": "search"},
    )
    tool["ParentSpanId"] = "server"
    records = [agent, client, server, tool]
    for record in records:
        record["TraceId"] = "trace"
    expected_nodes, expected_edges = normalize(records)
    expected_nodes = {(n["node_type"], n["id"]): n for n in expected_nodes}
    expected_edges = {(e["edge_type"], e["from_id"], e["to_id"]): e for e in expected_edges}
    for order in itertools.permutations(records):
        graph = InMemoryGraph()
        for record in order:
            process_record(graph, record, "span")
            graph = stored_copy(graph)
        call = nodes(graph, "ToolCall")[0]
        owner = nodes(graph, "AgentCall")[0]
        assert ("hasToolCall", owner["id"], call["id"]) in graph.edges
        assert ("usesTool", "planner", "search") in graph.edges
        assert [mas["id"] for mas in nodes(graph, "MAS")] == ["app"]
        assert graph.nodes == expected_nodes
        assert {
            key: {k: v for k, v in edge.items() if k not in ("from_type", "to_type")}
            for key, edge in graph.edges.items()
        } == expected_edges
        for record in records:
            assert process_record(graph, record, "span") == StreamDelta()


def test_transport_ancestry_does_not_cross_trace_or_session():
    agent = span("planner.agent", "agent", agent_id="planner")
    agent["TraceId"] = "trace"
    client = span("tools/call.mcp", "client", **{"ioa_observe.agent.span_id": "agent"})
    client["TraceId"] = "other-trace"
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool.update(TraceId="trace", ParentSpanId="client")
    graph = replay((record, "span") for record in (agent, client, tool))
    assert not any(edge == "hasToolCall" for edge, _, _ in graph.edges)
    client["TraceId"] = "trace"
    client["SpanAttributes"]["session.id"] = "app_other"
    process_record(graph, client, "span")
    assert not any(edge == "hasToolCall" for edge, _, _ in graph.edges)
    client["SpanAttributes"]["session.id"] = "app_s1"
    process_record(graph, client, "span")
    owner = nodes(graph, "AgentCall")[0]
    call = nodes(graph, "ToolCall")[0]
    assert ("hasToolCall", owner["id"], call["id"]) in graph.edges


def test_transport_ancestry_cycle_remains_unowned():
    first = span("tools/call.mcp", "one")
    first["ParentSpanId"] = "two"
    second = span("tools/call search", "two")
    second["ParentSpanId"] = "one"
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool["ParentSpanId"] = "one"
    graph = replay((record, "span") for record in (tool, first, second))
    assert len(nodes(graph, "ToolCall")) == 1
    assert not any(edge == "hasToolCall" for edge, _, _ in graph.edges)


def test_sessionless_transport_is_private_context_not_an_execution():
    record = {
        "TraceId": "trace",
        "SpanId": "server",
        "ParentSpanId": "client",
        "SpanName": "tools/call search",
        "ServiceName": "tool-service",
        "SpanAttributes": {},
    }
    graph = InMemoryGraph()
    delta = process_record(graph, record, "span")
    assert not delta.nodes and not delta.edges
    assert not graph.nodes and not graph.edges
    assert len(delta.span_contexts) == 1
    graph = stored_copy(graph)
    assert graph.load_span_contexts("trace")[0].session_id == ""
    assert process_record(graph, record, "span") == StreamDelta()


@pytest.mark.parametrize("include_transport_span", [False, True])
@pytest.mark.parametrize("include_message_input", [False, True])
def test_mcp_received_event_supplies_tool_parent_ownership_in_every_order(
    include_transport_span, include_message_input
):
    agent = span("planner.agent", "agent", agent_id="planner")
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool["ParentSpanId"] = "server"
    received = event(
        "mcp.message.received",
        "server",
        **{
            "source.agent": "planner",
            "source.agent.span.id": "agent",
            "source.agent.trace.id": "trace",
            "operation.name": "tools/call",
            "message.id": "request",
            "message.input": '{"name": "search", "arguments": {}}',
        },
    )
    if not include_message_input:
        received["LogAttributes"].pop("message.input")
    records = [(agent, "span"), (tool, "span"), (received, "event")]
    if include_transport_span:
        server = span("tools/call search", "server")
        server["SpanAttributes"] = {}
        server["ParentSpanId"] = "client"
        records.append((server, "span"))
    for record, _ in records:
        record["TraceId"] = "trace"
    for order in itertools.permutations(records):
        graph = InMemoryGraph()
        for record, kind in order:
            process_record(graph, record, kind)
            graph = stored_copy(graph)
        owner = nodes(graph, "AgentCall")[0]
        assert len(nodes(graph, "ToolCall")) == (2 if include_message_input else 1)
        for call in nodes(graph, "ToolCall"):
            assert ("hasToolCall", owner["id"], call["id"]) in graph.edges
        context = graph.span_contexts[("trace", "server")]
        assert context.owner_span_id == "agent"
        assert context.parent_span_id == ("client" if include_transport_span else "")
        for record, kind in records:
            assert process_record(graph, record, kind) == StreamDelta()
        graph.span_contexts.clear()
        for key in list(graph.edges):
            if key[0] == "hasToolCall":
                del graph.edges[key]
        process_record(graph, received, "event")
        for call in nodes(graph, "ToolCall"):
            assert ("hasToolCall", owner["id"], call["id"]) in graph.edges


def test_mcp_received_event_does_not_infer_an_owner_in_another_trace():
    agent = span("planner.agent", "agent", agent_id="planner")
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool["ParentSpanId"] = "server"
    received = event(
        "mcp.message.received",
        "server",
        **{
            "source.agent": "planner",
            "source.agent.span.id": "agent",
            "source.agent.trace.id": "other-trace",
            "message.id": "request",
            "message.input": '{"name": "search", "arguments": {}}',
        },
    )
    for record in (agent, tool, received):
        record["TraceId"] = "trace"
    graph = replay([(agent, "span"), (received, "event"), (tool, "span")])
    call = next(n for n in nodes(graph, "ToolCall") if n["spanId"] == "tool")
    assert not any(
        edge == "hasToolCall" and target == call["id"] for edge, _, target in graph.edges
    )


def test_explicit_transport_owner_takes_precedence_over_outer_agent_parent():
    inner = span("inner.agent", "inner", agent_id="inner")
    outer = span("outer.agent", "outer", agent_id="outer")
    client = span("tools/call.mcp", "client", **{"ioa_observe.agent.span_id": "inner"})
    client["ParentSpanId"] = "outer"
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool["ParentSpanId"] = "client"
    for order in ((tool, client, outer, inner), (outer, inner, client, tool)):
        graph = replay((record, "span") for record in order)
        call = nodes(graph, "ToolCall")[0]
        parents = [
            source
            for edge, source, target in graph.edges
            if edge == "hasToolCall" and target == call["id"]
        ]
        assert len(parents) == 1
        assert graph.nodes[("AgentCall", parents[0])]["spanId"] == "inner"


def test_replaying_legacy_completed_spans_upgrades_missing_ancestry():
    agent = span("planner.agent", "agent", agent_id="planner")
    tool = span("search.tool", "tool", **{"ioa_observe.entity.name": "search"})
    tool["ParentSpanId"] = "server"
    server = span(
        "MCP server tools/call search", "server", **{"ioa_observe.agent.span_id": "agent"}
    )
    for record in (agent, tool, server):
        record["TraceId"] = "trace"
    graph = replay((record, "span") for record in (tool, agent))
    graph.span_contexts.clear()
    for node in graph.nodes.values():
        node.get("rtNormalization", {}).pop("traceId", None)
    graph = stored_copy(graph)
    for record in (tool, agent, server):
        process_record(graph, record, "span")
    call = nodes(graph, "ToolCall")[0]
    owner = nodes(graph, "AgentCall")[0]
    assert call["rtNormalization"]["traceId"] == "trace"
    assert ("hasToolCall", owner["id"], call["id"]) in graph.edges
    assert ("usesTool", "planner", "search") in graph.edges


def test_session_completion_spans_keep_latest_end_and_redeliver_without_changes():
    start = span("session.start", "start", ioa_start_time=1000)
    latest = span("session.end", "latest", **{"session.ended_at": 1010})
    early = span("session.end", "early", **{"session.ended_at": 1005})
    for endings in ((latest, early), (early, latest)):
        graph = replay((record, "span") for record in (start, *endings))
        assert nodes(graph, "Session")[0]["endTime"] == 1010
        for record in (latest, early):
            assert process_record(graph, record, "span") == StreamDelta()


def test_agent_and_tool_with_same_id_keep_distinct_capability_relationships():
    agent = span("schedule.agent", "agent", agent_id="schedule")
    tool = span(
        "schedule.tool",
        "tool",
        **{"ioa_observe.entity.name": "schedule", "ioa_observe.agent.span_id": "agent"},
    )
    graph = replay((record, "span") for record in (tool, agent))
    process_record(graph, span("execute_tool schedule", "transport"), "span")
    assert ("Agent", "schedule") in graph.nodes
    assert ("Tool", "schedule") in graph.nodes
    assert ("usesTool", "schedule", "schedule") in graph.edges
    edge = graph.edges[("usesTool", "schedule", "schedule")]
    assert (edge["from_type"], edge["to_type"]) == ("Agent", "Tool")
    owner = nodes(graph, "AgentCall")[0]
    call = nodes(graph, "ToolCall")[0]
    assert graph.edges[("executes", owner["id"], "schedule")]["to_type"] == "Agent"
    assert graph.edges[("executes", call["id"], "schedule")]["to_type"] == "Tool"


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (3, 2, 1, 0), (2, 0, 3, 1)])
def test_batch_and_stream_share_full_graph_payloads(order):
    records = [
        span("planner.agent", "agent", agent_id="planner"),
        span(
            "model.chat",
            "chat",
            **{
                "ioa_observe.agent.span_id": "agent",
                "gen_ai.provider.name": "test",
                "gen_ai.request.model": "model",
            },
        ),
        span(
            "search.tool",
            "tool",
            **{
                "ioa_observe.agent.span_id": "agent",
                "ioa_observe.entity.name": "search",
            },
        ),
        span(
            "workflow.graph",
            "graph",
            **{
                "gen_ai.ioa.graph": json.dumps(
                    {"nodes": {"planner": {}, "router": {}, "search": {}}}
                )
            },
        ),
    ]
    ordered = [records[index] for index in order]
    batch_nodes, batch_edges = normalize(ordered)
    graph = replay((record, "span") for record in ordered)
    assert graph.nodes == {(node["node_type"], node["id"]): node for node in batch_nodes}
    assert {
        key: {k: v for k, v in edge.items() if k not in ("from_type", "to_type")}
        for key, edge in graph.edges.items()
    } == {(edge["edge_type"], edge["from_id"], edge["to_id"]): edge for edge in batch_edges}
    assert [agent["id"] for agent in nodes(graph, "Agent")] == ["planner"]


def test_legacy_worker_metadata_survives_reload_and_completed_span_enrichment():
    started = event(
        "tool.started",
        "tool",
        **{
            "tool.name": "search",
            "tool.call.id": "natural-id",
            "ioa_observe.agent.span_id": "agent",
            "tool.input": "provisional",
        },
    )
    graph = stored_copy(replay([(started, "event")]))
    completed = span(
        "search.tool",
        "tool",
        **{
            "gen_ai.tool.call.id": "natural-id",
            "ioa_observe.entity.name": "search",
            "ioa_observe.entity.input": "question",
            "ioa_observe.entity.output": "answer",
            "ioa_observe.agent.span_id": "agent",
        },
    )
    process_record(graph, completed, "span")
    graph = stored_copy(graph)
    process_record(graph, span("planner.agent", "agent", agent_id="planner"), "span")
    call = nodes(graph, "ToolCall")[0]
    assert call["id"] == "natural-id"
    assert call["rtNormalization"]["spanComplete"] is True
    assert "inputPriority" not in call["rtNormalization"]
    assert len(nodes(graph, "ToolCall")) == 1
    agent = nodes(graph, "AgentCall")[0]
    assert ("hasToolCall", agent["id"], call["id"]) in graph.edges
    assert process_record(graph, completed, "span") == StreamDelta()


def test_legacy_handoff_property_is_preserved_without_new_metadata():
    target = span(
        "target.agent",
        "target-span",
        **{
            "agent_id": "target",
            "ioa_start_time": 1001,
            "ioa_observe.entity.input": "request",
            "ioa_observe.entity.output": "reply",
            "ioa_observe.handoff.source.span_ids": json.dumps(["source-span"]),
        },
    )
    graph = stored_copy(replay([(target, "span")]))
    for node in graph.nodes.values():
        node.pop("rtNormalization", None)
    process_record(
        graph,
        span(
            "source.agent",
            "source-span",
            **{
                "agent_id": "source",
                "ioa_start_time": 1000,
                "ioa_observe.entity.output": "handoff",
            },
        ),
        "span",
    )
    call = next(n for n in nodes(graph, "AgentCall") if n["spanId"] == "target-span")
    assert call["handoffSourceSpanIds"] == ["source-span"]
    assert len(nodes(graph, "ProcessingCall")) == 1
    assert len(nodes(graph, "AgentCall")) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_cards_and_discovered_tools_preserve_application_names_with_underscores(reverse):
    records = [
        event(
            "a2a.agent_card",
            "card",
            service="tool_service",
            **{
                "session.id": "origin_mas_s1",
                "capability.content": json.dumps({"name": "Planner"}),
            },
        ),
        event(
            "mcp.tools.discovered",
            "discovery",
            service="tool_service",
            **{
                "session.id": "origin_mas_s1",
                "ioa_observe.agent.span_id": "agent",
                "capability.content": json.dumps({"tools": [{"name": "search"}]}),
            },
        ),
        event(
            "topology.node.started",
            "agent",
            service="origin_mas",
            **{"session.id": "origin_mas_s1", "agent.name": "Planner"},
        ),
    ]
    graph = replay((record, "event") for record in (reversed(records) if reverse else records))
    assert [mas["id"] for mas in nodes(graph, "MAS")] == ["origin_mas"]
    assert [session["id"] for session in nodes(graph, "Session")] == ["s1"]
    assert ("usesTool", "Planner", "search") in graph.edges
    assert ("belongsToMAS", "Planner", "origin_mas") in graph.edges


def test_container_final_state_follows_last_ending_child():
    long = span(
        "long.agent",
        "long",
        **{"agent_id": "long", "ioa_start_time": 1000, "ioa_observe.entity.output": "last"},
    )
    long["Duration"] = 10_000_000_000
    short = span(
        "short.agent",
        "short",
        **{"agent_id": "short", "ioa_start_time": 1001, "ioa_observe.entity.output": "first"},
    )
    for records in ((long, short), (short, long)):
        graph = replay((record, "span") for record in records)
        for cls in ("Session", "MASCall"):
            container = nodes(graph, cls)[0]
            assert container["startTime"] == 1000
            assert container["endTime"] == 1010
            final = next(
                edge["to_id"]
                for edge in graph.edges.values()
                if edge["edge_type"] == "hasFinalState" and edge["from_id"] == container["id"]
            )
            assert graph.nodes[("State", final)]["content"] == "last"


def test_invalid_graph_node_shape_fails_in_batch_and_stream():
    record = span("workflow.graph", "graph", **{"gen_ai.ioa.graph": '{"nodes":[]}'})
    with pytest.raises(ValueError, match="nodes must be an object"):
        normalize([record])
    with pytest.raises(ValueError, match="nodes must be an object"):
        StreamNormalizer(InMemoryGraph()).process(record)


def test_native_session_states_survive_until_child_boundaries_are_usable():
    graph = replay([(event("topology.session.started"), "event")])
    initial = next(
        edge["to_id"]
        for edge in graph.edges.values()
        if edge["edge_type"] == "hasInitialState" and edge["from_id"] == "s1"
    )
    process_record(
        graph, event("topology.node.started", "agent", **{"agent.name": "planner"}), "event"
    )
    assert ("hasInitialState", "s1", initial) in graph.edges
    process_record(
        graph,
        event("topology.session.completed", time="2026-11-10T00:00:04+00:00"),
        "event",
    )
    assert any(
        edge["edge_type"] == "hasFinalState" and edge["from_id"] == "s1"
        for edge in graph.edges.values()
    )
    process_record(
        graph,
        event(
            "topology.node.completed",
            "agent",
            time="2026-11-10T00:00:03+00:00",
            **{"agent.name": "planner", "agent.output": "done"},
        ),
        "event",
    )
    final = next(
        edge["to_id"]
        for edge in graph.edges.values()
        if edge["edge_type"] == "hasFinalState" and edge["from_id"] == "s1"
    )
    assert graph.nodes[("State", final)]["content"] == "done"


def test_pending_handoff_metadata_is_independent_of_arrival_order():
    records = [
        event(
            "topology.edge.updated",
            "edge",
            **{"source.agent.span.id": source, "target.agent": target},
        )
        for source, target in (("one", "two"), ("two", "three"))
    ]
    first = replay((record, "event") for record in records)
    second = replay((record, "event") for record in reversed(records))
    assert first.nodes == second.nodes
    assert first.edges == second.edges
    assert not nodes(first, "Agent")


def test_capability_ownership_is_independent_of_span_arrival_order():
    records = [
        span("planner.agent", "agent", agent_id="planner"),
        span(
            "model.chat",
            "chat",
            **{
                "agent_id": "planner",
                "ioa_observe.agent.span_id": "agent",
                "gen_ai.provider.name": "test",
                "gen_ai.request.model": "model",
            },
        ),
        span(
            "search.tool",
            "tool",
            **{
                "agent_id": "planner",
                "ioa_observe.agent.span_id": "agent",
                "ioa_observe.entity.name": "search",
            },
        ),
    ]
    for order in itertools.permutations(records):
        graph = replay((r, "span") for r in order)
        agent = nodes(graph, "AgentCall")[0]
        for node_type, edge_type in (
            ("LLMCall", "hasLLMCall"),
            ("ToolCall", "hasToolCall"),
        ):
            assert (
                edge_type,
                agent["id"],
                nodes(graph, node_type)[0]["id"],
            ) in graph.edges
        assert process_record(graph, order[-1], "span") == StreamDelta()


@pytest.mark.parametrize("event_first", [False, True])
def test_agent_events_and_spans_share_one_complete_call(event_first):
    started = event("topology.node.started", "agent", **{"agent.name": "Planner"})
    completed = span(
        "planner.agent",
        "agent",
        **{
            "agent_id": "planner-id",
            "ioa_observe.entity.name": "Planner",
            "ioa_observe.entity.input": "question",
            "ioa_observe.entity.output": "answer",
        },
    )
    records = [(started, "event"), (completed, "span")]
    graph = replay(records if event_first else reversed(records))
    assert len(nodes(graph, "Agent")) == len(nodes(graph, "AgentCall")) == 1
    call = nodes(graph, "AgentCall")[0]
    assert call["duration"] == 2000
    final = graph.edges[
        (
            "hasFinalState",
            call["id"],
            next(
                e["to_id"]
                for e in graph.edges.values()
                if e["edge_type"] == "hasFinalState" and e["from_id"] == call["id"]
            ),
        )
    ]
    assert graph.nodes[("State", final["to_id"])]["content"] == "answer"
    assert process_record(graph, completed, "span") == StreamDelta()


@pytest.mark.parametrize("server_first", [False, True])
def test_mcp_participant_does_not_create_another_mas(server_first):
    started = event("topology.node.started", "agent", **{"agent.name": "Planner"})
    message = event(
        "mcp.message.received",
        "server",
        service="tool-server",
        **{
            "source.agent": "Planner",
            "source.agent.span.id": "agent",
            "target.agent": "debug",
            "message.id": "request-1",
            "message.input": '{"name":"search","arguments":{}}',
        },
    )
    records = [(started, "event"), (message, "event")]
    graph = replay(records if not server_first else reversed(records))
    assert [n["id"] for n in nodes(graph, "MAS")] == ["app"]
    assert len(nodes(graph, "Session")) == len(nodes(graph, "Agent")) == 1
    assert {e["to_id"] for e in graph.edges.values() if e["edge_type"] == "executesSession"} == {
        "app"
    }


@pytest.mark.parametrize("graph_first", [False, True])
def test_graph_metadata_declares_only_observed_agents(graph_first):
    graph_span = span(
        "workflow.graph",
        "graph",
        **{
            "gen_ai.ioa.graph": json.dumps(
                {
                    "nodes": {n: {"id": n, "name": n} for n in ("planner", "router", "tools")},
                }
            )
        },
    )
    agent_span = span("planner.agent", "agent", agent_id="planner")
    records = [(graph_span, "span"), (agent_span, "span")]
    graph = replay(records if graph_first else reversed(records))
    assert [n["name"] for n in nodes(graph, "Agent")] == ["planner"]
    assert nodes(graph, "Agent")[0]["declared"] is True
    assert process_record(graph, graph_span, "span") == StreamDelta()


@pytest.mark.parametrize("edge_first", [False, True])
def test_explicit_handoff_is_backfilled_without_assigning_a_second_agent(edge_first):
    handoff = event(
        "topology.edge.updated",
        "transport",
        **{
            "source.agent": "Planner",
            "source.agent.span.id": "source",
            "target.agent": "Worker",
            "topology.edge.kind": "a2a_message",
            "network.protocol.name": "a2a",
        },
    )
    records = [
        (
            event("topology.node.started", "source", **{"agent.name": "Planner"}),
            "event",
        ),
        (
            event(
                "topology.node.completed",
                "source",
                **{
                    "agent.name": "Planner",
                    "agent.output": "plan",
                },
            ),
            "event",
        ),
        (
            event(
                "topology.node.started",
                "target",
                **{
                    "agent.name": "Worker",
                    "agent.input": "execute",
                },
            ),
            "event",
        ),
    ]
    graph = replay(
        ([(handoff, "event")] + records) if edge_first else (records + [(handoff, "event")])
    )
    assert nodes(graph, "ProcessingCall")
    for call in nodes(graph, "AgentCall"):
        assert (
            sum(
                e["edge_type"] == "executesAgent" and e["from_id"] == call["id"]
                for e in graph.edges.values()
            )
            == 1
        )
    assert process_record(graph, handoff, "event") == StreamDelta()


def test_presented_tools_and_a2a_completion_are_retained():
    graph = replay(
        [
            (
                event("topology.node.started", "agent", **{"agent.name": "Planner"}),
                "event",
            ),
            (
                event(
                    "llm.tools.presented",
                    "agent",
                    **{
                        "capability.content": json.dumps(
                            {"tools": [{"type": "function", "function": {"name": "search"}}]}
                        ),
                    },
                ),
                "event",
            ),
            (
                event(
                    "a2a.message.completed",
                    "transport",
                    **{
                        "source.agent": "Planner",
                        "source.agent.span.id": "agent",
                        "target.agent": "Worker",
                        "message.id": "m1",
                        "message.output": "reply",
                    },
                ),
                "event",
            ),
        ]
    )
    assert any(n["name"] == "search" for n in nodes(graph, "Tool"))
    assert any(n["content"] == "reply" for n in nodes(graph, "State"))


@pytest.mark.parametrize("message_kind", ["received", "completed"])
def test_unresolved_a2a_messages_stay_connected_until_their_owner_arrives(message_kind):
    message = event(
        f"a2a.message.{message_kind}",
        "transport",
        **{
            "source.agent": "Sender",
            "source.agent.span.id": "sender",
            "target.agent": "Worker Display Name",
            "message.id": "message",
            "message.input": "request",
            "message.output": "response",
        },
    )
    unrelated = event(
        "topology.node.completed",
        "other",
        **{"agent.name": "worker_runtime", "agent.input": "input", "agent.output": "output"},
    )
    owner = event(
        "topology.node.started",
        "receiver" if message_kind == "received" else "sender",
        **{"agent.name": "Worker Display Name" if message_kind == "received" else "Sender"},
    )
    for order in ((message,), *itertools.permutations((message, unrelated))):
        graph = replay((record, "event") for record in order)
        state = next(
            n
            for n in nodes(graph, "State")
            if n["content"] == ("request" if message_kind == "received" else "response")
        )
        assert ("hasState", "s1", state["id"]) in graph.edges
        assert not any(
            edge == "hasState" and target == state["id"] and source != "s1"
            for edge, source, target in graph.edges
        )
        assert process_record(graph, message, "event") == StreamDelta()
        graph = stored_copy(graph)
        process_record(graph, owner, "event")
        call = next(n for n in nodes(graph, "AgentCall") if n["spanId"] == owner["SpanId"])
        assert ("hasState", call["id"], state["id"]) in graph.edges
        assert ("hasState", "s1", state["id"]) not in graph.edges
        assert process_record(graph, message, "event") == StreamDelta()


@pytest.mark.parametrize("call_kind", ["llm", "tool"])
def test_completion_before_start_preserves_input_and_corrects_timing(call_kind):
    attrs = {
        "agent.name": "Planner",
        "ioa_observe.agent.span_id": "agent",
        "llm.name": "test/model",
        "tool.name": "search",
    }
    records = [
        (event("topology.node.started", "agent", **{"agent.name": "Planner"}), "event"),
        (
            event(
                f"{call_kind}.completed",
                "child",
                time="2026-11-10T00:00:02+00:00",
                **{**attrs, f"{call_kind}.output": "answer"},
            ),
            "event",
        ),
        (
            event(
                f"{call_kind}.started",
                "child",
                **{**attrs, f"{call_kind}.input": "question"},
            ),
            "event",
        ),
    ]
    graph = replay(records)
    call = nodes(graph, "LLMCall" if call_kind == "llm" else "ToolCall")[0]
    assert call["duration"] == 1000
    states = {
        e["edge_type"]: graph.nodes[("State", e["to_id"])]["content"]
        for e in graph.edges.values()
        if e["from_id"] == call["id"] and e["edge_type"] in ("hasInitialState", "hasFinalState")
    }
    assert states == {"hasInitialState": "question", "hasFinalState": "answer"}


def test_session_envelope_is_corrected_when_parent_span_arrives_last():
    graph = replay(
        [
            (
                event("topology.node.started", "agent", **{"agent.name": "Planner"}),
                "event",
            ),
            (
                span(
                    "planner.agent",
                    "agent",
                    **{
                        "agent_id": "planner",
                        "ioa_observe.entity.name": "Planner",
                    },
                ),
                "span",
            ),
        ]
    )
    call = nodes(graph, "AgentCall")[0]
    for kind in ("MASCall", "Session"):
        container = nodes(graph, kind)[0]
        assert container["startTime"] <= call["startTime"]
        assert container["endTime"] >= call["endTime"]


def test_mixed_records_converge_and_redelivery_is_a_noop():
    records = [
        (event("topology.session.started"), "event"),
        (event("topology.node.started", "agent", **{"agent.name": "Planner"}), "event"),
        (
            event(
                "llm.started",
                "chat",
                **{
                    "agent.name": "Planner",
                    "ioa_observe.agent.span_id": "agent",
                    "llm.name": "test/model",
                    "llm.input": "question",
                },
            ),
            "event",
        ),
        (
            event(
                "llm.completed",
                "chat",
                time="2026-11-10T00:00:02+00:00",
                **{
                    "agent.name": "Planner",
                    "ioa_observe.agent.span_id": "agent",
                    "llm.name": "test/model",
                    "llm.output": "answer",
                },
            ),
            "event",
        ),
        (
            event(
                "mcp.message.received",
                "server",
                service="tool-server",
                **{
                    "source.agent": "Planner",
                    "source.agent.span.id": "agent",
                    "target.agent": "debug",
                    "message.id": "request-1",
                    "message.input": '{"name":"search","arguments":{}}',
                },
            ),
            "event",
        ),
        (
            event(
                "tool.started",
                "tool",
                **{
                    "tool.name": "search",
                    "tool.call.id": "request-1",
                    "ioa_observe.agent.span_id": "agent",
                    "tool.input": "query",
                },
            ),
            "event",
        ),
        (
            span(
                "planner.agent",
                "agent",
                **{
                    "agent_id": "planner",
                    "ioa_observe.entity.name": "Planner",
                    "ioa_observe.entity.input": "question",
                    "ioa_observe.entity.output": "answer",
                },
            ),
            "span",
        ),
    ]
    expected = replay(records)
    for seed in range(10):
        order = random.Random(seed).sample(records, len(records))
        graph = replay(order)
        assert graph.nodes == expected.nodes
        assert graph.edges == expected.edges
        for record, kind in records:
            assert process_record(graph, record, kind) == StreamDelta()


def test_top_level_agent_id_does_not_duplicate_the_agent_named_by_events():
    completed = span("planner.agent", "agent", **{"ioa_observe.entity.name": "Planner"})
    completed["AgentId"] = "planner-runtime-id"
    graph = replay(
        [
            (
                event("topology.node.started", "agent", **{"agent.name": "Planner"}),
                "event",
            ),
            (completed, "span"),
        ]
    )
    assert len(nodes(graph, "Agent")) == len(nodes(graph, "AgentCall")) == 1


def test_presented_tools_enrich_existing_tool_definitions():
    graph = replay(
        [
            (event("tool.started", "tool", **{"tool.name": "search"}), "event"),
            (
                event(
                    "llm.tools.presented",
                    "agent",
                    **{
                        "capability.content": json.dumps(
                            {
                                "tools": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "search",
                                            "description": "Search documents",
                                        },
                                    }
                                ]
                            }
                        ),
                    },
                ),
                "event",
            ),
        ]
    )
    assert nodes(graph, "Tool")[0]["description"] == "Search documents"


def test_presented_tool_ownership_is_backfilled_when_agent_arrives_later():
    graph = replay(
        [
            (
                event(
                    "llm.tools.presented",
                    "agent",
                    **{
                        "capability.content": '{"tools":[{"function":{"name":"search"}}]}',
                    },
                ),
                "event",
            ),
            (
                event("topology.node.started", "agent", **{"agent.name": "Planner"}),
                "event",
            ),
        ]
    )
    agent = nodes(graph, "Agent")[0]
    assert ("usesTool", agent["id"], "search") in graph.edges


@pytest.mark.parametrize("event_first", [False, True])
@pytest.mark.parametrize("call_kind,native_id", [("llm", True), ("tool", True), ("tool", False)])
def test_completed_capability_spans_enrich_events_without_duplicate_calls(
    event_first,
    call_kind,
    native_id,
):
    started = event(
        f"{call_kind}.started",
        "child",
        **{
            "agent.name": "Planner",
            "ioa_observe.agent.span_id": "agent",
            "llm.name": "test/model",
            "tool.name": "search",
            "tool.call.id": "request-1",
        },
    )
    completed = span(
        "model.chat" if call_kind == "llm" else "search.tool",
        "child",
        **{
            "agent_id": "Planner",
            "ioa_observe.agent.span_id": "agent",
            "gen_ai.provider.name": "test",
            "gen_ai.request.model": "model",
            "gen_ai.usage.input_tokens": "42",
            "gen_ai.tool.call.id": "request-1",
            "ioa_observe.entity.name": "search",
            "ioa_observe.entity.output": "answer",
            "gen_ai.output.messages": "answer",
        },
    )
    if not native_id:
        completed["SpanAttributes"].pop("gen_ai.tool.call.id")
    records = [(started, "event"), (completed, "span")]
    graph = replay(records if event_first else reversed(records))
    calls = nodes(graph, "LLMCall" if call_kind == "llm" else "ToolCall")
    assert len(calls) == 1
    assert calls[0]["duration"] == 2000
    if call_kind == "llm":
        assert calls[0]["promptTokenCount"] == 42
    else:
        assert calls[0]["id"] == "request-1"
    assert process_record(graph, completed, "span") == StreamDelta()
