# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Parameterized ClickHouse reads for realtime session and event lookups."""

from __future__ import annotations

from typing import Any

from scope.models.realtime_events import EventScope, RealtimeEventQuery

TABLE = "normalized_events FINAL"
TRACES_TABLE = "otel_traces"
POSITION = "(event_time, event_id)"


def session_application_ids_query() -> str:
    # Events keep the raw session ID, while the trace column can strip its prefix.
    return f"""
        SELECT DISTINCT application_id
        FROM {TRACES_TABLE}
        WHERE (session_id = {{session_id:String}}
               OR SpanAttributes['session.id'] = {{session_id:String}})
          AND application_id != ''
        ORDER BY application_id
    """


def scope_params(scope: EventScope) -> dict[str, Any]:
    return {
        key: value
        for key, value in (("tenant_id", scope.tenant_id), ("mas_id", scope.mas_id))
        if value is not None
    }


def scope_conditions(scope: EventScope) -> list[str]:
    conditions = []
    if scope.tenant_id is not None:
        conditions.append("tenant_id = {tenant_id:String}")
    if scope.mas_id is not None:
        conditions.append("application_id = {mas_id:String}")
    return conditions


def event_query(scope: EventScope, *, session_id: str | None = None) -> str:
    conditions = scope_conditions(scope)
    if session_id is not None:
        conditions.append("session_id = {session_id:String}")
    conditions.append("event_id = {event_id:String}")
    return (
        "SELECT toUnixTimestamp64Nano(event_time), event_id, payload "
        f"FROM {TABLE} WHERE " + " AND ".join(conditions) + " LIMIT 2"
    )


def session_query(scope: EventScope) -> str:
    conditions = scope_conditions(scope) + [
        "span_id = {span_id:String}",
        "session_id != ''",
    ]
    return (
        f"SELECT DISTINCT session_id FROM {TABLE} WHERE "
        + " AND ".join(conditions)
        + " LIMIT 2"
    )


def history_query(
    query: RealtimeEventQuery,
    session_id: str,
    anchors: dict[str, tuple[int, str]],
    *,
    span_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    params = scope_params(query) | {
        "session_id": session_id,
        "page_size": query.limit + 1,
    }
    conditions = scope_conditions(query) + ["session_id = {session_id:String}"]
    if span_id is not None:
        conditions.append("span_id = {span_id:String}")
        params["span_id"] = span_id
    if query.start_time is not None:
        conditions.append(
            "event_time >= parseDateTime64BestEffort({start_time:String}, 9, 'UTC')"
        )
        params["start_time"] = query.start_time
    for name, operator in (
        ("start_event_id", ">="),
        ("end_event_id", "<="),
    ):
        if name in anchors:
            timestamp, event_id = anchors[name]
            conditions.append(
                f"{POSITION} {operator} "
                f"(fromUnixTimestamp64Nano({{{name}_time:Int64}}), "
                f"{{{name}:String}})"
            )
            params[f"{name}_time"] = timestamp
            params[name] = event_id
    if query.categories:
        category_conditions = {
            "llm": "startsWith(event_type, 'llm.')",
            "tool": "startsWith(event_type, 'tool.')",
            "mcp": (
                "(startsWith(event_type, 'mcp.') OR "
                "(startsWith(event_type, 'tool.') AND "
                "JSONExtractString(payload, 'data', 'protocol') = 'mcp'))"
            ),
            "a2a": "event_type IN ('a2a.message.sent', 'a2a.message.received')",
            "user": (
                "(startsWith(event_type, 'llm.') AND arrayExists("
                "message -> JSONExtractString(message, 'role') = 'user', "
                "arrayConcat("
                "JSONExtractArrayRaw(payload, 'data', 'input'), "
                "JSONExtractArrayRaw(payload, 'data', 'input', 'messages'), "
                "JSONExtractArrayRaw(payload, 'data', 'output'), "
                "JSONExtractArrayRaw(payload, 'data', 'output', 'messages'))))"
            ),
        }
        conditions.append(
            "(" + " OR ".join(category_conditions[c] for c in query.categories) + ")"
        )
    if query.event_types:
        conditions.append("event_type IN {event_types:Array(String)}")
        params["event_types"] = query.event_types
    sql = (
        f"SELECT event_id, payload FROM {TABLE} WHERE "
        + " AND ".join(conditions)
        + " ORDER BY event_time, event_id LIMIT {page_size:UInt32}"
    )
    return sql, params
