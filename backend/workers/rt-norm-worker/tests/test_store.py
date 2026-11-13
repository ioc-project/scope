# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

from norm import InMemoryGraph, StreamDelta, SpanContext

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


def test_private_trace_context_roundtrips_sessionless_spans_without_ontology_nodes():
    class ContextDB:
        def __init__(self):
            self.traces = {}
            self.commands = []

        def execute_command(self, command, params):
            self.commands.append(command)
            self.traces.setdefault(params["trace_id"], {}).update(params["props"])

        def execute(self, query, params):
            assert "_NormalizationTrace" in query
            properties = self.traces.get(params["trace_id"])
            return (
                [{"props": {"traceId": params["trace_id"], **properties}}]
                if properties
                else []
            )

    db = ContextDB()
    store = Neo4jStore(db)
    sessionless = SpanContext("trace", "server", "client", "", "")
    client = SpanContext("trace", "client", "", "agent", "session")
    store.apply(StreamDelta(span_contexts=[sessionless]))
    store.apply(StreamDelta(span_contexts=[client]))
    restored = Neo4jStore(db).load_span_contexts("trace")
    assert set(restored) == {sessionless, client}
    assert Neo4jStore(db).load_span_contexts("different") == []
    assert all(
        command == "MERGE (t:_NormalizationTrace {traceId: $trace_id}) SET t += $props"
        for command in db.commands
    )
    store.apply(StreamDelta(span_contexts=[sessionless]))
    assert set(store.load_span_contexts("trace")) == {sessionless, client}
