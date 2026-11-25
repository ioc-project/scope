# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Opt-in SQL integration tests against an existing local ClickHouse container.

Set SCOPE_REALTIME_CLICKHOUSE_CONTAINER to its container ID/name. These tests create
and drop only a uniquely named test database, never the configured application DB.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from clickhouse_connect.driver.binding import format_bind_value

from scope.client.local import LocalClient
from scope.connectors.base import Connector
from scope.core.exceptions import NotFoundError, ValidationError
from scope.models.realtime_events import EventScope, RealtimeEventQuery
from scope.query_builders import realtime_events as queries

CONTAINER = os.getenv("SCOPE_REALTIME_CLICKHOUSE_CONTAINER")
pytestmark = pytest.mark.skipif(
    not CONTAINER,
    reason="Set SCOPE_REALTIME_CLICKHOUSE_CONTAINER for SQL integration tests",
)
SCOPE = {"tenant_id": "tenant-1", "mas_id": "mas-1"}


def event_id(number: int) -> str:
    return f"{number:064x}"


class ContainerConnector(Connector):
    def connect(self):
        pass

    def close(self):
        pass

    def is_connected(self):
        return True

    def _run(self, query, params=None):
        command = [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "clickhouse-client",
            "--output_format_json_quote_64bit_integers=0",
            "--format=JSONCompact",
            "--query",
            query,
        ]
        for key, value in (params or {}).items():
            command.append(f"--param_{key}={format_bind_value(value)}")
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def execute(self, query, params=None):
        return json.loads(self._run(query, params))["data"]

    def execute_command(self, command, params=None):
        self._run(command, params)


@pytest.fixture(scope="module")
def seeded_client():
    db = ContainerConnector()
    database = "scope_realtime_test_" + uuid4().hex
    patch = pytest.MonkeyPatch()
    db.execute_command(f"CREATE DATABASE {database}")
    try:
        schema = (
            Path(__file__).resolve().parents[3]
            / "integrations/cognition-fabric/event-exporter/internal/storage/schema.sql"
        ).read_text()
        db.execute_command(schema % database)
        table = f"{database}.normalized_events"
        patch.setattr(queries, "TABLE", f"{table} FINAL")
        specs = [
            ("topology.session.started", {}),
            ("llm.started", {"input": [{"role": "user", "content": "hello"}]}),
            ("llm.completed", {"output": [{"role": "assistant", "content": "hi"}]}),
            ("tool.started", {"protocol": "mcp", "agent": {"name": "researcher"}}),
            ("tool.completed", {"protocol": "mcp", "agent": {"name": "researcher"}}),
            ("a2a.message.sent", {"protocol": "a2a"}),
            ("mcp.message.received", {"protocol": "mcp"}),
            (
                "topology.edge.updated",
                {
                    "edge": {
                        "kind": "agent_handoff",
                        "source_agent": "planner",
                        "target_agent": "researcher",
                    }
                },
            ),
            (
                "llm.completed",
                {
                    "input": {
                        "messages": [
                            {"role": "user", "content": "next"},
                        ]
                    }
                },
            ),
            ("topology.edge.updated", {"edge": {"source_agent": "planner"}}),
        ]
        records = []
        for index, (kind, data) in enumerate(specs, 1):
            timestamp = f"2026-10-16 09:00:00.{(4 if index == 5 else index):09d}"
            span_id = "1111111111111111" if index in (4, 5) else "0123456789abcdef"
            event = {
                "type": kind,
                "time": timestamp.replace(" ", "T") + "Z",
                "session": {"id": "session-1", "sequence": index},
                "source": {"service_name": "weather-agent"},
                "correlation": {"span_id": span_id},
                "data": data,
            }
            records.append(
                {
                    "event_id": event_id(index),
                    "tenant_id": "tenant-1",
                    "application_id": "mas-1",
                    "session_id": "session-1",
                    "event_time": timestamp,
                    "event_type": kind,
                    "span_id": span_id,
                    "service_name": "weather-agent",
                    "payload": json.dumps(event),
                }
            )
        records += [
            records[0].copy(),
            records[0] | {"tenant_id": "other-tenant"},
            records[0] | {"application_id": "other-mas"},
            records[0]
            | {
                "event_id": event_id(100),
                "session_id": "session-2",
                "span_id": "fedcba9876543210",
            },
        ]
        db.execute_command(
            f"INSERT INTO {table} FORMAT JSONEachRow\n"
            + "\n".join(json.dumps(record) for record in reversed(records))
        )
        yield LocalClient(db=db)
    finally:
        patch.undo()
        db.execute_command(f"DROP DATABASE {database}")


def history(client, **kwargs):
    return client.get_realtime_events(
        session_id="session-1", query=RealtimeEventQuery(**SCOPE, **kwargs)
    )


def ids(page):
    return [event["event_id"] for event in page.events]


def test_sql_deduplicates_scopes_and_orders_at_nanosecond_precision(seeded_client):
    page = history(seeded_client)
    assert ids(page) == [event_id(i) for i in range(1, 11)]
    assert page.events[0]["time"] == "2026-10-16T09:00:00.000000001Z"


@pytest.mark.parametrize(
    ("scope", "expected_count"),
    [
        ({}, 12),
        ({"tenant_id": "tenant-1"}, 11),
        ({"mas_id": "mas-1"}, 11),
        (SCOPE, 10),
    ],
)
def test_sql_optional_scope_filters(seeded_client, scope, expected_count):
    query = RealtimeEventQuery(**scope)
    page = seeded_client.get_realtime_events(session_id="session-1", query=query)
    assert len(page.events) == expected_count
    page = seeded_client.get_realtime_events_for_span(
        span_id="0123456789abcdef", query=query
    )
    assert len(page.events) == expected_count - 2
    page = seeded_client.get_realtime_events_for_span(
        span_id="0123456789abcdef", query=query, include_session_events=True
    )
    assert len(page.events) == expected_count
    event = seeded_client.get_realtime_event(
        event_id=event_id(4), scope=EventScope(**scope)
    )
    assert event["event_id"] == event_id(4)
    bounded = seeded_client.get_realtime_events(
        session_id="session-1",
        query=RealtimeEventQuery(
            **scope, start_event_id=event_id(4), end_event_id=event_id(5)
        ),
    )
    assert ids(bounded) == [event_id(4), event_id(5)]


@pytest.mark.parametrize("scope", [{}, {"tenant_id": "tenant-1"}, {"mas_id": "mas-1"}])
def test_sql_unscoped_duplicate_ids_remain_ambiguous(seeded_client, scope):
    with pytest.raises(ValidationError, match="ambiguous"):
        seeded_client.get_realtime_event(
            event_id=event_id(1), scope=EventScope(**scope)
        )
    with pytest.raises(ValidationError, match="ambiguous"):
        seeded_client.get_realtime_events(
            session_id="session-1",
            query=RealtimeEventQuery(**scope, start_event_id=event_id(1)),
        )


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        (["llm"], [2, 3, 9]),
        (["tool"], [4, 5]),
        (["mcp"], [4, 5, 7]),
        (["a2a"], [6]),
        (["user"], [2, 9]),
        (["llm", "tool"], [2, 3, 4, 5, 9]),
    ],
)
def test_sql_category_filters(seeded_client, categories, expected):
    assert ids(history(seeded_client, categories=categories)) == [
        event_id(i) for i in expected
    ]


def test_sql_event_bounds_and_limits_with_equal_timestamps(seeded_client):
    page = history(
        seeded_client, start_event_id=event_id(4), end_event_id=event_id(6), limit=1
    )
    assert ids(page) == [event_id(4)]
    assert page.has_more
    page = history(
        seeded_client,
        start_event_id=event_id(4),
        end_event_id=event_id(6),
    )
    assert ids(page) == [event_id(4), event_id(5), event_id(6)]
    assert not page.has_more


def test_sql_timestamp_is_inclusive_and_understands_timezones(seeded_client):
    page = history(seeded_client, start_time="2026-10-16T10:00:00+01:00")
    assert len(page.events) == 10
    page = history(seeded_client, start_time="2026-10-16T09:00:00.000001Z")
    assert page.events == []
    page = history(seeded_client, start_time="2026-10-16T09:00:00.000000004Z")
    assert ids(page) == [event_id(i) for i in range(4, 11)]
    page = history(seeded_client, start_time="2026-10-16T10:00:00.000000005+01:00")
    assert ids(page) == [event_id(i) for i in range(6, 11)]


def test_sql_event_type_filter_preserves_payload(seeded_client):
    page = history(
        seeded_client,
        event_types=["tool.completed"],
    )
    assert ids(page) == [event_id(5)]
    assert page.events[0]["data"] == {
        "protocol": "mcp",
        "agent": {"name": "researcher"},
    }
    assert history(seeded_client, event_types=["x' OR 1=1 --"]).events == []


def test_sql_scoped_anchor_cannot_reference_another_session(seeded_client):
    with pytest.raises(NotFoundError):
        history(seeded_client, end_event_id=event_id(100))


def test_sql_span_resolution_and_single_event(seeded_client):
    page = seeded_client.get_realtime_events_for_span(
        span_id="0123456789abcdef", query=RealtimeEventQuery(**SCOPE)
    )
    assert len(page.events) == 8
    assert all(
        event["correlation"]["span_id"] == "0123456789abcdef" for event in page.events
    )
    page = seeded_client.get_realtime_events_for_span(
        span_id="0123456789abcdef",
        query=RealtimeEventQuery(**SCOPE),
        include_session_events=True,
    )
    assert len(page.events) == 10
    event = seeded_client.get_realtime_event(
        event_id=event_id(4), scope=EventScope(**SCOPE)
    )
    assert event["data"]["protocol"] == "mcp"
    with pytest.raises(NotFoundError):
        seeded_client.get_realtime_event(
            event_id=event_id(4),
            scope=EventScope(tenant_id="other-tenant", mas_id="mas-1"),
        )


def test_sql_span_filter_applies_before_limit_and_supports_session_bounds(
    seeded_client,
):
    query = RealtimeEventQuery(
        **SCOPE, start_event_id=event_id(1), end_event_id=event_id(6), limit=1
    )
    page = seeded_client.get_realtime_events_for_span(
        span_id="1111111111111111", query=query
    )
    assert ids(page) == [event_id(4)]
    assert page.has_more
    query.limit = 2
    page = seeded_client.get_realtime_events_for_span(
        span_id="1111111111111111", query=query
    )
    assert ids(page) == [event_id(4), event_id(5)]
    assert not page.has_more


@pytest.mark.parametrize(
    ("include_session_events", "expected"),
    [(False, []), (True, [2, 3, 9])],
)
def test_sql_span_mode_preserves_category_filters(
    seeded_client, include_session_events, expected
):
    page = seeded_client.get_realtime_events_for_span(
        span_id="1111111111111111",
        query=RealtimeEventQuery(**SCOPE, categories=["llm"]),
        include_session_events=include_session_events,
    )
    assert ids(page) == [event_id(i) for i in expected]


def test_sql_start_after_upper_bound_is_rejected(seeded_client):
    with pytest.raises(ValidationError):
        history(
            seeded_client,
            start_time="2026-10-16T09:00:00.000001Z",
            end_event_id=event_id(1),
        )
