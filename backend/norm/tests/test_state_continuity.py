#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""A flow whose boundary States have the same content must be one chain in the graph.

When a call starts with what the previous step produced (an agent handing a tool its own input,
a tool returning what its agent returns, ...) the normalizer used to leave two separate State
nodes with identical content and no edge between them, so the chain could not be followed.
Those States are now merged. States whose content differs are still bridged.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
from scope_ontology.models.edges import (
    correspondsToTransition,
    hasFinalState,
    hasInitialState,
    hasState,
    inputTo,
    leadsTo,
)
from scope_ontology.models.nodes import ProcessingCall, State

from norm import load_otel_export, normalize, verify_kg
from norm.ioa_observe.registry import Registry
from norm.ioa_observe.trajectory import (
    add_state_pair_transition,
    bridge_states_if_mismatched,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "currency_demo.otel_export.jsonl"


def _flow(nodes, edges):
    """Nodes (States and Transitions) reachable from the session's first State through
    inputTo/leadsTo."""
    by_id = {n["id"]: n for n in nodes}
    forward = defaultdict(set)
    for e in edges:
        if e["edge_type"] in ("inputTo", "leadsTo"):
            forward[e["source_id"]].add(e["target_id"])
    start = next(
        e["target_id"]
        for e in edges
        if e["edge_type"] == "hasInitialState" and by_id[e["source_id"]]["node_type"] == "Session"
    )
    end = next(
        e["target_id"]
        for e in edges
        if e["edge_type"] == "hasFinalState" and by_id[e["source_id"]]["node_type"] == "Session"
    )
    seen, stack = set(), [start]
    while stack:
        item = stack.pop()
        if item not in seen:
            seen.add(item)
            stack.extend(forward[item])
    return seen, end


@pytest.fixture(scope="module")
def currency_kg():
    spans = load_otel_export(FIXTURE)
    nodes, edges = normalize(spans)
    return nodes, edges, spans


def test_every_state_is_on_the_chain_from_the_session_input(currency_kg) -> None:
    nodes, edges, _ = currency_kg
    reachable, final = _flow(nodes, edges)
    states = {n["id"] for n in nodes if n["node_type"] == "State"}

    assert states == {i for i in reachable if i in states}, (
        "States are cut off from the session's chain"
    )
    assert final in reachable


def test_a_tool_shares_its_boundary_states_with_its_agent(currency_kg) -> None:
    nodes, edges, _ = currency_kg
    by_id = {n["id"]: n for n in nodes}

    def boundary(call_name_part: str, edge_type: str) -> str:
        call = next(
            n["id"]
            for n in nodes
            if call_name_part in n.get("name", "") and n["node_type"].endswith("Call")
        )
        return next(
            e["target_id"] for e in edges if e["edge_type"] == edge_type and e["source_id"] == call
        )

    # lookup_rate is called with what rate_provider received and returns what it returns
    assert boundary("rate_provider call", "hasInitialState") == boundary(
        "lookup_rate", "hasInitialState"
    )
    assert boundary("rate_provider call", "hasFinalState") == boundary(
        "lookup_rate", "hasFinalState"
    )
    # convert_currency receives what currency_assistant received, but returns a number that is
    # not the sentence the agent writes: still two States, bridged
    assert boundary("currency_assistant call", "hasInitialState") == boundary(
        "convert_currency", "hasInitialState"
    )
    assert boundary("currency_assistant call", "hasFinalState") != boundary(
        "convert_currency", "hasFinalState"
    )
    assert by_id[boundary("convert_currency", "hasFinalState")]["content"] == "85.9"


def test_no_edge_points_at_a_state_that_was_merged_away(currency_kg) -> None:
    nodes, edges, _ = currency_kg
    ids = {n["id"] for n in nodes}

    assert all(e["source_id"] in ids and e["target_id"] in ids for e in edges)


def test_the_merged_graph_still_verifies(currency_kg) -> None:
    nodes, edges, spans = currency_kg
    report = verify_kg(nodes, edges, "currency-demo")

    for name, result in report["structural_checks"].items():
        if result.get("reason"):
            continue
        assert result["ok"], f"{name} failed: {result['violations'][:5]}"


# ---------------------------------------------------------------- the rule itself


def _two_calls(registry: Registry, first_out: str, second_in: str):
    add_state_pair_transition(registry, "a", "s", "span-a", "in a", first_out)
    add_state_pair_transition(registry, "b", "s", "span-b", second_in, "out b")
    return (
        registry.edge_target(hasFinalState, "a"),
        registry.edge_target(hasInitialState, "b"),
    )


def _bridge(registry, from_id, to_id, **kwargs):
    bridge_states_if_mismatched(
        registry,
        from_state_id=from_id,
        to_state_id=to_id,
        attach_to_id="parent",
        session_id="s",
        start_time=0.0,
        end_time=1.0,
        bridge_kind="test",
        **kwargs,
    )


def test_identical_states_are_merged_into_the_one_the_flow_comes_from() -> None:
    registry = Registry()
    final_a, initial_b = _two_calls(registry, "same", "same")

    _bridge(registry, final_a, initial_b)

    assert registry.get(State, initial_b) is None
    assert registry.edge_target(hasInitialState, "b") == final_a
    assert registry.edge_target(hasFinalState, "a") == final_a
    # b's transition now takes the merged State as input, and a's leads to it
    assert (
        inputTo,
        final_a,
        registry.edge_target(correspondsToTransition, "b"),
    ) in registry.edges
    assert any(t is leadsTo and tid == final_a for t, _s, tid in registry.edges)
    assert (hasState, "b", final_a) in registry.edges
    assert not registry.all_of(ProcessingCall)


def test_keep_to_keeps_the_outer_state() -> None:
    registry = Registry()
    final_a, initial_b = _two_calls(registry, "same", "same")

    _bridge(registry, final_a, initial_b, keep="to")

    assert registry.get(State, final_a) is None
    assert registry.edge_target(hasFinalState, "a") == initial_b


def test_different_states_are_still_bridged() -> None:
    registry = Registry()
    final_a, initial_b = _two_calls(registry, "one thing", "another thing")

    _bridge(registry, final_a, initial_b)

    assert registry.get(State, final_a) and registry.get(State, initial_b)
    assert len(registry.all_of(ProcessingCall)) == 1


def test_merging_twice_changes_nothing() -> None:
    registry = Registry()
    final_a, initial_b = _two_calls(registry, "same", "same")
    _bridge(registry, final_a, initial_b)
    edges, nodes = dict(registry.edges), dict(registry.nodes)

    _bridge(registry, final_a, final_a)  # now the same State
    _bridge(registry, final_a, initial_b)  # the dropped one is gone

    assert registry.edges == edges and registry.nodes == nodes


def test_merging_unrelated_equal_texts_elsewhere_is_not_done() -> None:
    """Only the pairs a heuristic compares are merged, not every State with the same text."""
    registry = Registry()
    add_state_pair_transition(registry, "x", "s", "span-x", "ok", "ok")
    add_state_pair_transition(registry, "y", "s", "span-y", "ok", "ok")

    assert len(registry.all_of(State)) == 4
