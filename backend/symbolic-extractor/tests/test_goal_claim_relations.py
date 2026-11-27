"""``GoalClaimRelationExtractor``: do the claims of the final answer satisfy the goal (``fulfils``)?"""

import pytest

from symbolic_extractor import SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AnnotatedNode, Claim, Goal, SessionTrajectory
from symbolic_extractor.relations import FULFILS, GoalClaimRelationExtractor
from symbolic_extractor.relations.goal_claim_relations import SYSTEM_PROMPT, render_prompt
from symbolic_extractor.store import ANSWERS
from symbolic_extractor.usage import LLMUsage


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


GOAL = Goal(
    id="g1", goal_text="Plan the trip.", success_criteria=["A day by day plan", "The total cost"]
)


def _claim(claim_id, text, call):
    return Claim(id=claim_id, claim_text=text, claim_force="committing", generated_by=call)


def _store(answers=("c2", "c3")):
    """A goal, a claim of an early call (c1) and two claims of the final call (c2, c3); ``answers``
    of them have the ``answers`` relation to the goal."""
    store = SymbolicSessionStore("s1")
    store.add("goals", GOAL, "conversation:s1")
    store.add("claims", _claim("c1", "Searching flights.", "l1"), "l1")
    store.add("claims", _claim("c2", "Day 1 is Lisbon.", "l2"), "l2")
    store.add("claims", _claim("c3", "The total is 900 EUR.", "l2"), "l2")
    for claim_id in answers:
        store.link(ANSWERS, claim_id, "g1")
    return store


def test_the_answer_claims_are_judged_together_against_the_goal_and_its_criteria():
    store = _store()
    llm = JudgeLLM({"satisfied": True, "fulfils": ["C1", "C2"], "reasoning": "plan and cost"})
    extractor = GoalClaimRelationExtractor(llm)

    (evaluation,) = extractor.extract(store, [GOAL])

    assert len(llm.prompts) == 1
    assert "Plan the trip." in llm.prompts[0] and "- The total cost" in llm.prompts[0]
    assert (
        "C1. Day 1 is Lisbon." in llm.prompts[0] and "C2. The total is 900 EUR." in llm.prompts[0]
    )
    assert "Searching flights." not in llm.prompts[0]  # a claim that does not answer the goal
    assert (
        evaluation.satisfied
        and evaluation.claim_ids == ["c2", "c3"]
        and evaluation.fulfils == ["c2", "c3"]
    )
    assert evaluation.reasoning == "plan and cost"
    assert store.related(FULFILS, "c2") == ["g1"] and store.related(FULFILS, "c3") == ["g1"]
    assert store.related(FULFILS, "c1") == []
    assert extractor.last_usage.calls == 1


def test_only_the_claims_the_reply_names_fulfil_the_goal():
    store = _store()
    extractor = GoalClaimRelationExtractor(
        JudgeLLM({"satisfied": True, "fulfils": ["c2"], "reasoning": ""})
    )
    (evaluation,) = extractor.extract(store, [GOAL])
    assert (
        evaluation.fulfils == ["c3"]
        and store.related(FULFILS, "c3") == ["g1"]
        and store.related(FULFILS, "c2") == []
    )


def test_claims_that_do_not_satisfy_the_goal_are_not_linked():
    store = _store()
    reply = {
        "satisfied": False,
        "fulfils": ["C1"],
        "reasoning": "no cost",
    }  # what it lists is ignored
    (evaluation,) = GoalClaimRelationExtractor(JudgeLLM(reply)).extract(store, [GOAL])
    assert (
        not evaluation.satisfied and evaluation.fulfils == [] and evaluation.reasoning == "no cost"
    )
    assert all(store.related(FULFILS, c) == [] for c in ("c1", "c2", "c3"))


def test_a_satisfied_goal_becomes_achieved_in_the_store():
    store = _store()
    assert store.get("g1").element.status == "set"
    reply = {"satisfied": True, "status": "achieved", "fulfils": ["C1"], "reasoning": ""}
    (evaluation,) = GoalClaimRelationExtractor(JudgeLLM(reply)).extract(store, [GOAL])
    assert (evaluation.previous_status, evaluation.status) == ("set", "achieved")
    assert (
        store.get("g1").element.status == "achieved"
        and store.get("g1").node_id == "conversation:s1"
    )
    assert [s.id for s in store.by_kind("goals")] == ["g1"]
    assert GOAL.status == "set"  # the model given is not modified


def test_the_status_of_a_goal_that_is_not_satisfied_is_abandoned_or_set():
    abandoned = {
        "satisfied": False,
        "status": "abandoned",
        "fulfils": [],
        "reasoning": "cannot be done",
    }
    store = _store()
    (evaluation,) = GoalClaimRelationExtractor(JudgeLLM(abandoned)).extract(store, [GOAL])
    assert evaluation.status == "abandoned" and store.get("g1").element.status == "abandoned"

    # not met, not given up; a missing, unknown or contradictory status is read as 'set'
    for status in ("set", None, "achieved", "nonsense"):
        store = _store()
        reply = {"satisfied": False, "status": status, "fulfils": [], "reasoning": ""}
        (evaluation,) = GoalClaimRelationExtractor(JudgeLLM(reply)).extract(store, [GOAL])
        assert evaluation.status == "set" and store.get("g1").element.status == "set"


def test_a_satisfied_goal_is_achieved_whatever_the_reply_says_of_the_status():
    for status in (None, "set", "abandoned"):
        reply = {"satisfied": True, "status": status, "fulfils": ["C1"], "reasoning": ""}
        (evaluation,) = GoalClaimRelationExtractor(JudgeLLM(reply)).extract(_store(), [GOAL])
        assert evaluation.status == "achieved"


def test_the_prompt_asks_for_the_status_with_the_values_of_the_ontology():
    for value in ("achieved", "abandoned", "set"):
        assert f'"{value}"' in SYSTEM_PROMPT


def test_a_goal_without_answer_claims_is_not_asked():
    store = _store(answers=())
    llm = JudgeLLM()
    assert GoalClaimRelationExtractor(llm).extract(store, [GOAL]) == []
    assert llm.prompts == []


def test_elements_that_are_not_goals_of_the_store_are_ignored():
    store = _store()
    llm = JudgeLLM()
    other = Goal(id="g9", goal_text="Not stored.", success_criteria=["x"])
    assert GoalClaimRelationExtractor(llm).extract(store, [other, store.get("c2").element]) == []
    assert llm.prompts == []


def test_an_unusable_reply_is_asked_again_once_and_then_fails():
    store = _store()
    bad = {"satisfied": True, "fulfils": [], "reasoning": "?"}  # satisfied, but no claim named
    extractor = GoalClaimRelationExtractor(JudgeLLM(bad, {"satisfied": "yes"}))
    with pytest.raises(ValueError):
        extractor.extract(store, [GOAL])
    assert extractor.last_usage.calls == 2

    good = {"satisfied": "true", "fulfils": ["1"], "reasoning": ""}
    llm = JudgeLLM(ValueError("not json"), good)
    (evaluation,) = GoalClaimRelationExtractor(llm).extract(_store(), [GOAL])
    assert evaluation.satisfied and evaluation.fulfils == ["c2"] and "not valid" in llm.prompts[1]


def test_render_prompt_lists_the_criteria_and_the_numbered_claims():
    text = render_prompt(GOAL, [_claim("c2", "Day 1 is Lisbon.", "l2")])
    assert "=== SUCCESS CRITERIA ===\n- A day by day plan\n- The total cost" in text
    assert text.endswith("C1. Day 1 is Lisbon.\n")


# --- run by SymbolicExtractor, only once the final answer is extracted -----------------------

STEPS = [
    {"call_id": "a1", "kind": "agent", "parent_id": ""},
    {"call_id": "l1", "kind": "llm", "parent_id": "a1"},
    {"call_id": "l2", "kind": "llm", "parent_id": "a1"},
]


def _extractor(llm, final_agent_call="a1", answers=True):
    extractor = SymbolicExtractor(relation_extractors=[GoalClaimRelationExtractor(llm)])
    extractor.store = _store(answers=())
    extractor.trajectory = SessionTrajectory(
        session_id="s1", steps=STEPS, final_agent_call=final_agent_call
    )
    if answers:
        extractor._link_answers(extractor.store, extractor.trajectory)
    return extractor


def _result(*claim_ids):
    from symbolic_extractor.models import ExtractionResult

    node = AnnotatedNode(
        node_type="LLMCall",
        node={"id": "l2"},
        symbolic_state_elements={"claims": [{"id": c} for c in claim_ids]},
    )
    return ExtractionResult(session_id="s1", nodes=[node])


def test_the_extractor_judges_the_goal_once_the_final_answer_is_extracted():
    llm = JudgeLLM({"satisfied": True, "status": "achieved", "fulfils": ["C1"], "reasoning": "ok"})
    extractor = _extractor(llm)
    result = _result("c2", "c3")
    result.nodes.append(
        AnnotatedNode(
            node_type="Conversation",
            node={"id": "conversation:s1"},
            symbolic_state_elements={"goals": [{"id": "g1", "status": "set"}]},
        )
    )

    extractor._evaluate_goals(result, extractor.store)

    assert [e.satisfied for e in extractor.goal_evaluations] == [True]
    assert [c["fulfils"] for c in result.nodes[0].symbolic_state_elements["claims"]] == [["g1"], []]
    assert (
        result.nodes[1].symbolic_state_elements["goals"][0]["status"] == "achieved"
    )  # the goal of the result
    assert extractor.store.get("g1").element.status == "achieved"
    assert result.llm_usage.calls == 1 and result.llm_usage_by_element["relations"].calls == 1


def test_nothing_is_judged_without_the_claims_of_the_final_answer():
    llm = JudgeLLM()
    extractor = _extractor(
        llm, final_agent_call=None
    )  # the KG gave no final agent call: no answers
    result = _result("c2")

    extractor._evaluate_goals(result, extractor.store)

    assert llm.prompts == [] and extractor.goal_evaluations == []
    assert "fulfils" not in result.nodes[0].symbolic_state_elements["claims"][0]


def test_a_goal_is_not_judged_twice_for_the_same_claims_and_a_failure_is_not_retried():
    llm = JudgeLLM({"satisfied": False, "fulfils": [], "reasoning": "no"})
    extractor = _extractor(llm)
    extractor._evaluate_goals(_result("c2"), extractor.store)
    result = _result("c3")
    extractor._evaluate_goals(result, extractor.store)
    assert len(llm.prompts) == 1 and len(extractor.goal_evaluations) == 1
    # a node of a later call still gets the key, the verdict is known
    assert result.nodes[0].symbolic_state_elements["claims"][0]["fulfils"] == []

    failing = JudgeLLM(
        ValueError("a"), ValueError("b"), {"satisfied": True, "fulfils": ["C1"], "reasoning": ""}
    )
    extractor = _extractor(failing)
    extractor._evaluate_goals(_result("c2"), extractor.store)  # fails twice: logged, skipped
    extractor._evaluate_goals(_result("c2"), extractor.store)
    assert len(failing.prompts) == 2 and extractor.goal_evaluations == []


def test_the_goal_claim_extractor_is_not_run_on_the_claims_of_each_node():
    llm = JudgeLLM()
    extractor = _extractor(llm, answers=False)
    result = _result("c2")
    extractor._extract_relations(result, extractor.store)
    assert llm.prompts == []
