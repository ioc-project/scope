# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

from norm import InMemoryGraph, StreamDelta, SpanContext, StreamNormalizer

from rt_norm_worker.store import Neo4jStore


class PopulatedDB:
    def __init__(self):
        self.commands = []
        self.reads = []

    def execute(self, query, params):
        self.reads.append((query, params))
        if "RETURN labels(n)" in query:
            return [
                {
                    "label": "AgentCall",
                    "props": {
                        "id": "call",
                        "sessionId": "s1",
                        "rtNormalization": '__json__:{"spanComplete":true}',
                    },
                }
            ]
        return [
            {
                "edge_type": "executesAgent",
                "props": {"evidence": '__json__:{"source":"span"}'},
                "from_id": "call",
                "from_label": "AgentCall",
                "from_props": {"id": "call", "sessionId": "s1"},
                "to_id": "planner",
                "to_label": "Agent",
                "to_props": {"id": "planner", "name": "Planner"},
            }
        ]

    def execute_command(self, command, params):
        self.commands.append((command, params))


def test_load_decodes_reconciliation_metadata_and_loads_edge_endpoints():
    db = PopulatedDB()
    nodes, edges = Neo4jStore(db).load("s1", ["planner"])
    by_id = {n["id"]: n for n in nodes}
    assert by_id["call"]["rtNormalization"] == {"spanComplete": True}
    assert by_id["planner"] == {
        "node_type": "Agent",
        "id": "planner",
        "name": "Planner",
    }
    assert edges == [
        {
            "edge_type": "executesAgent",
            "from_id": "call",
            "to_id": "planner",
            "from_type": "AgentCall",
            "to_type": "Agent",
            "evidence": {"source": "span"},
        }
    ]
    assert db.reads[0][1] == {"sid": "s1", "ids": ["planner"]}
    assert "properties(r) AS props" in db.reads[1][0]


def test_apply_removes_superseded_edges_and_nodes_before_writing_replacements():
    db = PopulatedDB()
    edge = {
        "edge_type": "hasInitialState",
        "from_id": "call",
        "to_id": "old",
        "from_type": "AgentCall",
        "to_type": "State",
    }
    Neo4jStore(db).apply(
        StreamDelta(
            removed_edges=[edge],
            removed_nodes=[{"node_type": "State", "id": "old"}],
            nodes=[{"node_type": "State", "id": "new", "content": "question"}],
            edges=[{**edge, "to_id": "new"}],
        )
    )
    queries = [q for q, _ in db.commands]
    assert queries[0].endswith("DELETE r")
    assert queries[1].endswith("DETACH DELETE n")
    assert queries[2].startswith("MERGE (n:State")
    assert queries[3].startswith("MERGE (a:AgentCall")
    assert db.commands[0][1] == {"from_id": "call", "to_id": "old"}
    assert db.commands[1][1] == {"id": "old"}
    assert db.commands[3][1]["to_id"] == "new"


def test_memory_store_applies_earlier_start_time_corrections():
    graph = InMemoryGraph()
    node = {"node_type": "Session", "id": "s1", "startTime": 20.0, "endTime": 30.0}
    graph.apply(StreamDelta(nodes=[node]))
    graph.apply(StreamDelta(nodes=[{**node, "startTime": 10.0, "duration": 20000.0}]))
    assert graph.nodes[("Session", "s1")]["startTime"] == 10.0


def test_private_trace_context_stays_in_memory_and_is_scoped_to_store_lifetime():
    db = PopulatedDB()
    store = Neo4jStore(db)
    sessionless = SpanContext("trace", "server", "client", "", "")
    client = SpanContext("trace", "client", "", "agent", "session")
    other_trace = SpanContext("other", "server", "", "other-agent", "other-session")
    store.apply(StreamDelta(span_contexts=[sessionless]))
    store.apply(StreamDelta(span_contexts=[client, other_trace]))
    assert set(store.load_span_contexts("trace")) == {sessionless, client}
    assert store.load_span_contexts("other") == [other_trace]
    assert store.load_span_contexts("different") == []
    store.apply(StreamDelta(span_contexts=[sessionless]))
    assert set(store.load_span_contexts("trace")) == {sessionless, client}
    enriched = SpanContext("trace", "server", "client", "agent", "session")
    store.apply(StreamDelta(span_contexts=[enriched]))
    assert set(store.load_span_contexts("trace")) == {enriched, client}
    assert store.load_span_contexts("other") == [other_trace]
    assert Neo4jStore(db).load_span_contexts("trace") == []
    assert db.commands == []
    assert db.reads == []


def test_late_sessionless_transport_backfills_ownership_using_store_memory(monkeypatch):
    db = PopulatedDB()
    store = Neo4jStore(db)
    graph = InMemoryGraph()
    monkeypatch.setattr(store, "load", graph.load)
    common = {
        "TraceId": "trace",
        "Timestamp": "2026-11-10 00:00:00.000000000",
        "Duration": 1_000_000_000,
        "SpanAttributes": {"application_id": "app", "session.id": "app_s1"},
    }
    records = [
        {
            **common,
            "SpanName": "planner.agent",
            "SpanId": "agent",
            "SpanAttributes": {**common["SpanAttributes"], "agent_id": "planner"},
        },
        {
            **common,
            "SpanName": "search.tool",
            "SpanId": "tool",
            "ParentSpanId": "server",
            "SpanAttributes": {**common["SpanAttributes"], "ioa_observe.entity.name": "search"},
        },
        {
            **common,
            "SpanName": "tools/call search",
            "SpanId": "server",
            "ParentSpanId": "agent",
            "SpanAttributes": {},
        },
    ]
    for record in records:
        delta = StreamNormalizer(store).process(record, "span")
        store.apply(delta)
        graph.apply(delta)
        graph.span_contexts.clear()
    owner = next(n for (kind, _), n in graph.nodes.items() if kind == "AgentCall")
    call = next(n for (kind, _), n in graph.nodes.items() if kind == "ToolCall")
    assert ("hasToolCall", owner["id"], call["id"]) in graph.edges
    assert store.load_span_contexts("trace")
    assert db.reads == []
    assert not any("_NormalizationTrace" in query for query, _ in db.commands)
    for record in records:
        assert StreamNormalizer(store).process(record, "span") == StreamDelta()
