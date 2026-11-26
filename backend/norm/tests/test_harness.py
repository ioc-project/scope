#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
from copy import deepcopy
from pathlib import Path

from norm import load_otel_export, normalize_harness

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return load_otel_export(FIXTURES / f"harness_{name}.jsonl")


def nodes(replay, kind):
    return [n for n in replay.nodes if n["node_type"] == kind]


def test_claude_enrichment_and_async_ancestry():
    traces, logs = fixture("claude_traces"), fixture("claude_logs")
    replay = normalize_harness("test", traces=traces, logs=logs)
    (llm,) = nodes(replay, "LLMCall")
    (tool,) = nodes(replay, "ToolCall")
    main = next(a for a in nodes(replay, "AgentCall") if a.get("spanId") == "claude-turn")
    assert len(nodes(replay, "AgentCall")) == 2
    assert llm["startTime"] > main["endTime"]
    graph_edges = {(e["edge_type"], e["from_id"], e["to_id"]) for e in replay.edges}
    assert ("hasLLMCall", main["id"], llm["id"]) not in graph_edges
    assert ("hasToolCall", main["id"], tool["id"]) in graph_edges
    assert llm["cacheReadTokenCount"] == 3
    assert replay.coverage["claude_cost_usd_by_call"] == {llm["id"]: 0.02}
    assert replay.coverage["counts"]["claude_housekeeping"] == 1
    assert replay.coverage["counts"]["observed_claude_permission"] == 1
    assert replay == normalize_harness("test", traces=reversed(traces), logs=reversed(logs))
    duplicate = normalize_harness("test", traces=traces * 2, logs=logs * 2)
    assert (duplicate.nodes, duplicate.edges) == (replay.nodes, replay.edges)
    assert duplicate.coverage["counts"]["duplicate_rows"] == len(traces) + len(logs)


def test_codex_usage_and_payload_boundary():
    logs = fixture("codex_logs")
    traces = [
        {
            "TraceId": "background",
            "SpanId": "fs-call",
            "ServiceName": "codex-app-server",
            "SpanName": "fs.read_file",
            "SpanAttributes": {},
        }
    ]
    replay = normalize_harness("test", traces=traces, logs=logs)
    assert len(nodes(replay, "AgentCall")) == len(nodes(replay, "ToolCall")) == 1
    assert not nodes(replay, "LLMCall")
    assert replay.coverage["counts"]["unmatched_codex_sse_usage"] == 1
    assert replay.coverage["counts"]["codex_housekeeping"] == 2
    assert {s["content"] for s in nodes(replay, "State")} == {"(no content recorded)"}
    approved = normalize_harness("test", traces=traces, logs=logs, include_content=True)
    assert approved.edges == replay.edges
    assert {s["id"] for s in nodes(approved, "State")} == {s["id"] for s in nodes(replay, "State")}
    contents = {s["content"] for s in nodes(approved, "State")}
    assert {"Synthetic turn input", "Synthetic turn output", "Synthetic tool output"} <= contents
    assert "Synthetic" not in repr(approved.coverage)


def test_claude_bare_log_names_and_housekeeping_response():
    traces, logs = fixture("claude_traces"), fixture("claude_logs")
    for row in logs:
        attrs = row["LogAttributes"]
        attrs["event.name"] = attrs["event.name"].removeprefix("claude_code.")
        row["EventName"] = ""
    traces[-1]["SpanAttributes"]["query_source_safe"] = "away_summary"
    for source, response in (
        ("repl_main_thread", "Synthetic turn output"),
        ("away_summary", "Synthetic background summary"),
    ):
        logs.append(
            {
                "ServiceName": "claude-code",
                "TraceId": "trace-claude",
                "SpanId": "claude-turn",
                "EventName": "",
                "LogAttributes": {
                    "event.name": "assistant_response",
                    "session.id": "native-claude",
                    "query_source": source,
                    "response": response,
                },
            }
        )
    replay = normalize_harness("test", traces=traces, logs=logs, include_content=True)
    (llm,) = nodes(replay, "LLMCall")
    assert replay.coverage["claude_cost_usd_by_call"] == {llm["id"]: 0.02}
    assert replay.coverage["counts"]["joined_claude_tool_result"] == 1
    states = {s["id"]: s["content"] for s in nodes(replay, "State")}
    turn = next(node for node in nodes(replay, "AgentCall") if node.get("spanId") == "claude-turn")
    (final,) = (
        e["to_id"]
        for e in replay.edges
        if e["from_id"] == turn["id"] and e["edge_type"] == "hasFinalState"
    )
    assert states[final] == "Synthetic turn output"
    assert "Synthetic background summary" not in states.values()


def test_ambiguous_evidence_is_not_assigned():
    traces = fixture("claude_traces")
    logs = fixture("claude_logs")
    conflict = deepcopy(logs[-1])
    conflict["LogAttributes"]["cost_usd"] = "9"
    codex = fixture("codex_logs")
    second_turn = deepcopy(codex[-2])
    second_turn["LogAttributes"]["turn.id"] = "turn-2"
    replay = normalize_harness("test", traces=traces, logs=[*logs, conflict, *codex, second_turn])
    assert replay.coverage["counts"]["ambiguous_claude_api_request"] == 2
    assert replay.coverage["claude_cost_usd_by_call"] == {}
    assert replay.coverage["counts"]["ambiguous_codex_turn"] == 2
    assert len(nodes(replay, "Session")) == 2
    assert len(nodes(replay, "ToolCall")) == 1  # Claude tool only


def test_codex_explicit_request_id_allows_usage_without_borrowing_turn_io():
    logs = fixture("codex_logs")
    for row in logs[1:3]:
        row["LogAttributes"]["request_id"] = "synthetic-request"
    replay = normalize_harness("test", logs=logs, include_content=True)
    (llm,) = nodes(replay, "LLMCall")
    assert llm["promptTokenCount"] == 15
    states = {s["id"]: s["content"] for s in nodes(replay, "State")}
    assert {
        states[e["to_id"]]
        for e in replay.edges
        if e["from_id"] == llm["id"] and e["edge_type"] in {"hasInitialState", "hasFinalState"}
    } == {"(no content recorded)"}
    assert replay.coverage["counts"]["joined_codex_sse_usage"] == 1


def test_synthetic_bridge_evidence_and_explicit_shared_session():
    traces = fixture("claude_traces")
    logs = fixture("codex_logs")
    for row in traces + logs:
        row["ResourceAttributes"]["ioc.session.id"] = "shared-session"
    send, receive = [
        {
            "SpanName": f"a2a_slimrpc_{direction}.task",
            "TraceId": "trace-claude",
            "SpanId": direction,
            "ParentSpanId": parent,
            "ServiceName": "synthetic-bridge",
            "ResourceAttributes": {"ioc.session.id": "shared-session"},
            "SpanAttributes": {"a2a.context_id": "context-a", "peer.did": "did:example:peer"},
        }
        for direction, parent in (("send", "claude-tool"), ("receive", "send"))
    ]
    replay = normalize_harness("test", traces=[*traces, send, receive], logs=logs)
    assert len(nodes(replay, "Session")) == 1
    assert replay.coverage["hops"][0]["classification"] == "bridge_only"
    for row in logs:
        if row["TraceId"] == "trace-codex":
            row.update(TraceId="trace-claude", SpanId="receive")
    replay = normalize_harness("test", traces=[*traces, send, receive], logs=logs)
    (hop,) = replay.coverage["hops"]
    assert hop["classification"] == "end_to_end"
    assert hop["caller_call_id"] != hop["callee_call_id"]
    assert hop["context_id_observed"] and hop["peer_did_observed"]


def test_unknown_service_cannot_claim_a_harness_event():
    row = fixture("codex_logs")[-2]
    row["ServiceName"] = "muse"
    replay = normalize_harness("test", logs=[row])
    assert replay.nodes == []
    assert replay.coverage["counts"]["unsupported_source"] == 1
