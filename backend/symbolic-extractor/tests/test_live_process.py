"""``SymbolicExtractor.live_process``: one node at a time of a session in progress."""

import json
import logging

import pytest

from symbolic_extractor import ExtractionRequest, SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.embeddings import TextEmbedder
from symbolic_extractor.extractors import (
    ClaimExtractionPrompt,
    ElementExtractor,
    ToolResultFactExtractor,
)
from symbolic_extractor.llm import LLMReply, TextReply
from symbolic_extractor.models import KGNode, SessionData, SessionTrajectory
from symbolic_extractor.relations import (
    DERIVED_FROM,
    SUPPORTED_BY,
    ClaimRelationExtractor,
    FactClaimLinker,
)
from symbolic_extractor.store import IN_CONTEXT
from symbolic_extractor.usage import LLMUsage

SYSTEM = json.dumps([{"type": "text", "content": "system prompt"}])
REQUEST = ExtractionRequest(session_id="s1")
FARE = "Fare: $75"


def llm_input(*texts, tool_responses=()):
    messages = [{"role": "user", "parts": [{"type": "text", "content": t}]} for t in texts]
    messages += [
        {"role": "tool", "parts": [{"type": "tool_call_response", "id": "x", "response": r}]}
        for r in tool_responses
    ]
    return f"{SYSTEM}\n\n{json.dumps(messages)}"


class EchoLLM:
    """One claim per call: the whole output of the call, which is also its verbatim source."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        output = user.split("(extract claims from this) ===\n")[1].strip()
        claim = {"claim_text": output, "claim_force": "assertive", "claim_kind": "factual", "verbatim_source": output}
        return LLMReply(data={"claims": [claim]}, usage=LLMUsage(calls=1, prompt_tokens=10, completion_tokens=5, cost_usd=0.01))


# The session: agent a1 runs l1, then the tool t1, then l2 (which received the claim of l1 and the tool result).
L1 = KGNode(node_type="LLMCall", node={"id": "l1", "startTime": 1.0, "prompt": llm_input("q"), "completion": "Hotel Alfama is free."})
T1 = KGNode(node_type="ToolCall", node={"id": "t1", "startTime": 2.0, "outputContent": json.dumps(FARE)})
L2 = KGNode(
    node_type="LLMCall",
    node={
        "id": "l2",
        "startTime": 3.0,
        "prompt": llm_input("q", "Hotel Alfama is free.", tool_responses=[FARE]),
        "completion": "I will book it.",
    },
)
NODES = [L1, T1, L2]
STEPS = [
    {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
    {"call_id": "l1", "kind": "llm", "parent_id": "a1", "start_time": 1.0, "duration_ms": 100.0},
    {"call_id": "t1", "kind": "tool", "parent_id": "a1", "start_time": 2.0, "duration_ms": 100.0},
    {"call_id": "l2", "kind": "llm", "parent_id": "a1", "start_time": 3.0, "duration_ms": 100.0},
]


def trajectory(steps=STEPS):
    return SessionTrajectory(session_id="s1", steps=steps)


def pipeline(embedder=None, relation_extractors=()):
    llm = EchoLLM()
    extractor = SymbolicExtractor(
        [ElementExtractor(llm, ClaimExtractionPrompt()), ToolResultFactExtractor()],
        embedder=embedder,
        relation_extractors=relation_extractors,
    )
    return extractor, llm


class YesNoLLM:
    """Text LLM for the fact-claim linker: answers the given replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_text(self, system, user):
        self.calls.append(user)
        reply = self.replies[len(self.calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return TextReply(text=reply, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=2, cost_usd=0.001))


def test_each_call_extracts_from_its_node_only_and_returns_only_that_node():
    extractor, llm = pipeline()
    result = extractor.live_process(L1, REQUEST, trajectory())

    assert [n.node_id for n in result.nodes] == ["l1"]
    assert result.session_id == "s1"
    assert llm.calls == 1  # no other node, and nothing is read from the KG
    assert len(result.nodes[0].symbolic_state_elements["claims"]) == 1
    assert result.llm_usage.calls == 1 and result.llm_usage.cost_usd == pytest.approx(0.01)
    assert result.llm_usage_by_element["claims"] == result.llm_usage


def test_the_store_is_kept_between_calls_and_gives_the_input_claims_and_facts_of_later_nodes():
    extractor, _ = pipeline()
    first = extractor.live_process(L1, REQUEST, trajectory())
    tool = extractor.live_process(T1, REQUEST, trajectory())
    last = extractor.live_process(L2, REQUEST, trajectory())

    (claim_id,) = [c["id"] for c in first.nodes[0].symbolic_state_elements["claims"]]
    (fact_id,) = [f["id"] for f in tool.nodes[0].symbolic_state_elements["facts"]]
    assert first.nodes[0].input_claims == [] and first.nodes[0].input_facts == []
    assert last.nodes[0].input_claims == [claim_id]
    assert last.nodes[0].input_facts == [fact_id]
    assert tool.nodes[0].input_claims is None  # only LLM calls have them

    store = extractor.store
    assert {s.id for s in store} == {claim_id, fact_id, last.nodes[0].symbolic_state_elements["claims"][0]["id"]}
    assert store.related("input_claim", "l2") == [claim_id]
    assert store.related("input_fact", "l2") == [fact_id]
    assert store.related("input_claim", "l1") == []
    assert last.symbolic_state.input_claims["l2"] == [claim_id]


def test_an_input_fact_is_in_context_of_every_claim_the_call_generated():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory())
    tool = extractor.live_process(T1, REQUEST, trajectory())
    last = extractor.live_process(L2, REQUEST, trajectory())

    (fact_id,) = [f["id"] for f in tool.nodes[0].symbolic_state_elements["facts"]]
    (claim_l1,) = extractor.store.claims_for_node("l1")
    (claim_l2,) = extractor.store.claims_for_node("l2")
    # l2 received the fact and generated one claim; l1 did not receive it
    assert extractor.store.related(IN_CONTEXT, fact_id) == [claim_l2.id]
    assert claim_l1.id not in extractor.store.related(IN_CONTEXT, fact_id)
    assert last.nodes[0].input_facts == [fact_id]


def test_in_context_is_the_same_per_session_and_live():
    session = SessionData(session_id="s1", llm_spans=[L1.node, L2.node], tool_spans=[T1.node])
    whole, _ = pipeline()
    whole.process(session, trajectory=trajectory())
    live, _ = pipeline()
    for node in NODES:
        live.live_process(node, REQUEST, trajectory())

    (fact,) = whole.store.by_kind("facts")
    assert len(whole.store.related(IN_CONTEXT, fact.id)) == 1
    assert whole.store.related(IN_CONTEXT, fact.id) == live.store.related(IN_CONTEXT, fact.id)


def test_the_fact_claim_linker_runs_on_the_claims_of_the_node_and_the_facts_in_their_context():
    text_llm = YesNoLLM("YES")
    extractor, _ = pipeline(relation_extractors=[FactClaimLinker(text_llm)])
    first = extractor.live_process(L1, REQUEST, trajectory())
    tool = extractor.live_process(T1, REQUEST, trajectory())
    assert text_llm.calls == []  # no fact in the context of l1's claim, no claim for the tool call
    assert first.llm_usage.calls == 1 and "relations" not in first.llm_usage_by_element

    last = extractor.live_process(L2, REQUEST, trajectory())
    (fact_id,) = [f["id"] for f in tool.nodes[0].symbolic_state_elements["facts"]]
    (claim_l2,) = extractor.store.claims_for_node("l2")
    assert len(text_llm.calls) == 1  # one (fact, claim) pair
    assert FARE in text_llm.calls[0] and claim_l2.element.claim_text in text_llm.calls[0]
    assert extractor.store.related(SUPPORTED_BY, claim_l2.id) == [fact_id]
    # the usage of the linker is added to the usage of the node: 1 extraction call + 1 judgement
    assert last.llm_usage.calls == 2 and last.llm_usage.cost_usd == pytest.approx(0.011)
    assert last.llm_usage_by_element["relations"].calls == 1


def test_a_not_backed_pair_records_no_supported_by():
    extractor, _ = pipeline(relation_extractors=[FactClaimLinker(YesNoLLM("NO"))])
    for node in NODES:
        extractor.live_process(node, REQUEST, trajectory())
    (claim_l2,) = extractor.store.claims_for_node("l2")
    assert extractor.store.related(SUPPORTED_BY, claim_l2.id) == []


def test_a_failing_relation_extractor_does_not_lose_the_node(caplog):
    extractor, _ = pipeline(relation_extractors=[FactClaimLinker(YesNoLLM(RuntimeError("proxy down")))])
    extractor.live_process(L1, REQUEST, trajectory())
    extractor.live_process(T1, REQUEST, trajectory())
    with caplog.at_level(logging.WARNING):
        last = extractor.live_process(L2, REQUEST, trajectory())
    assert len(last.nodes[0].symbolic_state_elements["claims"]) == 1
    assert last.nodes[0].input_facts and "FactClaimLinker failed" in caplog.text


def test_without_relation_extractors_nothing_is_added():
    extractor, _ = pipeline()
    for node in NODES:
        last = extractor.live_process(node, REQUEST, trajectory())
    assert "relations" not in last.llm_usage_by_element
    assert all(not extractor.store.related(SUPPORTED_BY, c.id) for c in extractor.store.by_kind("claims"))


def test_process_runs_the_relation_extractors_on_every_node():
    text_llm = YesNoLLM("YES")
    extractor, _ = pipeline(relation_extractors=[FactClaimLinker(text_llm)])
    session = SessionData(session_id="s1", llm_spans=[L1.node, L2.node], tool_spans=[T1.node])
    result = extractor.process(session, trajectory=trajectory())

    (fact,) = extractor.store.by_kind("facts")
    (claim_l1,) = extractor.store.claims_for_node("l1")
    (claim_l2,) = extractor.store.claims_for_node("l2")
    assert len(text_llm.calls) == 1  # only l2's claim has a fact in its context
    assert extractor.store.related(SUPPORTED_BY, claim_l2.id) == [fact.id]
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["l2"].symbolic_state_elements["claims"][0]["supported_by"] == [fact.id]
    assert "supported_by" not in by_id["l1"].symbolic_state_elements["claims"][0]
    assert claim_l1.id not in extractor.store.related(IN_CONTEXT, fact.id)
    assert result.llm_usage.calls == 3  # 2 extractions + 1 judgement
    assert result.llm_usage_by_element["relations"].calls == 1


def test_process_and_live_record_the_same_supported_by():
    session = SessionData(session_id="s1", llm_spans=[L1.node, L2.node], tool_spans=[T1.node])
    whole, _ = pipeline(relation_extractors=[FactClaimLinker(YesNoLLM("YES"))])
    whole.process(session, trajectory=trajectory())
    live, _ = pipeline(relation_extractors=[FactClaimLinker(YesNoLLM("YES"))])
    for node in NODES:
        live.live_process(node, REQUEST, trajectory())

    (claim_whole,) = whole.store.claims_for_node("l2")
    (claim_live,) = live.store.claims_for_node("l2")
    assert whole.store.related(SUPPORTED_BY, claim_whole.id) == live.store.related(SUPPORTED_BY, claim_live.id)
    assert len(live.store.related(SUPPORTED_BY, claim_live.id)) == 1


class RelationLLM:
    """JSON LLM for the claim-claim relation extractor: always says N1 is built on E1."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        data = {"relations": [{"new": "N1", "earlier": "E1", "relation": "contributes", "reasoning": "x"}]}
        return LLMReply(data=data, usage=LLMUsage(calls=1, prompt_tokens=50, completion_tokens=10, cost_usd=0.005))


def test_live_annotates_the_claims_with_their_relations_to_the_input_claims():
    relation_llm = RelationLLM()
    extractor, _ = pipeline(relation_extractors=[ClaimRelationExtractor(relation_llm)])
    results = {n.node_id: extractor.live_process(n, REQUEST, trajectory()) for n in NODES}

    (claim_l1,) = extractor.store.claims_for_node("l1")
    (claim_l2,) = extractor.store.claims_for_node("l2")
    l1_claim = results["l1"].nodes[0].symbolic_state_elements["claims"][0]
    l2_claim = results["l2"].nodes[0].symbolic_state_elements["claims"][0]

    assert l2_claim["derived_from"] == [claim_l1.id]
    assert l2_claim["conflicts_with"] == [] and l2_claim["same_as"] == []
    assert extractor.store.related(DERIVED_FROM, claim_l2.id) == [claim_l1.id]
    assert not {"derived_from", "conflicts_with", "same_as"} & set(l1_claim)  # l1 has no input claims
    assert relation_llm.calls == 1  # l1 has no input claims: no request
    assert results["l2"].llm_usage_by_element["relations"].calls == 1
    assert results["l1"].llm_usage_by_element.get("relations") is None


def test_process_gives_the_same_claim_relations_as_live():
    session = SessionData(session_id="s1", llm_spans=[L1.node, L2.node], tool_spans=[T1.node])
    whole, _ = pipeline(relation_extractors=[ClaimRelationExtractor(RelationLLM())])
    result = whole.process(session, trajectory=trajectory())

    (claim_l1,) = whole.store.claims_for_node("l1")
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["l2"].symbolic_state_elements["claims"][0]["derived_from"] == [claim_l1.id]
    assert "derived_from" not in by_id["l1"].symbolic_state_elements["claims"][0]
    assert result.llm_usage_by_element["relations"].calls == 1


def test_live_gives_the_same_input_claims_and_facts_as_the_per_session_process():
    session = SessionData(
        session_id="s1",
        llm_spans=[L1.node, L2.node],
        tool_spans=[T1.node],
    )
    whole, _ = pipeline()
    expected = {n.node_id: (n.input_claims, n.input_facts) for n in whole.process(session, trajectory=trajectory()).nodes}

    live, _ = pipeline()
    got = {}
    for node in NODES:
        annotated = live.live_process(node, REQUEST, trajectory()).nodes[0]
        got[annotated.node_id] = (annotated.input_claims, annotated.input_facts)

    assert got == expected
    assert {s.id for s in live.store} == {s.id for s in whole.store}


def test_a_trajectory_that_grows_with_the_session_is_enough():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory(STEPS[:2]))
    extractor.live_process(T1, REQUEST, trajectory(STEPS[:3]))
    last = extractor.live_process(L2, REQUEST, trajectory(STEPS))
    assert len(last.nodes[0].input_claims) == 1 and len(last.nodes[0].input_facts) == 1


def test_the_final_llm_call_is_recognised_by_the_extractor_and_its_claims_answer_the_goal():
    from symbolic_extractor.models import Goal

    extractor, _ = pipeline()
    extractor.store = SymbolicSessionStore("s1")  # the goal comes from the Conversation node, first
    extractor.store.add("goals", Goal(id="g1", goal_text="Book it", success_criteria=["booked"]), "conv")
    final = SessionTrajectory(session_id="s1", steps=STEPS, final_agent_call="a1")  # l2 is its last LLM call

    first = extractor.live_process(L1, REQUEST, final)
    extractor.live_process(T1, REQUEST, final)
    last = extractor.live_process(L2, REQUEST, final)

    # nobody told the extractor that l2 is the last node: the trajectory did
    assert [c["answers"] for c in first.nodes[0].symbolic_state_elements["claims"]] == [[]]
    assert [c["answers"] for c in last.nodes[0].symbolic_state_elements["claims"]] == [["g1"]]


def test_live_the_goal_is_judged_when_the_final_llm_node_is_processed_and_not_before():
    from symbolic_extractor.models import Goal
    from symbolic_extractor.relations import GoalClaimRelationExtractor

    class JudgeLLM:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system, user):
            self.calls += 1
            return LLMReply(
                data={"satisfied": True, "fulfils": ["C1"], "reasoning": "booked"}, usage=LLMUsage(calls=1)
            )

    judge = JudgeLLM()
    extractor, _ = pipeline(relation_extractors=[GoalClaimRelationExtractor(judge)])
    extractor.store = SymbolicSessionStore("s1")
    extractor.store.add("goals", Goal(id="g1", goal_text="Book it", success_criteria=["booked"]), "conv")
    final = SessionTrajectory(session_id="s1", steps=STEPS, final_agent_call="a1")  # l2 is its last LLM call

    first = extractor.live_process(L1, REQUEST, final)
    extractor.live_process(T1, REQUEST, final)
    assert judge.calls == 0 and "fulfils" not in first.nodes[0].symbolic_state_elements["claims"][0]

    last = extractor.live_process(L2, REQUEST, final)

    assert judge.calls == 1
    (evaluation,) = extractor.goal_evaluations
    assert evaluation.satisfied and evaluation.goal_id == "g1"
    (claim,) = last.nodes[0].symbolic_state_elements["claims"]
    assert claim["fulfils"] == ["g1"]
    assert last.llm_usage_by_element["relations"].calls == 1
    # the verdict and its reasoning are in the result of the step that judged, and only there
    assert [e["reasoning"] for e in last.evaluations["goals"]] == ["booked"]
    assert first.evaluations["goals"] == [] and first.evaluations["tasks"] == []


def test_without_a_known_final_agent_call_live_claims_have_no_answers_key():
    extractor, _ = pipeline()
    last = extractor.live_process(L2, REQUEST, trajectory())
    assert "answers" not in last.nodes[0].symbolic_state_elements["claims"][0]


def test_without_a_trajectory_the_elements_are_extracted_but_the_inputs_are_not_tracked():
    extractor, _ = pipeline()
    result = extractor.live_process(L1, REQUEST)

    assert len(result.nodes[0].symbolic_state_elements["claims"]) == 1
    assert result.nodes[0].input_claims is None and result.nodes[0].input_facts is None
    assert result.symbolic_state is None
    assert len(extractor.store) == 1  # still kept for later


def test_a_node_without_a_trajectory_has_no_input_claims():
    extractor, _ = pipeline()
    assert extractor.live_process(L1, REQUEST).nodes[0].input_claims is None


def test_a_node_missing_from_the_trajectory_is_reported_and_has_no_input_claims(caplog):
    extractor, _ = pipeline()
    with caplog.at_level(logging.WARNING):
        result = extractor.live_process(L2, REQUEST, trajectory(STEPS[:3]))  # l2 not in it yet
    assert result.nodes[0].input_claims is None
    assert "l2" in caplog.text and "not in the trajectory" in caplog.text


class TopicBackend:
    """Embeds a text as the unit vector of its topic: 'hotel' texts together, the others apart."""

    model_id = "fake-embed"

    def __init__(self):
        self.calls = []

    def encode(self, sentences):
        self.calls.append(list(sentences))
        return [[1.0, 0.0] if "Alfama" in s else [0.0, 1.0] for s in sentences]


# A later call that restates the claim of l1 (it received it as input).
L3 = KGNode(
    node_type="LLMCall",
    node={"id": "l3", "startTime": 4.0, "prompt": llm_input("q", "Hotel Alfama is free."), "completion": "Hotel Alfama is free."},
)
STEPS3 = STEPS + [{"call_id": "l3", "kind": "llm", "parent_id": "a1", "start_time": 4.0, "duration_ms": 100.0}]


def embedding_pipeline():
    backend = TopicBackend()
    extractor, _ = pipeline(embedder=TextEmbedder(backend))
    return extractor, backend


def test_same_claims_are_computed_one_node_at_a_time_and_embeddings_accumulate():
    extractor, backend = embedding_pipeline()
    first = extractor.live_process(L1, REQUEST, trajectory(STEPS3))
    assert first.nodes[0].symbolic_state_elements["claims"][0]["same_claims"] == []  # no input claim yet
    assert len(extractor.embeddings.elements) == 1

    extractor.live_process(T1, REQUEST, trajectory(STEPS3))
    second = extractor.live_process(L2, REQUEST, trajectory(STEPS3))
    assert second.nodes[0].symbolic_state_elements["claims"][0]["same_claims"] == []  # other topic
    last = extractor.live_process(L3, REQUEST, trajectory(STEPS3))

    (claim_l1,) = first.nodes[0].symbolic_state_elements["claims"]
    (claim_l3,) = last.nodes[0].symbolic_state_elements["claims"]
    assert claim_l3["same_claims"] == [claim_l1["id"]]
    assert extractor.store.related("same_claim", claim_l3["id"]) == [claim_l1["id"]]
    assert {e.element_id for e in extractor.embeddings.elements} == {
        c["id"] for r in (first, second, last) for c in r.nodes[0].symbolic_state_elements["claims"]
    }
    assert extractor.embeddings.model == "fake-embed" and extractor.embeddings.dimensions == 2
    # each call only embeds the claims of its own node (text and snippet are the same here: 1 text)
    assert [len(c) for c in backend.calls] == [1, 1, 1]


def test_live_same_claims_equal_the_per_session_ones():
    session = SessionData(session_id="s1", llm_spans=[L1.node, L2.node, L3.node], tool_spans=[T1.node])
    whole, _ = pipeline(embedder=TextEmbedder(TopicBackend()))
    expected = {
        n.node_id: [c["same_claims"] for c in n.symbolic_state_elements.get("claims", [])]
        for n in whole.process(session, trajectory=trajectory(STEPS3)).nodes
    }

    live, _ = pipeline(embedder=TextEmbedder(TopicBackend()))
    got = {}
    for node in [L1, T1, L2, L3]:
        n = live.live_process(node, REQUEST, trajectory(STEPS3)).nodes[0]
        got[n.node_id] = [c["same_claims"] for c in n.symbolic_state_elements.get("claims", [])]

    assert got == expected
    assert got["l3"] != [[]]  # the comparison did find something
    assert {e.element_id for e in live.embeddings.elements} == {e.element_id for e in whole.embeddings.elements}


def test_without_a_trajectory_claims_are_embedded_but_not_compared():
    extractor, _ = embedding_pipeline()
    result = extractor.live_process(L1, REQUEST)
    assert "same_claims" not in result.nodes[0].symbolic_state_elements["claims"][0]
    assert len(extractor.embeddings.elements) == 1


def test_a_nodes_without_claims_adds_no_embeddings():
    extractor, backend = embedding_pipeline()
    extractor.live_process(T1, REQUEST, trajectory(STEPS3))
    assert backend.calls == [] and extractor.embeddings.elements == []


def test_a_node_of_another_session_starts_new_embeddings():
    extractor, _ = embedding_pipeline()
    extractor.live_process(L1, REQUEST, trajectory(STEPS3))
    first = extractor.embeddings
    extractor.live_process(L1, ExtractionRequest(session_id="s2"))
    assert extractor.embeddings is not first and extractor.embeddings.session_id == "s2"
    assert len(first.elements) == 1  # untouched


def test_no_embedder_means_no_same_claims_and_no_embeddings():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory())
    last = extractor.live_process(L2, REQUEST, trajectory())
    assert extractor.embeddings is None
    assert "same_claims" not in last.nodes[0].symbolic_state_elements["claims"][0]


def test_a_node_of_another_session_starts_a_new_store():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory())
    first_store = extractor.store
    extractor.live_process(L1, REQUEST, trajectory())
    assert extractor.store is first_store  # same session: kept

    extractor.live_process(L1, ExtractionRequest(session_id="s2"))
    assert extractor.store is not first_store and extractor.store.session_id == "s2"
    assert len(extractor.store) == 1


def test_processing_the_same_node_twice_does_not_duplicate_its_elements():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory())
    extractor.live_process(L1, REQUEST, trajectory())
    assert len(extractor.store.by_kind("claims")) == 1


def test_process_starts_from_a_new_store_each_time_unless_one_is_given():
    session = SessionData(session_id="s1", llm_spans=[L1.node])
    extractor, _ = pipeline()
    extractor.process(session)
    first = extractor.store
    extractor.process(session)
    assert extractor.store is not first and len(extractor.store) == 1

    mine = SymbolicSessionStore("s1")
    extractor.process(session, store=mine)
    assert extractor.store is mine and len(mine) == 1


def test_process_after_live_does_not_reuse_the_live_store():
    extractor, _ = pipeline()
    extractor.live_process(L1, REQUEST, trajectory())
    live_store = extractor.store
    extractor.process(SessionData(session_id="s1", llm_spans=[L2.node]), trajectory=trajectory())
    assert extractor.store is not live_store
    assert len(live_store) == 1  # untouched
