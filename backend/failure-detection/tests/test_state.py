from conftest import rows, state, trajectory

from failure_detection import SymbolicState, build_delta
from failure_detection import models as m


def delta(st=None):
    return build_delta(st or state(), trajectory())


def test_elements_are_read_with_the_ontology_names():
    d = delta()
    claims = {c.id: c for c in d.claims}
    assert claims["c1"].text == "the fare is 5" and claims["c1"].generated_by == "L1"
    assert claims["c1"].kind == "factual"
    assert {f.id: f.grounded_in for f in d.facts} == {"f1": "T1"}
    assert d.goals[0].success_criteria == ("done",)
    assert d.constraints[0].source == "user" and d.constraints[0].strength == "hard"
    assert [(p.id, p.agent) for p in d.system_prompts] == [("sp-a", "agent-a")]


def test_standing_tasks_and_capabilities_are_defined_in_their_system_prompt():
    d = delta()
    tasks = {t.id: t for t in d.tasks}
    assert tasks["st-a"].standing and tasks["st-a"].system_prompt == "sp-a"
    assert not tasks["t1"].standing and tasks["t1"].owned_by == "A2"
    assert d.capabilities[0].system_prompt == "sp-a"


def test_relations_use_the_names_of_the_detectors():
    edges = {(e.source, e.relation, e.target) for e in delta().edges}
    assert ("c2", m.DERIVED_FROM, "c1") in edges
    assert ("c1", m.CONTRADICTS, "c2") in edges  # the ontology's conflictsWith
    assert ("t1", m.ACHIEVES, "g1") in edges
    assert not any(
        r in ("generatedBy", "ownedBy", "inputClaim") for _, r, _ in edges
    )  # fields, not edges


def test_calls_come_from_the_trajectory():
    calls = {c.id: c for c in delta().calls}
    assert (
        calls["L1"].parent == "A2" and calls["L1"].agent == "agent-a" and calls["A2"].parent is None
    )
    assert calls["L1"].end == 3.0
    assert calls["T1"].failed and not calls["L1"].failed
    assert calls["L2"].input_claims == ("c1",) and calls["L2"].input_facts == ("f1",)
    assert calls["A2"].routed_by == "A1"  # the KG handoff
    assert {c.id for c in delta().calls if c.final} == {
        "L2"
    }  # last LLM call of the final agent call


def test_the_state_of_an_agent_call_is_rebuilt():
    calls = {c.id: c for c in delta().calls}
    assert set(calls["A2"].state_claims) == {"c1", "c2"}  # read c1, wrote c1 and c2
    assert calls["A1"].state_claims == ()  # no LLM call and no predecessor


def test_a_task_is_compared_with_capabilities_when_its_agent_has_some():
    tasks = {t.id: t for t in delta().tasks}
    assert tasks["t1"].capability_checked  # agent-a has a capability
    assert not tasks["st-a"].capability_checked  # standing, no owner
    _, edges = rows()
    other = state(edges=[e for e in edges if e["source"] != "sp-a"])  # no prompt: no known agent
    assert not {t.id: t for t in delta(other).tasks}["t1"].capability_checked


def test_a_session_without_symbolic_state_has_no_elements():
    assert state().has_elements()
    assert not SymbolicState.from_rows([], []).has_elements()
