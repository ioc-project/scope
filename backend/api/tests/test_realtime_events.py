# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from scope.connectors.base import Connector
from scope.core.exceptions import NotFoundError, ValidationError
from scope.dependencies import get_db
from scope.client.local import LocalClient
from scope.models.realtime_events import EventScope, RealtimeEventQuery
from scope.query_builders import realtime_events as queries

EVENT_A = "a" * 64
EVENT_B = "b" * 64
EVENT_C = "c" * 64
SCOPE = {"tenant_id": "tenant-1", "mas_id": "mas-1"}
BASE = "/api/v1/realtime"
REMOVED_FILTERS = {
    "protocol": "mcp",
    "service_name": "weather-agent",
    "agent_name": "researcher",
    "after_event_id": EVENT_A,
    "edge_kind": "agent_handoff",
    "cross_agent_only": "true",
    "include_payload": "false",
}


def payload(event_type: str = "llm.completed") -> str:
    return json.dumps(
        {
            "type": event_type,
            "time": "2026-10-16T09:00:00.123456789Z",
            "session": {"id": "session-1", "sequence": 2},
            "source": {"service_name": "weather-agent"},
            "correlation": {"span_id": "0123456789abcdef"},
            "data": {"input": [{"role": "user", "content": "hello"}]},
        }
    )


@pytest.fixture
def db():
    return Mock(spec=Connector)


@pytest.fixture
def http(db, cognition_fabric_app):
    cognition_fabric_app.dependency_overrides[get_db] = lambda: db
    return TestClient(cognition_fabric_app, raise_server_exceptions=False)


def test_default_history_keeps_normalized_payload_and_storage_event_id(http, db):
    db.execute.return_value = [(EVENT_A, payload())]
    response = http.get(f"{BASE}/sessions/session-1/events", params=SCOPE)
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "events": [json.loads(payload()) | {"event_id": EVENT_A}],
        "has_more": False,
    }
    sql, params = db.execute.call_args.args
    assert "normalized_events FINAL" in sql
    assert "ORDER BY event_time, event_id" in sql
    assert params == SCOPE | {"session_id": "session-1", "page_size": 101}


def test_page_reads_one_extra_and_keeps_full_payload(http, db):
    db.execute.return_value = [(EVENT_A, payload()), (EVENT_B, payload())]
    response = http.get(
        f"{BASE}/sessions/session-1/events",
        params=SCOPE | {"limit": 1},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["has_more"] is True
    assert set(body) == {"session_id", "events", "has_more"}
    assert len(body["events"]) == 1
    assert body["events"][0]["data"] == json.loads(payload())["data"]
    assert body["events"][0]["source"] == {"service_name": "weather-agent"}
    assert db.execute.call_args.args[1]["page_size"] == 2


def test_event_bounds_are_inclusive(http, db):
    db.execute.side_effect = [
        [(100, EVENT_A, payload())],
        [(200, EVENT_C, payload())],
        [(EVENT_C, payload())],
    ]
    response = http.get(
        f"{BASE}/sessions/session-1/events",
        params=SCOPE
        | {
            "start_event_id": EVENT_A,
            "end_event_id": EVENT_C,
        },
    )
    assert response.status_code == 200
    for call in db.execute.call_args_list[:2]:
        sql, params = call.args
        assert "session_id = {session_id:String}" in sql
        assert params["session_id"] == "session-1"
        assert params["tenant_id"] == "tenant-1"
        assert params["mas_id"] == "mas-1"
    sql, params = db.execute.call_args.args
    assert "(event_time, event_id) >= " in sql
    assert "(event_time, event_id) <= " in sql
    assert "(event_time, event_id) > " not in sql
    assert params["start_event_id_time"] == 100


@pytest.mark.parametrize("include_session_events", [None, False, True])
def test_span_defaults_to_span_events_and_can_include_session(
    http, db, include_session_events
):
    db.execute.side_effect = [
        [("session-1",)],
        [(EVENT_A, payload()), (EVENT_B, payload("tool.completed"))],
    ]
    params = SCOPE | (
        {"include_session_events": include_session_events}
        if include_session_events is not None
        else {}
    )
    response = http.get(f"{BASE}/spans/0123456789abcdef/events", params=params)
    assert response.status_code == 200
    assert len(response.json()["events"]) == 2
    assert "span_id = {span_id:String}" in db.execute.call_args_list[0].args[0]
    sql, bound = db.execute.call_args.args
    if include_session_events:
        assert "span_id" not in sql
        assert "span_id" not in bound
    else:
        assert "span_id = {span_id:String}" in sql
        assert bound["span_id"] == "0123456789abcdef"
    assert db.execute.call_args.args[1]["session_id"] == "session-1"


@pytest.mark.parametrize(
    ("rows", "status"), [([], 404), ([("session-1",), ("session-2",)], 422)]
)
def test_missing_or_ambiguous_span(http, db, rows, status):
    db.execute.return_value = rows
    response = http.get(f"{BASE}/spans/0123456789abcdef/events", params=SCOPE)
    assert response.status_code == status
    assert db.execute.call_count == 1


def test_single_event_lookup(http, db):
    db.execute.return_value = [(100, EVENT_A, payload())]
    response = http.get(f"{BASE}/events/{EVENT_A}", params=SCOPE)
    assert response.status_code == 200
    assert response.json() == json.loads(payload()) | {"event_id": EVENT_A}
    assert db.execute.call_args.args[1] == SCOPE | {"event_id": EVENT_A}


@pytest.mark.parametrize(
    "path",
    [f"/events/{EVENT_A}", "/sessions/session-1/events"],
)
def test_missing_event_or_bound(http, db, path):
    db.execute.return_value = []
    response = http.get(
        BASE + path,
        params=SCOPE | ({"start_event_id": EVENT_A} if "sessions" in path else {}),
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        SCOPE | {"tenant_id": ""},
        SCOPE | {"mas_id": ""},
        SCOPE | {"limit": 0},
        SCOPE | {"limit": 1001},
        SCOPE | {"start_time": "2026-10-16T09:00:00"},
        SCOPE | {"start_time": "2026-02-30T09:00:00Z"},
        SCOPE | {"start_time": "2026-10-16T09:00:00.1234567891Z"},
        SCOPE | {"start_event_id": "bad"},
        SCOPE | {"categories": "unsupported"},
        SCOPE | {"service_name": ""},
        SCOPE | {"unexpected_filter": "value"},
        SCOPE | {"start_time": "2026-10-16T09:00:00Z", "start_event_id": EVENT_A},
    ],
)
def test_invalid_query_is_rejected_before_reading(http, db, params):
    response = http.get(f"{BASE}/sessions/session-1/events", params=params)
    assert response.status_code == 422
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/sessions/session-1/events",
        "/spans/0123456789abcdef/events",
        f"/events/{EVENT_A}",
    ],
)
@pytest.mark.parametrize(("field", "value"), REMOVED_FILTERS.items())
def test_removed_query_fields_are_rejected(http, db, path, field, value):
    response = http.get(BASE + path, params=SCOPE | {field: value})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", field]
    db.execute.assert_not_called()


@pytest.mark.parametrize("path", ["/events/invalid", "/spans/invalid/events"])
def test_invalid_path_identifier(http, db, path):
    assert http.get(BASE + path, params=SCOPE).status_code == 422
    db.execute.assert_not_called()


def test_session_mode_is_only_accepted_on_span_endpoint(http, db):
    response = http.get(
        f"{BASE}/sessions/session-1/events",
        params=SCOPE | {"include_session_events": True},
    )
    assert response.status_code == 422
    db.execute.assert_not_called()


def test_invalid_session_mode_is_rejected(http, db):
    response = http.get(
        f"{BASE}/spans/0123456789abcdef/events",
        params=SCOPE | {"include_session_events": "invalid"},
    )
    assert response.status_code == 422
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "scope", [{}, {"tenant_id": "tenant-1"}, {"mas_id": "mas-1"}, SCOPE]
)
@pytest.mark.parametrize("endpoint", ["session", "span", "event", "bounds"])
def test_scope_filters_are_independently_optional(http, db, scope, endpoint):
    if endpoint == "event":
        path = f"/events/{EVENT_A}"
        db.execute.return_value = [(100, EVENT_A, payload())]
    elif endpoint == "span":
        path = "/spans/0123456789abcdef/events"
        db.execute.side_effect = [[("session-1",)], [(EVENT_A, payload())]]
    elif endpoint == "bounds":
        path = "/sessions/session-1/events"
        db.execute.side_effect = [
            [(100, EVENT_A, payload())],
            [(200, EVENT_B, payload())],
            [(EVENT_B, payload())],
        ]
    else:
        path = "/sessions/session-1/events"
        db.execute.return_value = [(EVENT_A, payload())]
    params = scope | (
        {
            "start_event_id": EVENT_A,
            "end_event_id": EVENT_B,
        }
        if endpoint == "bounds"
        else {}
    )
    response = http.get(BASE + path, params=params)
    assert response.status_code == 200
    for call in db.execute.call_args_list:
        sql, bound = call.args
        for field, column in (
            ("tenant_id", "tenant_id"),
            ("mas_id", "application_id"),
        ):
            if field in scope:
                assert bound[field] == scope[field]
                assert f"{column} = {{{field}:String}}" in sql
            else:
                assert field not in bound
                assert f"{{{field}:String}}" not in sql


def test_openapi_marks_scope_filters_optional(cognition_fabric_app):
    schema = cognition_fabric_app.openapi()
    for path in (
        "/sessions/{session_id}/events",
        "/spans/{span_id}/events",
        "/events/{event_id}",
    ):
        parameters = schema["paths"][BASE + path]["get"]["parameters"]
        assert not REMOVED_FILTERS.keys() & {p["name"] for p in parameters}
        scope_parameters = [
            p for p in parameters if p["name"] in ("tenant_id", "mas_id")
        ]
        assert len(scope_parameters) == 2
        assert all(p["in"] == "query" and not p["required"] for p in scope_parameters)
    parameters = schema["paths"][BASE + "/spans/{span_id}/events"]["get"]["parameters"]
    mode = next(p for p in parameters if p["name"] == "include_session_events")
    assert mode["required"] is False
    assert mode["schema"]["default"] is False
    assert set(schema["components"]["schemas"]["RealtimeEventPage"]["properties"]) == {
        "session_id",
        "events",
        "has_more",
    }


def test_clickhouse_fixed_string_ids_are_json_strings_in_pages_and_bounds(http, db):
    db.execute.side_effect = [
        [(100, EVENT_A.encode("ascii"), payload())],
        [(200, EVENT_C.encode("ascii"), payload())],
        [(EVENT_B.encode("ascii"), payload()), (EVENT_C.encode("ascii"), payload())],
    ]
    response = http.get(
        f"{BASE}/sessions/session-1/events",
        params=SCOPE
        | {
            "start_event_id": EVENT_A,
            "end_event_id": EVENT_C,
            "limit": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["events"][0]["event_id"] == EVENT_B
    assert set(body) == {"session_id", "events", "has_more"}
    assert body["has_more"] is True
    assert "data" in body["events"][0]
    bound = db.execute.call_args.args[1]
    assert bound["start_event_id"] == EVENT_A
    assert bound["end_event_id"] == EVENT_C


def test_clickhouse_fixed_string_id_is_a_json_string_in_single_event(http, db):
    db.execute.return_value = [(100, EVENT_A.encode("ascii"), payload())]
    response = http.get(f"{BASE}/events/{EVENT_A}", params=SCOPE)
    assert response.status_code == 200
    assert response.json()["event_id"] == EVENT_A


def test_clickhouse_fixed_string_id_in_span_history(http, db):
    db.execute.side_effect = [
        [("session-1",)],
        [(EVENT_A.encode("ascii"), payload())],
    ]
    response = http.get(f"{BASE}/spans/0123456789abcdef/events", params=SCOPE)
    assert response.status_code == 200
    assert response.json()["events"][0]["event_id"] == EVENT_A


def test_invalid_fixed_string_bytes_are_not_silently_replaced(http, db):
    db.execute.return_value = [(b"\xff" * 64, payload())]
    response = http.get(f"{BASE}/sessions/session-1/events", params=SCOPE)
    assert response.status_code == 500


def test_repeated_categories_and_types_are_bound_not_interpolated(http, db):
    db.execute.return_value = []
    params = list(SCOPE.items()) + [
        ("categories", "llm"),
        ("categories", "tool"),
        ("event_types", "llm.completed"),
        ("event_types", "tool.completed"),
        ("event_types", "x' OR 1=1 --"),
    ]
    response = http.get(f"{BASE}/sessions/session-1/events", params=params)
    assert response.status_code == 200
    sql, bound = db.execute.call_args.args
    assert "startsWith(event_type, 'llm.') OR startsWith(event_type, 'tool.')" in sql
    assert bound["event_types"] == [
        "llm.completed",
        "tool.completed",
        "x' OR 1=1 --",
    ]
    assert "x' OR 1=1 --" not in sql


def test_reversed_bounds_are_rejected(db):
    db.execute.side_effect = [
        [(200, EVENT_B, payload())],
        [(100, EVENT_A, payload())],
    ]
    with pytest.raises(ValidationError, match="must not follow"):
        LocalClient(db=db).get_realtime_events(
            session_id="session-1",
            query=RealtimeEventQuery(
                **SCOPE, start_event_id=EVENT_B, end_event_id=EVENT_A
            ),
        )
    assert db.execute.call_count == 2


def test_start_time_and_end_event_bound_preserve_nanoseconds(db):
    db.execute.side_effect = [[(1_792_141_200_123_456_789, EVENT_A, payload())], []]
    LocalClient(db=db).get_realtime_events(
        session_id="session-1",
        query=RealtimeEventQuery(
            **SCOPE,
            start_time="2026-10-16T09:00:00.123456Z",
            end_event_id=EVENT_A,
        ),
    )
    assert db.execute.call_count == 2
    assert db.execute.call_args.args[1]["start_time"] == "2026-10-16T09:00:00.123456Z"


def test_missing_anchor_must_belong_to_scoped_session(db):
    db.execute.return_value = []
    with pytest.raises(NotFoundError, match="scoped session"):
        LocalClient(db=db).get_realtime_events(
            session_id="session-1",
            query=RealtimeEventQuery(**SCOPE, start_event_id=EVENT_A),
        )
    assert db.execute.call_count == 1


def test_single_event_ambiguity_is_explicit(db):
    db.execute.return_value = [
        (100, EVENT_A, payload()),
        (200, EVENT_A, payload()),
    ]
    with pytest.raises(ValidationError, match="ambiguous"):
        LocalClient(db=db).get_realtime_event(
            event_id=EVENT_A, scope=EventScope(**SCOPE)
        )


def test_backend_errors_and_corrupt_payloads_are_not_empty_successes(http, db):
    db.execute.side_effect = RuntimeError("database unavailable")
    assert (
        http.get(f"{BASE}/sessions/session-1/events", params=SCOPE).status_code == 500
    )
    db.execute.side_effect = None
    db.execute.return_value = [(EVENT_A, "not json")]
    assert (
        http.get(f"{BASE}/sessions/session-1/events", params=SCOPE).status_code == 500
    )


def test_empty_session_returns_an_empty_page(http, db):
    db.execute.return_value = []
    response = http.get(f"{BASE}/sessions/missing/events", params=SCOPE)
    assert response.status_code == 200
    assert response.json()["events"] == []
    assert response.json()["has_more"] is False


def test_start_timestamp_query_is_parameterized():
    query = RealtimeEventQuery(**SCOPE, start_time="2026-10-16T10:00:00+01:00")
    sql, params = queries.history_query(query, "session-1", {})
    assert "parseDateTime64BestEffort({start_time:String}, 9, 'UTC')" in sql
    assert params["start_time"] == "2026-10-16T10:00:00+01:00"


def test_nanosecond_start_after_end_is_rejected(db):
    db.execute.return_value = [(1_792_141_200_123_456_789, EVENT_A, payload())]
    with pytest.raises(ValidationError, match="start_time must not follow"):
        LocalClient(db=db).get_realtime_events(
            session_id="session-1",
            query=RealtimeEventQuery(
                **SCOPE,
                start_time="2026-10-16T09:00:00.123456790Z",
                end_event_id=EVENT_A,
            ),
        )
