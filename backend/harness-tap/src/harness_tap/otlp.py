#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""OTLP/HTTP JSON trace export (no SDK dependency), plus a ClickHouse-row view.

The OTel Collector's OTLP receiver accepts `application/json` on /v1/traces;
the ClickHouse exporter then writes the same otel_traces rows norm reads.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from . import SCOPE_NAME, __version__
from .config import Config
from .spans import SpanRecord

_KIND = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
_STATUS = {"unset": 0, "ok": 1, "error": 2}


def any_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}  # proto3 JSON encodes int64 as a string
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [any_value(v) for v in value]}}
    return {"stringValue": "" if value is None else str(value)}


def key_values(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"key": k, "value": any_value(v)} for k, v in sorted(attrs.items()) if v is not None]


def resource_attrs(cfg: Config, harness: str) -> Dict[str, Any]:
    attrs = {"service.name": cfg.service_name, "ioc.agent.harness": harness}
    attrs.update(cfg.resource)
    attrs.setdefault("ioc.tap.version", __version__)
    return attrs


def export_request(cfg: Config, spans: Iterable[SpanRecord]) -> Dict[str, Any]:
    by_harness: Dict[str, List[SpanRecord]] = defaultdict(list)
    for span in spans:
        by_harness[span.harness].append(span)
    resource_spans = []
    for harness, group in sorted(by_harness.items()):
        resource_spans.append(
            {
                "resource": {"attributes": key_values(resource_attrs(cfg, harness))},
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": __version__},
                        "spans": [_span(s) for s in group],
                    }
                ],
            }
        )
    return {"resourceSpans": resource_spans}


def _span(span: SpanRecord) -> Dict[str, Any]:
    data = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": _KIND.get(span.kind, 1),
        "startTimeUnixNano": str(span.start_ns),
        "endTimeUnixNano": str(max(span.end_ns, span.start_ns)),
        "attributes": key_values(span.attributes),
        "status": {"code": _STATUS.get(span.status, 0)},
    }
    if span.parent_span_id:
        data["parentSpanId"] = span.parent_span_id
    if span.status_message:
        data["status"]["message"] = span.status_message
    if span.links:
        data["links"] = [
            {"traceId": link["trace_id"], "spanId": link["span_id"]}
            for link in span.links
            if link.get("trace_id") and link.get("span_id")
        ]
    return data


def post(cfg: Config, spans: List[SpanRecord]) -> Tuple[bool, str]:
    """POST spans to <endpoint>/v1/traces. Returns (ok, detail)."""
    if not spans:
        return True, "nothing to send"
    url = cfg.endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    body = json.dumps(export_request(cfg, spans)).encode()
    headers = {"Content-Type": "application/json", **cfg.headers}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=cfg.export_timeout_s) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return False, type(error).__name__ + ": " + str(getattr(error, "reason", error))


def clickhouse_rows(cfg: Config, spans: Iterable[SpanRecord]) -> List[Dict[str, Any]]:
    """Render spans as otel_traces-shaped rows (what norm.load_otel_export reads)."""
    rows = []
    for span in spans:
        moment = datetime.fromtimestamp(span.start_ns / 1e9, tz=timezone.utc)
        rows.append(
            {
                "Timestamp": moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{span.start_ns % 10**9:09d}",
                "TraceId": span.trace_id,
                "SpanId": span.span_id,
                "ParentSpanId": span.parent_span_id,
                "SpanName": span.name,
                "SpanKind": span.kind.capitalize(),
                "ServiceName": cfg.service_name,
                "ResourceAttributes": {
                    k: str(v) for k, v in resource_attrs(cfg, span.harness).items()
                },
                "ScopeName": SCOPE_NAME,
                "SpanAttributes": {
                    k: (
                        json.dumps(v)
                        if isinstance(v, (list, dict))
                        else str(v).lower()
                        if isinstance(v, bool)
                        else str(v)
                    )
                    for k, v in span.attributes.items()
                    if v is not None
                },
                "Duration": max(span.end_ns - span.start_ns, 0),
                "StatusCode": span.status.capitalize(),
                "StatusMessage": span.status_message,
            }
        )
    return rows
