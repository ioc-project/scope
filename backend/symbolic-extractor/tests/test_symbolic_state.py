import json
from pathlib import Path

import pytest

from symbolic_extractor import SymbolicSessionStore
from symbolic_extractor.extractors.claims import Claim
from symbolic_extractor.output import read_trajectory_file
from symbolic_extractor.symbolic_state import (
    build_transitions,
    input_texts,
    route_decision,
    track_symbolic_state,
)

SYSTEM = json.dumps([{"type": "text", "content": "You are a bot. Paris is nice."}])


def llm_input(*texts: str) -> str:
    """Input text as stored in the KG: system array, blank line, messages array."""
    messages = [{"role": "user", "parts": [{"type": "text", "content": t}]} for t in texts]
    return f"{SYSTEM}\n\n{json.dumps(messages)}"


def agent(call_id, name, start, goto=None):
    output = (
        json.dumps({"type": "Command", "goto": goto, "update": {"next": goto}})
        if goto
        else "(no content recorded)"
    )
    return {
        "call_id": call_id,
        "kind": "agent",
        "agent_id": name,
        "name": name,
        "parent_id": "",
        "start_time": start,
        "output_text": output,
    }


def llm(call_id, owner, start, input_text, duration_ms=100.0):
    return {
        "call_id": call_id,
        "kind": "llm",
        "agent_id": "x",
        "parent_id": owner,
        "start_time": start,
        "duration_ms": duration_ms,
        "input_text": input_text,
    }


def store_with(**claims):
    """claims: claim_id=(llm_call_id, verbatim_source)."""
    store = SymbolicSessionStore("s")
    for cid, (node, snippet) in claims.items():
        store.add(
            "claims",
            Claim(
                id=cid,
                claim_text=cid,
                claim_force="assertive",
                claim_kind="factual",
                verbatim_source=snippet,
                generated_by=node,
            ),
            node,
        )
    return store


def test_route_decision():
    assert route_decision('{"type": "Command", "goto": "a", "update": {"next": "a"}}') == (
        "a",
        False,
    )
    assert route_decision('{"goto": "__end__"}') == ("__end__", True)
    assert route_decision('{"update": {"next_agent": "FINISH"}}') == ("FINISH", True)
    assert route_decision('{"type": "Command", "goto": "b", "upd') == ("b", False)  # truncated
    assert route_decision("(no content recorded)") is None
    assert route_decision(None) is None
    assert route_decision('{"result": 1}') is None


def test_input_texts_skips_system_and_decodes_both_arrays():
    texts = input_texts(llm_input("Hello   world\nsecond"))
    assert texts == ["Hello world second"]
    assert not any("Paris" in t for t in texts)  # system prompt ignored


def test_input_texts_includes_tool_responses_and_plain_fallback():
    messages = [
        {
            "role": "tool",
            "parts": [{"type": "tool_call_response", "id": "1", "response": "Fare: $75"}],
        }
    ]
    assert input_texts(f"{SYSTEM}\n\n{json.dumps(messages)}") == ["Fare: $75"]
    assert input_texts("not json  at all") == ["not json at all"]
    assert input_texts("") == []


def test_transitions_router_worker_roundtrip_and_end():
    steps = [
        agent("m1", "moderator", 1, goto="w"),
        agent("w1", "w", 2),
        agent("m2", "moderator", 3, goto="w"),
        agent("w2", "w", 4),
        agent("m3", "moderator", 5, goto="__end__"),
    ]
    pairs = [(t.from_call, t.to_call, t.via) for t in build_transitions(steps)]
    assert pairs == [
        ("m1", "w1", "route"),
        ("m2", "w2", "route"),
        ("w1", "m2", "return"),
        ("w2", "m3", "return"),
    ]


def test_transitions_fallback_sequence_but_not_after_end():
    steps = [
        agent("a", "a", 1),
        agent("b", "b", 2),
        agent("r", "r", 3, goto="end"),
        agent("c", "c", 4),
    ]
    pairs = [(t.from_call, t.to_call, t.via) for t in build_transitions(steps)]
    assert pairs == [("a", "b", "sequence"), ("b", "r", "sequence")]


def _triples(transitions):
    return [(t.from_call, t.to_call, t.via) for t in transitions]


def test_kg_handoffs_come_first_and_heuristics_cover_the_rest():
    steps = [agent("a", "a", 1), agent("b", "b", 2), agent("c", "c", 3), agent("d", "d", 4)]
    handoffs = [
        {"from_call": "a", "to_call": "c", "kind": "handoff"},
        {"from_call": "c", "to_call": "b", "kind": "shared_state"},  # backwards: dropped
        {"from_call": "a", "to_call": "c", "kind": "handoff"},  # duplicate
        {"from_call": "zz", "to_call": "d", "kind": "handoff"},  # unknown call
    ]
    # c is covered by the KG; b and d have no usable handoff and follow the previous call
    assert _triples(build_transitions(steps, handoffs)) == [
        ("a", "c", "handoff"),
        ("a", "b", "sequence"),
        ("c", "d", "sequence"),
    ]


def test_kg_handoff_fallback_kind_is_ignored():
    steps = [agent("a", "a", 1), agent("b", "b", 2), agent("c", "c", 3)]
    handoffs = [{"from_call": "a", "to_call": "c", "kind": "handoff_fallback"}]
    assert _triples(build_transitions(steps, handoffs)) == _triples(build_transitions(steps))


def test_kg_handoff_replaces_the_return_guess():
    steps = [
        agent("m1", "moderator", 1, goto="w"),
        agent("w1", "w", 2),
        agent("m2", "moderator", 3, goto="w"),
        agent("w2", "w", 4),
    ]
    handoffs = [{"from_call": "w1", "to_call": "m2", "kind": "handoff"}]
    assert _triples(build_transitions(steps, handoffs)) == [
        ("w1", "m2", "handoff"),
        ("m1", "w1", "route"),
        ("m2", "w2", "route"),
    ]


def test_no_handoffs_means_heuristics_only():
    steps = [
        agent("m1", "moderator", 1, goto="w"),
        agent("w1", "w", 2),
        agent("m2", "moderator", 3, goto="__end__"),
    ]
    assert _triples(build_transitions(steps, [])) == _triples(build_transitions(steps, None))
    assert _triples(build_transitions(steps)) == [("m1", "w1", "route"), ("w1", "m2", "return")]


def test_claims_flow_follows_kg_handoffs():
    steps = [
        agent("a", "a", 1),
        llm("l_a", "a", 1.1, llm_input("question")),
        agent("b", "b", 2),
        llm("l_b", "b", 2.1, llm_input("question", "Fact A is true.")),
    ]
    store = store_with(ca=("l_a", "Fact A is true."))
    trace = track_symbolic_state(
        steps, store, handoffs=[{"from_call": "a", "to_call": "b", "kind": "handoff"}]
    )
    assert trace.input_claims["l_b"] == ["ca"]
    assert [t.via for t in trace.transitions] == ["handoff"]


def test_claims_flow_router_worker_router_and_only_present_ones_transfer():
    steps = [
        agent("m1", "mod", 1, goto="w"),
        llm("l_m1", "m1", 1.1, llm_input("question")),
        agent("w1", "w", 2),
        llm("l_w1", "w1", 2.1, llm_input("question", "Fact A is true.")),
        agent("m2", "mod", 3),
    ]
    steps[-1]["output_text"] = json.dumps({"goto": "__end__"})
    steps.append(llm("l_m2", "m2", 3.1, llm_input("question", "Answer: B holds.")))
    store = store_with(
        cm1=("l_m1", "Fact A is true."),  # claim of the router call
        cw1=("l_w1", "Answer: B holds."),
    )
    trace = track_symbolic_state(steps, store)
    assert trace.input_claims["l_m1"] == []
    assert trace.input_claims["l_w1"] == ["cm1"]  # transferred: present in first LLM input
    assert trace.input_claims["l_m2"] == ["cw1"]  # cm1 not in m2's input, so it is dropped
    assert trace.agent_states["w1"] == ["cm1", "cw1"]
    assert trace.agent_states["m2"] == ["cw1"]


def test_state_evolves_inside_one_agent_call():
    steps = [
        agent("a", "a", 1),
        llm("l1", "a", 1.0, llm_input("q"), duration_ms=100),
        llm("l2", "a", 2.0, llm_input("q", "First finding."), duration_ms=100),
        llm("l3", "a", 3.0, llm_input("q", "First finding.", "Second finding."), duration_ms=100),
    ]
    store = store_with(c1=("l1", "First finding."), c2=("l2", "Second finding."))
    trace = track_symbolic_state(steps, store)
    assert trace.input_claims["l1"] == []
    assert trace.input_claims["l2"] == ["c1"]
    assert trace.input_claims["l3"] == ["c1", "c2"]
    assert trace.agent_states["a"] == ["c1", "c2"]


def test_overlapping_llm_calls_do_not_see_each_other():
    steps = [
        agent("a", "a", 1),
        llm("l1", "a", 1.0, llm_input("q"), duration_ms=2000),
        llm("l2", "a", 1.5, llm_input("q", "First finding."), duration_ms=100),
    ]
    store = store_with(c1=("l1", "First finding."))
    trace = track_symbolic_state(steps, store)
    assert trace.input_claims["l2"] == []  # l1 had not finished when l2 started


def test_claim_in_system_prompt_or_without_snippet_is_not_an_input_claim():
    steps = [
        agent("a", "a", 1),
        llm("l1", "a", 1.0, llm_input("q")),
        llm("l2", "a", 2.0, llm_input("q")),
    ]
    store = store_with(c1=("l1", "Paris is nice."), c2=("l1", None))
    trace = track_symbolic_state(steps, store)
    assert trace.input_claims["l2"] == []
    assert trace.agent_states["a"] == ["c1", "c2"]  # still part of the state


def test_snippet_with_line_breaks_and_json_escapes_matches():
    steps = [
        agent("a", "a", 1),
        llm("l1", "a", 1.0, llm_input("q")),
        llm("l2", "a", 2.0, llm_input('He said "hi"\n\n- item   one')),
    ]
    store = store_with(c1=("l1", 'He said "hi" - item one'))
    assert track_symbolic_state(steps, store).input_claims["l2"] == ["c1"]


def test_agent_without_llm_calls_passes_state_through():
    steps = [
        agent("a", "a", 1, goto="b"),
        llm("l1", "a", 1.1, llm_input("q")),
        agent("b", "b", 2),  # no LLM calls
        agent("c", "c", 3),
        llm("l3", "c", 3.1, llm_input("x", "Claim text.")),
    ]
    store = store_with(c1=("l1", "Claim text."))
    trace = track_symbolic_state(steps, store)
    assert trace.agent_states["b"] == ["c1"]
    assert trace.input_claims["l3"] == ["c1"]


REAL = (
    Path(__file__).parent.parent / "input" / "trajectory_eeec0920-92ed-4ced-90d9-33734edfea9d.json"
)


@pytest.mark.skipif(not REAL.exists(), reason="saved trajectory not available")
def test_real_trajectory_transitions():
    steps = read_trajectory_file(REAL).steps
    transitions = build_transitions([s for s in steps if s["kind"] == "agent"])
    assert [t.via for t in transitions].count("route") == 5
    assert [t.via for t in transitions].count("return") == 5
    assert "sequence" not in [t.via for t in transitions]
