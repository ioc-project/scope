#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The neurosymbolic layer in the KG: the queries and the client (fake database)."""

from typing import Any

import pytest
from pydantic import ValidationError

from scope.client.neurosymbolic import (
    get_final_agent_call,
    get_mas_name,
    get_session_handoffs,
    get_symbolic_extraction_input,
    write_symbolic_state,
)
from scope.core.exceptions import DatabaseError, NotFoundError
from scope.query_builders import neurosymbolic as ns


class _Db:
    """Records the queries; ``missing`` ids are not in the KG, so they match nothing."""

    def __init__(self, missing=(), rows=None, fail=False):
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[str] = []
        self.missing = set(missing)
        self.rows = rows
        self.fail = fail

    def execute(self, query, params=None):
        if self.fail:
            raise RuntimeError("neo4j down")
        params = params or {}
        self.queries.append((query, params))
        if self.rows is not None:
            return self.rows
        if "RETURN n.id AS id" in query:
            return [{"id": i} for i in params["ids"] if i not in self.missing]
        rows = params.get("rows")
        if rows is not None and "row.from_id" in query:
            return [
                {
                    "written": sum(
                        1 for r in rows if not {r["from_id"], r["to_id"]} & self.missing
                    )
                }
            ]
        if rows is not None:
            return [{"written": len(rows)}]
        return [{"deleted": 7}]

    def execute_command(self, command, params=None):
        self.commands.append(command)


_PROPS = {
    "Claim": {"claimText": "fare 5", "claimForce": "assertive", "claimKind": "factual"},
    "Fact": {"factText": "5", "factSourceType": "tool_result"},
}


def _node(label="Claim", node_id="c1"):
    return {"label": label, "id": node_id, "props": dict(_PROPS[label])}


def _edge(type_="generatedBy", to_id="llmcall:l1"):
    return {
        "type": type_,
        "from_id": "c1",
        "from_label": "Claim",
        "to_id": to_id,
        "to_label": "LLMCall",
    }


def test_labels_cannot_be_injected():
    with pytest.raises(ValueError):
        ns.merge_nodes_query("Claim) DETACH DELETE (n", [])
    with pytest.raises(ValueError):
        ns.merge_edges_query("x", "Claim", "Fact) DETACH DELETE (n", [])
    assert ns.is_safe_identifier("supportedBy")


def test_write_indexes_merges_nodes_then_edges():
    db = _Db()
    report = write_symbolic_state(db, "s1", [_node(), _node("Fact", "f1")], [_edge()])

    assert report["nodes"] == {"Claim": 1, "Fact": 1}
    assert report["edges"] == {"generatedBy": 1}
    assert (
        report["missing_edges"] == {}
        and report["missing_nodes"] == {}
        and report["deleted"] == 0
    )
    assert (
        sorted(db.commands) == sorted(set(db.commands)) and len(db.commands) == 2
    )  # one index per label
    kinds = [
        "node" if "MERGE (n:" in q else "edge" for q, _ in db.queries if "MERGE" in q
    ]
    assert kinds == ["node", "node", "edge"]


def test_a_missing_kg_node_is_reported_and_its_edge_not_counted():
    report = write_symbolic_state(
        _Db(missing={"llmcall:l1"}), "s1", [_node()], [_edge()]
    )

    assert report["missing_nodes"] == {"LLMCall": ["llmcall:l1"]}
    assert report["missing_edges"] == {"generatedBy": 1}
    assert report["edges"] == {"generatedBy": 0}


def test_replace_cleans_only_the_symbolic_labels_of_the_session():
    db = _Db()
    report = write_symbolic_state(db, "s1", [_node()], [], replace=True)

    clean, params = db.queries[0]
    assert "DETACH DELETE" in clean and params == {"session_id": "s1"}
    assert report["deleted"] == 7
    labels = clean.split("MATCH (n:")[1].split(")")[0].split("|")
    assert sorted(labels) == sorted(
        ns.SYMBOLIC_LABELS
    )  # never an original node of the KG


def test_the_session_id_is_added_to_every_node_and_is_not_an_ontology_property():
    db = _Db()
    write_symbolic_state(db, "s1", [_node()], [])

    [(_, params)] = [(q, p) for q, p in db.queries if "MERGE (n:" in q]
    assert params["rows"] == [
        {"id": "c1", "props": {**_PROPS["Claim"], "sessionId": "s1"}}
    ]
    with pytest.raises(
        ValidationError
    ):  # sessionId is the writer's, not part of the ontology's model
        write_symbolic_state(
            db, "s1", [{**_node(), "props": {**_PROPS["Claim"], "sessionId": "s1"}}], []
        )


def test_properties_must_fit_the_ontology_model_of_the_label():
    db = _Db()
    no_text = {"label": "Claim", "id": "c1", "props": {"claimForce": "assertive"}}
    unknown = {**_node(), "props": {**_PROPS["Claim"], "colour": "red"}}
    with pytest.raises(ValidationError):
        write_symbolic_state(db, "s1", [no_text], [])
    with pytest.raises(ValidationError):
        write_symbolic_state(db, "s1", [unknown], [])
    assert db.queries == [] and db.commands == []  # nothing written


def test_only_the_labels_of_the_layer_are_written():
    with pytest.raises(ValueError):
        write_symbolic_state(
            _Db(), "s1", [{"label": "LLMCall", "id": "l1", "props": {}}], []
        )
    bad_end = {**_edge(), "to_label": "Session"}
    with pytest.raises(ValueError):
        write_symbolic_state(_Db(), "s1", [], [bad_end])
    with pytest.raises(ValueError):
        write_symbolic_state(_Db(), "s1", [], [_edge("x]->() DETACH DELETE (n //")])
    with pytest.raises(
        ValueError
    ):  # a safe name, but not a relationship of the ontology
        write_symbolic_state(_Db(), "s1", [], [_edge("knows")])


def test_a_blank_session_id_is_refused():
    with pytest.raises(ValueError):
        write_symbolic_state(_Db(), " ", [], [])


def test_database_failures_are_reported():
    with pytest.raises(DatabaseError):
        write_symbolic_state(_Db(fail=True), "s1", [_node()], [])


def test_handoffs_are_listed_once_per_pair_in_a_stable_order():
    rows = [
        {"source": "b", "target": "c", "kind": "shared_state"},
        {"source": "a", "target": "b", "kind": "handoff bridge"},
        {"source": "a", "target": "b", "kind": "handoff_fallback"},
    ]
    assert get_session_handoffs(_Db(rows=rows), "s1") == [
        {"from_call": "a", "to_call": "b", "kind": "handoff bridge"},
        {"from_call": "b", "to_call": "c", "kind": "shared_state"},
    ]
    assert get_session_handoffs(_Db(rows=[]), "s1") == []


def test_final_agent_call_and_mas_name():
    assert (
        get_final_agent_call(_Db(rows=[{"id": "z"}, {"id": "a"}, {"id": None}]), "s1")
        == "a"
    )
    assert get_final_agent_call(_Db(rows=[]), "s1") is None
    assert get_mas_name(_Db(rows=[{"name": "trip"}]), "s1") == "trip"
    assert get_mas_name(_Db(rows=[]), "s1") is None


PAYLOAD = {"session_id": "s1", "session": {"sessionId": "s1"}, "agent_spans": []}
TRAJECTORY = {
    "session_id": "s1",
    "steps": [{"call_id": "agentcall:a", "kind": "agent"}],
}


class _Provider:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def fetch(self, resource_id, requirements):
        self.calls.append((resource_id, requirements))
        return self.payload


@pytest.fixture
def source(monkeypatch):
    """The session payload and trajectory the KG would give; returns the provider to inspect its calls."""
    provider = _Provider(PAYLOAD)
    monkeypatch.setattr("scope.providers.SCOPEKGProvider", lambda db: provider)
    monkeypatch.setattr(
        "scope.client.dal.get_session_trajectory", lambda db, sid: dict(TRAJECTORY)
    )
    return provider


def test_the_extraction_input_is_the_session_and_its_trajectory_with_handoffs_and_final_call(
    source,
):
    rows = [{"source": "a", "target": "b", "kind": "handoff", "id": "agentcall:b"}]
    result = get_symbolic_extraction_input(_Db(rows=rows), "s1")

    assert source.calls == [("s1", None)]  # the provider's full-session fetch
    assert result["session"] == PAYLOAD
    assert result["trajectory"]["steps"] == TRAJECTORY["steps"]
    assert result["trajectory"]["handoffs"] == [
        {"from_call": "a", "to_call": "b", "kind": "handoff"}
    ]
    assert result["trajectory"]["final_agent_call"] == "agentcall:b"


def test_a_failing_handoff_or_final_call_query_leaves_the_key_out(source):
    result = get_symbolic_extraction_input(_Db(fail=True), "s1")

    assert (
        "handoffs" not in result["trajectory"]
        and "final_agent_call" not in result["trajectory"]
    )
    assert result["trajectory"]["steps"] == TRAJECTORY["steps"]


def test_a_session_the_kg_does_not_have_is_not_found(monkeypatch):
    monkeypatch.setattr("scope.providers.SCOPEKGProvider", lambda db: _Provider({}))
    with pytest.raises(NotFoundError):
        get_symbolic_extraction_input(_Db(), "unknown")
