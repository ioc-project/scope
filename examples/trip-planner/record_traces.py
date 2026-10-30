#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Record the traces of the trip planner as the dataset that load_traces.py replays.

It runs `trip_planner.py --questions all` against a local OTLP receiver, with the model you configure
(OPENAI_API_KEY, OPENAI_BASE_URL, MODEL), and writes one line per session (its spans, as OTLP/JSON) to
traces/trip_planner_sessions.jsonl.gz. The dataset in the repository was made with it; you only need it
to record your own.

    uv run record_traces.py
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

OUT = Path(__file__).resolve().parent / "traces" / "trip_planner_sessions.jsonl.gz"
requests: list[ExportTraceServiceRequest] = []


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path.endswith("/v1/traces"):
            if self.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            requests.append(ExportTraceServiceRequest.FromString(body))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def span_to_json(span) -> dict:
    """OTLP/JSON of a span: MessageToDict encodes ids as base64, OTLP/JSON wants hex."""
    data = MessageToDict(span)
    data["traceId"], data["spanId"] = span.trace_id.hex(), span.span_id.hex()
    data["parentSpanId"] = span.parent_span_id.hex()
    for link, proto in zip(data.get("links", []), span.links):
        link["traceId"], link["spanId"] = proto.trace_id.hex(), proto.span_id.hex()
    return data


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    Thread(target=server.serve_forever, daemon=True).start()
    env = {**os.environ, "OTLP_HTTP_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}"}
    subprocess.run([sys.executable, "trip_planner.py", "--questions", "all"], env=env, check=True)

    sessions: dict[str, list[dict]] = defaultdict(list)
    resource: dict = {}
    for request in requests:
        for resource_spans in request.resource_spans:
            resource = MessageToDict(resource_spans.resource)
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    attributes = {kv.key: kv.value.string_value for kv in span.attributes}
                    sessions[attributes["session.id"]].append(span_to_json(span))

    OUT.parent.mkdir(exist_ok=True)
    ordered = sorted(sessions.items(), key=lambda item: min(int(s["startTimeUnixNano"]) for s in item[1]))
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        for session_id, spans in ordered:
            spans.sort(key=lambda s: int(s["startTimeUnixNano"]))
            handle.write(json.dumps({"session_id": session_id, "resource": resource, "spans": spans}) + "\n")
    print(f"{len(ordered)} sessions, {sum(len(s) for _, s in ordered)} spans written to {OUT}")


if __name__ == "__main__":
    main()
