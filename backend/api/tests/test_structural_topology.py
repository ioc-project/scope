#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The topology of an application that declared no graph comes from the KG's structural agents."""

from __future__ import annotations

import pytest

from scope.api.api_v1.endpoints import applications as endpoints
from scope.client.applications import _get_application_structural_topology
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


AGENTS = [
    {
        "agentId": "currency_assistant",
        "agentName": "currency_assistant",
        "agentDescription": "Converts",
    },
    {
        "agentId": "rate_provider",
        "agentName": "rate_provider",
        "agentDescription": None,
    },
]


def test_the_agents_of_the_application_become_the_nodes() -> None:
    db = FakeDb(AGENTS)

    topology = _get_application_structural_topology(db, application_id="currency-demo")

    assert topology["edges"] == []
    assert set(topology["nodes"]) == {"currency_assistant", "rate_provider"}
    assistant = topology["nodes"]["currency_assistant"]
    assert (assistant["id"], assistant["name"], assistant["description"]) == (
        "currency_assistant",
        "currency_assistant",
        "Converts",
    )
    # same shape as a declared graph's node, so the UI renders both alike
    assert {"id", "name", "data", "metadata"} <= set(topology["nodes"]["rate_provider"])
    assert "description" not in topology["nodes"]["rate_provider"]
    assert db.calls[0][1] == {"applicationId": "currency-demo"}


def test_an_application_without_agents_has_no_topology() -> None:
    assert _get_application_structural_topology(FakeDb([]), application_id="x") == {}
    assert (
        _get_application_structural_topology(
            FakeDb([{"agentId": ""}]), application_id="x"
        )
        == {}
    )


def test_a_database_error_is_reported() -> None:
    with pytest.raises(DatabaseError, match="currency-demo"):
        _get_application_structural_topology(
            FakeDb(error=RuntimeError("down")), application_id="currency-demo"
        )


def test_the_query_is_scoped_to_the_application_through_its_mas() -> None:
    query, params = ui_queries.structural_agents_by_application_query(
        application_id="app-1"
    )

    assert "MAS {id: $applicationId}" in query and "belongsToMAS" in query
    assert "Processing" not in query and params == {"applicationId": "app-1"}


class FakeClient:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def get_application_topology(self, *, application_id):
        self.calls += 1
        return self.result

    def get_application_structural_topology(self, *, application_id):
        self.calls += 1
        return {"nodes": {"a": {}}, "edges": [], "from": "kg"}


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(endpoints.settings, "CACHE_ENABLED", False)


def _topology(declared):
    ui, kg = FakeClient(declared), FakeClient(None)
    result = endpoints.application_topology(
        "app", ui_client=ui, kg_client=kg, redis_client=None
    )
    return result, ui, kg


def test_a_declared_graph_is_returned_as_is_without_asking_the_kg() -> None:
    declared = {"nodes": {"moderator": {"id": "moderator"}}, "edges": []}

    result, _ui, kg = _topology(declared)

    assert result is declared and kg.calls == 0


@pytest.mark.parametrize("declared", [{}, None, {"edges": []}, {"nodes": {}}])
def test_without_a_declared_graph_the_structural_one_is_returned(declared) -> None:
    result, _ui, kg = _topology(declared)

    assert result["from"] == "kg" and kg.calls == 1


# ---------------------------------------------------------------- live topology of an application


def test_the_live_sessions_of_an_application_match_the_prefixed_event_session_ids() -> (
    None
):
    """Event session ids are "<application>_<uuid>"; the traces' session_id has no prefix."""
    from scope.query_builders import live_topology as lt

    sql = str(lt.get_live_topology_sessions(application_name="currency-demo"))

    assert "multiIf(position(LogAttributes['session.id'], '_') > 0" in sql
    assert " OR " in sql  # an id without prefix still matches
