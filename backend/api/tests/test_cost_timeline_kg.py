#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The totalTokens / totalCost timelines of an application are computed from the KG's LLMCall nodes."""

from __future__ import annotations

import pytest

from scope.client.applications import _get_application_cost_timeline
from scope.client.constants import COST_PER_TOKEN, PERIODS_COUNT
from scope.core.exceptions import DatabaseError
from scope.query_builders import ui as ui_queries


class FakeDb:
    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.calls = list(rows), error, []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if self.error:
            raise self.error
        return self.rows


def test_sessions_are_bucketed_and_averaged() -> None:
    # 12 buckets of 100s over [0, 1200]
    db = FakeDb(
        [
            {"sessionId": "a", "startTime": 10.0, "tokens": 1000},
            {"sessionId": "b", "startTime": 50.0, "tokens": 3000},
            {"sessionId": "c", "startTime": 1150.0, "tokens": 400000},
        ]
    )

    tokens, cost = _get_application_cost_timeline(
        db, application_id="app", start_time="0", end_time="1200"
    )

    assert len(tokens) == len(cost) == PERIODS_COUNT
    assert tokens[0].sessionIDs == ["a", "b"]
    assert tokens[0].value.value == 2000
    assert cost[0].value.value == pytest.approx(2000 * COST_PER_TOKEN)
    assert cost[0].value.unit == "DOLLAR"
    assert cost[-1].sessionIDs == ["c"]
    assert cost[-1].value.value == pytest.approx(400000 * COST_PER_TOKEN)
    assert cost[1].sessionIDs == [] and cost[1].value.value == 0


def test_cost_is_not_flat_across_buckets() -> None:
    db = FakeDb(
        [
            {"sessionId": "a", "startTime": 10.0, "tokens": 1000},
            {"sessionId": "b", "startTime": 250.0, "tokens": 9000},
        ]
    )
    _, cost = _get_application_cost_timeline(
        db, application_id="app", start_time="0", end_time="1200"
    )
    assert len({c.value.value for c in cost}) > 1


def test_the_query_is_scoped_to_the_application_and_range() -> None:
    db = FakeDb()
    _get_application_cost_timeline(
        db, application_id="app", start_time="0", end_time="1200"
    )
    query, params = db.calls[0]
    assert "LLMCall" in query
    assert params == {"applicationId": "app", "startEpoch": 0.0, "endEpoch": 1200.0}
    assert query == ui_queries.application_llm_usage_by_session_query(
        application_id="app", start_epoch=0.0, end_epoch=1200.0
    )[0]


def test_database_errors_are_wrapped() -> None:
    with pytest.raises(DatabaseError):
        _get_application_cost_timeline(
            FakeDb(error=RuntimeError("boom")), application_id="app"
        )


# ── session stats (sessions count + average session duration) ────────────────

from scope.client.applications import _get_application_session_stats  # noqa: E402


def test_session_stats_come_from_the_session_nodes() -> None:
    db = FakeDb(
        [
            {
                "sessions": 3,
                "avgDurationMs": 1234.5678,
                "avgLlmCalls": 4.3333,
                "avgToolCalls": 2.0,
                "avgErrors": 0.6667,
            }
        ]
    )

    stats = _get_application_session_stats(
        db, application_id="app", start_time="0", end_time="1200"
    )

    assert stats == {
        "sessions": 3.0,
        "avgDurationMs": 1234.57,
        "avgLlmCalls": 4.33,
        "avgToolCalls": 2.0,
        "avgErrors": 0.67,
    }
    query, params = db.calls[0]
    assert "s.duration" in query and "LLMCall" in query and "ToolCall" in query
    assert "success = false" in query
    assert params == {"applicationId": "app", "startEpoch": 0.0, "endEpoch": 1200.0}


def test_session_stats_are_zero_without_sessions() -> None:
    zeros = {
        "sessions": 0.0,
        "avgDurationMs": 0.0,
        "avgLlmCalls": 0.0,
        "avgToolCalls": 0.0,
        "avgErrors": 0.0,
    }
    db = FakeDb(
        [
            {
                "sessions": 0,
                "avgDurationMs": None,
                "avgLlmCalls": None,
                "avgToolCalls": None,
                "avgErrors": None,
            }
        ]
    )
    assert _get_application_session_stats(db, application_id="app") == zeros
    assert _get_application_session_stats(FakeDb([]), application_id="app") == zeros


def test_session_stats_errors_are_wrapped() -> None:
    with pytest.raises(DatabaseError):
        _get_application_session_stats(
            FakeDb(error=RuntimeError("boom")), application_id="app"
        )


# ── conversation count ───────────────────────────────────────────────────────

from scope.client.applications import _get_application_conversation_count  # noqa: E402


def test_conversations_are_agent_session_pairs_with_an_llm_call() -> None:
    db = FakeDb([{"conversations": 7}])

    count = _get_application_conversation_count(
        db, application_id="app", start_time="0", end_time="1200"
    )

    assert count == 7.0
    query, params = db.calls[0]
    assert "hasLLMCall" in query and "executesAgent" in query
    assert "count(DISTINCT [s.sessionId, a.id])" in query
    assert params == {"applicationId": "app", "startEpoch": 0.0, "endEpoch": 1200.0}


def test_conversations_are_zero_without_rows() -> None:
    assert _get_application_conversation_count(FakeDb([]), application_id="app") == 0.0
    assert (
        _get_application_conversation_count(
            FakeDb([{"conversations": None}]), application_id="app"
        )
        == 0.0
    )


def test_conversation_count_errors_are_wrapped() -> None:
    with pytest.raises(DatabaseError):
        _get_application_conversation_count(
            FakeDb(error=RuntimeError("boom")), application_id="app"
        )


# ── most active agent (self time) ────────────────────────────────────────────

from scope.client.applications import (  # noqa: E402
    _agent_self_times,
    _get_application_most_active_agent,
)


def test_nested_agent_time_is_not_credited_to_the_enclosing_agent() -> None:
    # supervisor runs [0, 10]; worker runs inside it, [2, 8]
    times = _agent_self_times([("supervisor", 0, 10), ("worker", 2, 8)])
    assert times == {"supervisor": 4.0, "worker": 6.0}


def test_sequential_and_deeply_nested_calls() -> None:
    times = _agent_self_times(
        [
            ("a", 0, 10),
            ("b", 1, 6),
            ("c", 2, 4),  # nested in b, itself nested in a
            ("d", 12, 15),  # after a: not nested
        ]
    )
    assert times == {"a": 5.0, "b": 3.0, "c": 2.0, "d": 3.0}
    assert sum(times.values()) == pytest.approx(10 + 3)


def test_a_call_ending_after_its_enclosing_call_is_clamped() -> None:
    times = _agent_self_times([("a", 0, 10), ("b", 5, 20)])
    assert times == {"a": 5.0, "b": 5.0}


def test_the_most_active_agent_is_the_one_with_the_most_self_time() -> None:
    db = FakeDb(
        [
            # supervisor encloses the worker: 4s of its own, the worker has 6s
            {"sessionId": "s1", "agentId": "sup", "agentName": "super-visor", "startTime": 0.0, "endTime": 10.0},
            {"sessionId": "s1", "agentId": "wrk", "agentName": "worker", "startTime": 2.0, "endTime": 8.0},
        ]
    )

    name, share = _get_application_most_active_agent(
        db, application_id="app", start_time="0", end_time="1200"
    )

    assert name == "Worker"
    assert share == 60.0
    query, params = db.calls[0]
    assert "hasAgentCall" in query and "executesAgent" in query
    assert params == {"applicationId": "app", "startEpoch": 0.0, "endEpoch": 1200.0}


def test_sessions_are_not_nested_into_each_other() -> None:
    db = FakeDb(
        [
            {"sessionId": "s1", "agentId": "a", "agentName": "a", "startTime": 0.0, "endTime": 10.0},
            {"sessionId": "s2", "agentId": "b", "agentName": "b", "startTime": 2.0, "endTime": 5.0},
        ]
    )
    name, share = _get_application_most_active_agent(db, application_id="app")
    assert (name, share) == ("A", pytest.approx(10 / 13 * 100, abs=0.01))


def test_no_agent_calls_means_no_most_active_agent() -> None:
    assert _get_application_most_active_agent(FakeDb([]), application_id="app") == ("", 0.0)


def test_most_active_agent_errors_are_wrapped() -> None:
    with pytest.raises(DatabaseError):
        _get_application_most_active_agent(
            FakeDb(error=RuntimeError("boom")), application_id="app"
        )
