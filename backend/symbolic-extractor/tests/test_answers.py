"""``answers`` (Claim -> Goal): the claims of the LLM call that wrote the final answer answer the goal."""

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.models import AnnotatedNode, Claim, Goal, SessionTrajectory
from symbolic_extractor.store import ANSWERS, SymbolicSessionStore

STEPS = [
    {"call_id": "agentcall:entry", "kind": "agent", "parent_id": ""},
    {"call_id": "llmcall:early", "kind": "llm", "parent_id": "agentcall:entry"},
    {"call_id": "agentcall:final", "kind": "agent", "parent_id": ""},
    {"call_id": "llmcall:tools", "kind": "llm", "parent_id": "agentcall:final"},
    {"call_id": "llmcall:answer", "kind": "llm", "parent_id": "agentcall:final"},
    {"call_id": "agentcall:late", "kind": "agent", "parent_id": ""},
    {"call_id": "llmcall:late", "kind": "llm", "parent_id": "agentcall:late"},
]


def _claim(claim_id, call):
    return Claim(id=claim_id, claim_text=claim_id, claim_force="committing", generated_by=call)


def _setup(final_agent_call="agentcall:final"):
    store = SymbolicSessionStore("s1")
    store.add(
        "goals", Goal(id="g1", goal_text="Plan the trip", success_criteria=["a plan"]), "conv"
    )
    for claim_id, call in [
        ("c1", "llmcall:early"),
        ("c2", "llmcall:answer"),
        ("c3", "llmcall:answer"),
        ("c4", "llmcall:late"),
    ]:
        store.add("claims", _claim(claim_id, call), call)
    trajectory = SessionTrajectory(session_id="s1", steps=STEPS, final_agent_call=final_agent_call)
    extractor = SymbolicExtractor()
    extractor.store, extractor.trajectory = store, trajectory
    return extractor, store, trajectory


def _node(*claim_ids):
    return AnnotatedNode(
        node_type="LLMCall",
        node={"id": "n"},
        symbolic_state_elements={"claims": [{"id": c} for c in claim_ids]},
    )


def test_final_llm_call_is_the_last_llm_call_of_the_final_agent_call():
    assert (
        SessionTrajectory(
            session_id="s", steps=STEPS, final_agent_call="agentcall:final"
        ).final_llm_call()
        == "llmcall:answer"
    )
    # the call that starts last is not the one that delivers the answer
    assert (
        SessionTrajectory(
            session_id="s", steps=STEPS, final_agent_call="agentcall:entry"
        ).final_llm_call()
        == "llmcall:early"
    )


def test_final_llm_call_is_unknown_without_final_agent_call_or_llm_call():
    assert SessionTrajectory(session_id="s", steps=STEPS).final_llm_call() is None
    assert (
        SessionTrajectory(
            session_id="s", steps=STEPS, final_agent_call="agentcall:nope"
        ).final_llm_call()
        is None
    )


def test_claims_of_the_final_llm_call_answer_the_goal():
    extractor, store, trajectory = _setup()
    extractor._link_answers(store, trajectory)

    assert store.related(ANSWERS, "c2") == ["g1"] and store.related(ANSWERS, "c3") == ["g1"]
    # the earlier tool-use turn of the final agent call and the other calls are not part of it
    assert store.related(ANSWERS, "c1") == [] and store.related(ANSWERS, "c4") == []


def test_nothing_is_linked_when_the_final_agent_call_is_unknown():
    extractor, store, trajectory = _setup(final_agent_call=None)
    extractor._link_answers(store, trajectory)
    extractor._link_answers(store, None)
    assert all(store.related(ANSWERS, c) == [] for c in ("c1", "c2", "c3", "c4"))


def test_claims_of_the_nodes_get_their_answers_list():
    extractor, store, trajectory = _setup()
    extractor._link_answers(store, trajectory)
    nodes = [_node("c1"), _node("c2", "c3")]
    extractor.annotate_answers(nodes)

    assert [c["answers"] for c in nodes[0].symbolic_state_elements["claims"]] == [[]]
    assert [c["answers"] for c in nodes[1].symbolic_state_elements["claims"]] == [["g1"], ["g1"]]


def test_claims_have_no_answers_key_when_the_final_agent_call_is_unknown():
    extractor, store, _ = _setup(final_agent_call=None)
    nodes = [_node("c2")]
    extractor.annotate_answers(nodes)
    assert "answers" not in nodes[0].symbolic_state_elements["claims"][0]
