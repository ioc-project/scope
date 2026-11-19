#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0

import gzip
import json
import threading
import urllib.error
import urllib.request
from copy import deepcopy

from norm import InMemoryGraph

from rt_norm_worker import otlp
from rt_norm_worker.server import make_server

SESSION = "noa-trip-planner-mas_3aefd85c"


def _attrs(**kwargs: str) -> list[dict]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in kwargs.items()]


LOGS = {
    "resourceLogs": [
        {
            "resource": {
                "attributes": _attrs(**{"service.name": "noa-trip-planner-mas"})
            },
            "scopeLogs": [
                {
                    "scope": {"name": "ioa_observe.runtime_events"},
                    "logRecords": [
                        {
                            "timeUnixNano": "1794000000000000000",
                            "spanId": "427bddeaf7b098f0",
                            "body": {"stringValue": "topology.session.started"},
                            "attributes": _attrs(
                                **{
                                    "event.name": "topology.session.started",
                                    "event.time": "2026-11-10T00:38:20+00:00",
                                    "session.id": SESSION,
                                    "application_id": "noa-trip-planner-mas",
                                }
                            ),
                        }
                    ],
                }
            ],
        }
    ]
}

TRACES = {
    "resourceSpans": [
        {
            "resource": {"attributes": _attrs(**{"service.name": "svc"})},
            "scopeSpans": [
                {
                    "scope": {"name": "t", "version": "1"},
                    "spans": [
                        {
                            "traceId": "a" * 32,
                            "spanId": "b" * 16,
                            "name": "x.agent",
                            "kind": "SPAN_KIND_INTERNAL",
                            "startTimeUnixNano": "1794000000000000000",
                            "endTimeUnixNano": "1794000001000000000",
                            "status": {"code": "STATUS_CODE_OK"},
                            "attributes": [
                                {"key": "n", "value": {"intValue": "3"}},
                                {"key": "ok", "value": {"boolValue": True}},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}


def test_otlp_flattening_matches_the_collector_export_rows() -> None:
    [span] = list(otlp.spans(TRACES))
    assert span["SpanName"] == "x.agent" and span["SpanKind"] == "Internal"
    assert span["ServiceName"] == "svc" and span["StatusCode"] == "Ok"
    assert span["Duration"] == "1000000000"
    assert span["SpanAttributes"] == {"n": "3", "ok": "true"}
    assert span["Timestamp"].endswith(".000000000") and span["Timestamp"][:4] == "2026"
    [log] = list(otlp.logs(LOGS))
    assert (
        log["Body"] == "topology.session.started"
        and log["ServiceName"] == "noa-trip-planner-mas"
    )
    assert log["LogAttributes"]["session.id"] == SESSION


def _post(
    port: int,
    path: str,
    body: bytes,
    content_type: str = "application/json",
    **headers: str,
):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": content_type, **headers},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_server_normalizes_logs_pushed_by_the_collector() -> None:
    graph = InMemoryGraph()
    server = make_server(graph, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert _post(port, "/v1/logs", json.dumps(LOGS).encode()) == 200
        assert ("Session", "3aefd85c") in graph.nodes and (
            "MAS",
            "noa-trip-planner-mas",
        ) in graph.nodes
        # gzip, as the collector sends by default
        assert (
            _post(
                port,
                "/v1/logs",
                gzip.compress(json.dumps(LOGS).encode()),
                **{"Content-Encoding": "gzip"},
            )
            == 200
        )
        assert _post(port, "/v1/traces", json.dumps(TRACES).encode()) == 200
        assert (
            _post(
                port,
                "/v1/logs",
                b"{",
            )
            == 400
        )
        assert (
            _post(port, "/v1/logs", b"\x00", content_type="application/x-protobuf")
            == 415
        )
        assert _post(port, "/v1/nope", b"{}") == 404
    finally:
        server.shutdown()
        server.server_close()


def test_server_enriches_event_placeholders_with_completed_spans() -> None:
    graph = InMemoryGraph()
    logs = deepcopy(LOGS)
    record = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["body"]["stringValue"] = "topology.node.started"
    record["attributes"] = _attrs(
        **{
            "event.name": "topology.node.started",
            "event.time": "2026-11-06T16:00:00+00:00",
            "session.id": "svc_s1",
            "application_id": "svc",
            "agent.name": "Planner",
        }
    )
    traces = deepcopy(TRACES)
    span = traces["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["spanId"] = record["spanId"]
    span["name"] = "planner.agent"
    span["attributes"] = _attrs(
        **{
            "session.id": "svc_s1",
            "application_id": "svc",
            "agent_id": "planner-runtime",
            "ioa_observe.entity.name": "Planner",
            "ioa_observe.entity.output": "answer",
        }
    )
    server = make_server(graph, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert _post(port, "/v1/logs", json.dumps(logs).encode()) == 200
        assert _post(port, "/v1/traces", json.dumps(traces).encode()) == 200
        calls = [n for (t, _), n in graph.nodes.items() if t == "AgentCall"]
        assert len(calls) == 1
        assert calls[0]["duration"] == 1000
        final = next(
            e["to_id"]
            for e in graph.edges.values()
            if e["edge_type"] == "hasFinalState" and e["from_id"] == calls[0]["id"]
        )
        assert graph.nodes[("State", final)]["content"] == "answer"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
