"""``produced_by`` (Claim -> Task) and ``TaskClaimRelationExtractor``: which claims fulfil their task."""

import pytest

from symbolic_extractor import SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AnnotatedNode, Claim, ExtractionResult, SessionTrajectory, Task
from symbolic_extractor.relations import FULFILS_TASK, TaskClaimRelationExtractor
from symbolic_extractor.relations.task_claim_relations import SYSTEM_PROMPT, render_prompt
from symbolic_extractor.store import PRODUCED_BY
from symbolic_extractor.usage import LLMUsage

STEPS = [
    {"call_id": "a1", "kind": "agent", "parent_id": ""},
    {"call_id": "l1", "kind": "llm", "parent_id": "a1"},
    {"call_id": "l2", "kind": "llm", "parent_id": "a1"},
    {"call_id": "a2", "kind": "agent", "parent_id": ""},
    {"call_id": "l3", "kind": "llm", "parent_id": "a2"},
    {"call_id": "a3", "kind": "agent", "parent_id": ""},
    {"call_id": "l4", "kind": "llm", "parent_id": "a3"},
]
TASK = Task(id="t1", task_text="Find a hotel in Lisbon.", acceptance_criteria=["under 150 EUR"], owned_by="a1")


class JudgeLLM:
    """Answers the replies given, in order (an exception is raised, a dict is the JSON reply)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete_json(self, system, user):
        assert system == SYSTEM_PROMPT
        self.prompts.append(user)
        reply = self.replies[len(self.prompts) - 1]
        if isinstance(reply, Exception):
            raise reply
        return LLMReply(data=reply, usage=LLMUsage(calls=1, prompt_tokens=10))


def _claim(claim_id, text, call):
    return Claim(id=claim_id, claim_text=text, claim_force="committing", generated_by=call)


def _store(linked=True):
    """Task t1 (agent call a1, LLM calls l1 and l2), task t2 (a2, l3), no task for a3 (l4)."""
    store = SymbolicSessionStore("s1")
    store.add("tasks", TASK, "a1")
    store.add("tasks", Task(id="t2", task_text="Book a flight.", owned_by="a2"), "a2")
    for claim_id, text, call in [
        ("c1", "Searching hotels.", "l1"),
        ("c2", "Hotel Alfama costs 120 EUR.", "l2"),
        ("c3", "I recommend Hotel Alfama.", "l2"),
        ("c4", "A flight leaves at 9.", "l3"),
        ("c5", "Routing to the next agent.", "l4"),
    ]:
        store.add("claims", _claim(claim_id, text, call), call)
    trajectory = SessionTrajectory(session_id="s1", steps=STEPS)
    extractor = SymbolicExtractor(relation_extractors=[])
    extractor.store, extractor.trajectory = store, trajectory
    if linked:
        extractor._link_produced_by(store, trajectory)
    return extractor, store, trajectory


# --- produced_by: deterministic ------------------------------------------------------------


def test_claims_are_produced_by_the_task_of_the_agent_call_of_their_llm_call():
    _, store, _ = _store()
    assert [store.related(PRODUCED_BY, c) for c in ("c1", "c2", "c3")] == [["t1"], ["t1"], ["t1"]]
    assert store.related(PRODUCED_BY, "c4") == ["t2"]
    assert store.related(PRODUCED_BY, "c5") == []  # an agent call without task


def test_an_agent_call_with_several_tasks_links_each_claim_to_all_of_them():
    extractor, store, trajectory = _store(linked=False)
    store.add("tasks", Task(id="t1b", task_text="Compare prices.", owned_by="a1"), "a1")
    extractor._link_produced_by(store, trajectory)
    assert store.related(PRODUCED_BY, "c2") == ["t1", "t1b"]


def test_nothing_is_linked_without_a_trajectory_or_when_the_llm_call_is_unknown():
    extractor, store, _ = _store(linked=False)
    extractor._link_produced_by(store, None)
    assert all(store.related(PRODUCED_BY, c) == [] for c in ("c1", "c2", "c3", "c4", "c5"))
    extractor._link_produced_by(store, SessionTrajectory(session_id="s1", steps=[]))
    assert all(store.related(PRODUCED_BY, c) == [] for c in ("c1", "c2", "c3", "c4", "c5"))


def test_linking_twice_changes_nothing():
    extractor, store, trajectory = _store()
    extractor._link_produced_by(store, trajectory)
    assert store.related(PRODUCED_BY, "c2") == ["t1"]


def _node(*claim_ids):
    return AnnotatedNode(
        node_type="LLMCall", node={"id": "n"}, symbolic_state_elements={"claims": [{"id": c} for c in claim_ids]}
    )


def test_claims_of_the_nodes_get_their_produced_by_list():
    extractor, _, _ = _store()
    nodes = [_node("c1", "c5"), _node("c4")]
    extractor.annotate_produced_by(nodes)
    assert [c["produced_by"] for c in nodes[0].symbolic_state_elements["claims"]] == [["t1"], []]
    assert nodes[1].symbolic_state_elements["claims"][0]["produced_by"] == ["t2"]


def test_claims_have_no_produced_by_key_without_a_trajectory():
    extractor, _, _ = _store()
    extractor.trajectory = None
    nodes = [_node("c1")]
    extractor.annotate_produced_by(nodes)
    assert "produced_by" not in nodes[0].symbolic_state_elements["claims"][0]


# --- the LLM judgement --------------------------------------------------------------------


def _given(store, *claim_ids):
    return [store.get(c).element for c in claim_ids]


def test_the_claims_given_are_judged_together_against_the_task_and_its_criteria():
    _, store, _ = _store()
    llm = JudgeLLM({"fulfils": ["C2", "C3"], "reasoning": "price and recommendation"})
    extractor = TaskClaimRelationExtractor(llm)

    (evaluation,) = extractor.extract(store, [TASK, *_given(store, "c1", "c2", "c3", "c4")])

    assert len(llm.prompts) == 1
    assert "Find a hotel in Lisbon." in llm.prompts[0] and "- under 150 EUR" in llm.prompts[0]
    assert "C1. Searching hotels." in llm.prompts[0] and "C3. I recommend Hotel Alfama." in llm.prompts[0]
    assert "A flight leaves at 9." not in llm.prompts[0]  # a claim produced by another task
    assert evaluation.task_id == "t1" and evaluation.claim_ids == ["c1", "c2", "c3"]
    assert evaluation.fulfils == ["c2", "c3"] and evaluation.reasoning == "price and recommendation"
    assert store.related(FULFILS_TASK, "c2") == ["t1"] and store.related(FULFILS_TASK, "c3") == ["t1"]
    assert store.related(FULFILS_TASK, "c1") == []
    assert extractor.last_usage.calls == 1


def test_only_the_claims_given_are_judged_not_all_those_produced_by_the_task():
    _, store, _ = _store()
    llm = JudgeLLM({"fulfils": ["C1"], "reasoning": ""})
    (evaluation,) = TaskClaimRelationExtractor(llm).extract(store, [TASK, *_given(store, "c2")])
    assert evaluation.claim_ids == ["c2"] and evaluation.fulfils == ["c2"]
    assert "Searching hotels." not in llm.prompts[0] and "C1. Hotel Alfama costs 120 EUR." in llm.prompts[0]


def test_no_claim_fulfilling_the_task_is_a_valid_verdict():
    _, store, _ = _store()
    claims = _given(store, "c1", "c2", "c3")
    (evaluation,) = TaskClaimRelationExtractor(JudgeLLM({"fulfils": [], "reasoning": "only process"})).extract(
        store, [TASK, *claims]
    )
    assert evaluation.fulfils == [] and evaluation.claim_ids == ["c1", "c2", "c3"]
    assert all(store.related(FULFILS_TASK, c) == [] for c in ("c1", "c2", "c3"))


def test_labels_that_are_not_claims_are_ignored_and_digits_are_accepted():
    _, store, _ = _store()
    reply = {"fulfils": ["2", "C2", "c9", "C7"], "reasoning": ""}
    (evaluation,) = TaskClaimRelationExtractor(JudgeLLM(reply)).extract(store, [TASK, *_given(store, "c1", "c2", "c3")])
    assert evaluation.fulfils == ["c2"]


def test_a_task_without_claims_among_the_elements_and_elements_that_are_not_stored_are_ignored():
    _, store, _ = _store()
    store.add("tasks", Task(id="t3", task_text="Nothing yet.", owned_by="a9"), "a9")
    llm = JudgeLLM()
    other = Task(id="t9", task_text="Not stored.", owned_by="a1")
    extractor = TaskClaimRelationExtractor(llm)
    assert extractor.extract(store, [store.get("t3").element, other, *_given(store, "c1")]) == []
    assert extractor.extract(store, [TASK, *_given(store, "c4")]) == []  # c4 is produced by t2, not t1
    assert extractor.extract(store, [TASK]) == []
    assert llm.prompts == []


def test_an_unusable_reply_is_asked_again_once_and_then_fails():
    _, store, _ = _store()
    elements = [TASK, *_given(store, "c1", "c2", "c3")]
    extractor = TaskClaimRelationExtractor(JudgeLLM({"reasoning": "no list"}, {"fulfils": "C1"}))
    with pytest.raises(ValueError):
        extractor.extract(store, elements)
    assert extractor.last_usage.calls == 2

    llm = JudgeLLM(ValueError("not json"), {"fulfils": ["C1"], "reasoning": ""})
    (evaluation,) = TaskClaimRelationExtractor(llm).extract(store, elements)
    assert evaluation.fulfils == ["c1"] and "not valid" in llm.prompts[1]


def test_render_prompt_lists_the_criteria_and_the_numbered_claims():
    text = render_prompt(TASK, [_claim("c2", "Hotel Alfama costs 120 EUR.", "l2")])
    assert "=== ACCEPTANCE CRITERIA ===\n- under 150 EUR" in text
    assert text.endswith("C1. Hotel Alfama costs 120 EUR.\n")
    none = render_prompt(Task(id="t", task_text="x", owned_by="a"), [_claim("c", "y", "l")])
    assert "(none stated)" in none


# --- run by SymbolicExtractor, one LLM node at a time ------------------------------------------


def _with(llm):
    extractor, store, trajectory = _store()
    extractor._relation_extractors = [TaskClaimRelationExtractor(llm)]
    return extractor, store


def _llm_node(call, *claim_ids):
    return AnnotatedNode(
        node_type="LLMCall", node={"id": call}, symbolic_state_elements={"claims": [{"id": c} for c in claim_ids]}
    )


def test_the_claims_of_a_node_are_judged_against_their_task_and_annotated():
    llm = JudgeLLM({"fulfils": ["C1"], "reasoning": "price"})
    extractor, store = _with(llm)
    node = _llm_node("l2", "c2", "c3")

    usage = extractor._extract_node_relations(node, store)

    assert len(llm.prompts) == 1 and usage.calls == 1
    assert "Searching hotels." not in llm.prompts[0]  # a claim of another LLM call of the same agent call
    assert [(e.task_id, e.claim_ids, e.fulfils) for e in extractor.task_evaluations] == [
        ("t1", ["c2", "c3"], ["c2"])
    ]
    assert [c.get("fulfils_task") for c in node.symbolic_state_elements["claims"]] == [["t1"], []]


def test_each_llm_call_of_an_agent_call_judges_its_own_claims():
    llm = JudgeLLM({"fulfils": [], "reasoning": ""}, {"fulfils": ["C2"], "reasoning": ""})
    extractor, store = _with(llm)
    first, second = _llm_node("l1", "c1"), _llm_node("l2", "c2", "c3")

    extractor._extract_node_relations(first, store)
    extractor._extract_node_relations(second, store)

    assert len(llm.prompts) == 2
    assert [(e.task_id, e.claim_ids) for e in extractor.task_evaluations] == [("t1", ["c1"]), ("t1", ["c2", "c3"])]
    assert first.symbolic_state_elements["claims"][0]["fulfils_task"] == []
    assert [c["fulfils_task"] for c in second.symbolic_state_elements["claims"]] == [[], ["t1"]]


def test_a_node_whose_claims_have_no_task_is_not_judged():
    llm = JudgeLLM()
    extractor, store = _with(llm)
    node = _llm_node("l4", "c5")
    assert extractor._extract_node_relations(node, store).calls == 0
    assert llm.prompts == [] and "fulfils_task" not in node.symbolic_state_elements["claims"][0]


def test_an_llm_node_processed_before_the_task_of_its_agent_call_is_not_judged():
    # live mode with the nodes out of order: the LLM call comes before its AgentCall
    store = SymbolicSessionStore("s1")
    store.add("claims", _claim("c1", "Searching hotels.", "l1"), "l1")
    trajectory = SessionTrajectory(session_id="s1", steps=STEPS)
    llm = JudgeLLM()
    extractor = SymbolicExtractor(relation_extractors=[TaskClaimRelationExtractor(llm)])
    extractor.store, extractor.trajectory = store, trajectory
    extractor._link_produced_by(store, trajectory)
    node = _llm_node("l1", "c1")

    extractor._extract_node_relations(node, store)

    assert llm.prompts == [] and "fulfils_task" not in node.symbolic_state_elements["claims"][0]


def test_every_task_of_the_agent_call_is_judged_and_a_failing_one_does_not_cost_the_others():
    extractor, store, trajectory = _store(linked=False)
    store.add("tasks", Task(id="t1b", task_text="Compare prices.", owned_by="a1"), "a1")
    extractor._link_produced_by(store, trajectory)
    llm = JudgeLLM(ValueError("a"), ValueError("b"), {"fulfils": ["C1"], "reasoning": ""})
    extractor._relation_extractors = [TaskClaimRelationExtractor(llm)]
    node = _llm_node("l2", "c2", "c3")

    extractor._extract_node_relations(node, store)  # t1 fails twice: logged, skipped; t1b is judged

    assert len(llm.prompts) == 3
    assert [e.task_id for e in extractor.task_evaluations] == ["t1b"]
    assert [c["fulfils_task"] for c in node.symbolic_state_elements["claims"]] == [["t1b"], []]


def test_nothing_is_judged_without_produced_by_links():
    extractor, store, _ = _store(linked=False)
    llm = JudgeLLM()
    extractor._relation_extractors = [TaskClaimRelationExtractor(llm)]
    node = _llm_node("l2", "c2")
    extractor._extract_node_relations(node, store)
    assert llm.prompts == [] and extractor.task_evaluations == []
    assert "fulfils_task" not in node.symbolic_state_elements["claims"][0]


def test_extract_relations_runs_it_on_each_node_and_adds_the_usage():
    llm = JudgeLLM({"fulfils": ["C1"], "reasoning": ""}, {"fulfils": [], "reasoning": ""})
    extractor, store = _with(llm)
    result = ExtractionResult(session_id="s1", nodes=[_llm_node("l2", "c2"), _llm_node("l3", "c4")])

    extractor._extract_relations(result, store)

    assert len(llm.prompts) == 2
    assert {e.task_id: e.fulfils for e in extractor.task_evaluations} == {"t1": ["c2"], "t2": []}
    assert result.llm_usage.calls == 2 and result.llm_usage_by_element["relations"].calls == 2
