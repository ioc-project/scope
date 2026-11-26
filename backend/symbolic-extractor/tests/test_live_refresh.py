"""``SymbolicExtractor._refresh``: what the live mode brings up to date on the nodes returned earlier."""

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.models import AnnotatedNode, Goal, Task
from symbolic_extractor.relations import CapabilityTaskRelationExtractor
from symbolic_extractor.relations.goal_claim_relations import GoalEvaluation
from symbolic_extractor.relations.task_capability_relations import CapabilityEvaluation
from symbolic_extractor.store import SymbolicSessionStore


def node(node_id, node_type="LLMCall", **elements):
    return AnnotatedNode(node_type=node_type, node={"id": node_id}, symbolic_state_elements=elements)


def extractor(*relation_extractors):
    p = SymbolicExtractor(relation_extractors=list(relation_extractors))
    p.store = SymbolicSessionStore("s1")
    return p


def test_judging_the_goal_gives_every_returned_claim_its_fulfils_even_without_an_edge():
    p = extractor()
    early, current = node("l1", claims=[{"id": "c1"}]), node("l2", claims=[{"id": "c2"}])
    p._returned = {"l1": early}
    p.goal_evaluations = [GoalEvaluation(goal_id="g1", claim_ids=["c2"], satisfied=False)]

    updated = p._refresh(current, [], ["g1"], goal_judged=True)

    assert early.symbolic_state_elements["claims"][0]["fulfils"] == []  # judged, fulfils nothing: no edge
    assert current.symbolic_state_elements["claims"][0]["fulfils"] == []
    assert updated == [early]


def test_a_goal_judged_in_an_earlier_step_does_not_touch_the_nodes_again():
    p = extractor()
    early, current = node("l1", claims=[{"id": "c1"}]), node("l2", claims=[{"id": "c2"}])
    p._returned = {"l1": early}
    p.goal_evaluations = [GoalEvaluation(goal_id="g1", claim_ids=["c2"], satisfied=False)]
    p._refresh(current, [], ["g1"], goal_judged=True)

    third = node("l3", claims=[{"id": "c3"}])
    assert p._refresh(third, []) == []  # nothing judged in this step: the earlier nodes are not annotated again


def test_a_task_judged_against_the_capabilities_has_its_marker_even_when_no_capability_supports_it():
    p = extractor(CapabilityTaskRelationExtractor(llm=None))
    p.store.add("tasks", Task(id="t1", task_text="Book a hotel.", owned_by="a1"), "a1")
    early = node("a1", "AgentCall", tasks=[{"id": "t1"}])
    p._returned = {"a1": early}
    p.capability_evaluations = [CapabilityEvaluation(task_id="t1", capability_ids=["c1"], supports=[])]

    updated = p._refresh(node("l1"), [], ["t1"])

    assert early.symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == []  # judged, none supports it
    assert updated == [early]


def test_the_status_of_a_judged_goal_reaches_the_node_that_carried_it():
    p = extractor()
    p.store.add("goals", Goal(id="g1", goal_text="Fix it.", success_criteria=["It is fixed."], status="set"), "conv")
    conversation = node("conv", "Conversation", goals=[{"id": "g1", "status": "achieved"}])
    p._returned = {"conv": conversation}
    p.goal_evaluations = [GoalEvaluation(goal_id="g1", claim_ids=["c2"], satisfied=False, status="set")]

    updated = p._refresh(node("l2", claims=[{"id": "c2"}]), [], ["g1"], goal_judged=True)

    assert conversation.symbolic_state_elements["goals"][0]["status"] == "set"
    assert updated == [conversation]
