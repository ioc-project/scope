"""``ArgumentOriginExtractor``: which claims the arguments of a tool call come from (``argument_from``)."""

import pytest

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AnnotatedNode, Claim, DelegationClaim, Fact, SessionTrajectory
from symbolic_extractor.relations import ArgumentOriginExtractor
from symbolic_extractor.relations.fact_claim_links import SUPPORTED_BY
from symbolic_extractor.relations.argument_origin_relations import (
    MAX_CANDIDATES,
    MAX_VALUE_CHARS,
    SYSTEM_PROMPT,
    candidate_claims,
    parse_origins,
    render_prompt,
)
from symbolic_extractor.store import ANNOUNCES, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

OUTPUT = "TOOL-OUTPUT-MUST-NOT-BE-SHOWN"


class OriginLLM:
    """Answers each request with the next of ``replies`` (the last one repeats)."""

    def __init__(self, *replies):
        self.replies = list(replies) or [
            {"arguments": [{"name": "expression", "basis": "derived", "from": ["C2", "C3"], "reasoning": "sum"}]}
        ]
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        data = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(data, Exception):
            raise data
        return LLMReply(data=data, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))


def claim(cid, text, call, force="assertive"):
    kind = {"claim_kind": "factual"} if force == "assertive" else {}
    return Claim(id=cid, claim_text=text, claim_force=force, generated_by=call, **kind)


def fact(fid, call, arguments=None, tool="calculate"):
    return Fact(
        id=fid,
        fact_text=OUTPUT,
        verbatim_source=OUTPUT,
        grounded_in=call,
        tool_name=tool,
        tool_arguments={"expression": "3 + 2"} if arguments is None else arguments,
    )


def step(call_id, kind, parent, start, agent="planner"):
    return {"call_id": call_id, "kind": kind, "name": call_id, "agent_id": agent, "parent_id": parent, "start_time": start}


#   a1:  l1 (t=1) -> tool t1 (t=2) -> l2 (t=3) -> tool t2 (t=4)         a2: l3 (t=1.5)
TRAJECTORY = SessionTrajectory(
    session_id="s1",
    steps=[
        step("a1", "agent", "", 0),
        step("l1", "llm", "a1", 1),
        step("t1", "tool", "a1", 2),
        step("l2", "llm", "a1", 3),
        step("t2", "tool", "a1", 4),
        step("a2", "agent", "", 0.5, agent="other"),
        step("l3", "llm", "a2", 1.5, agent="other"),
    ],
)


def make_store():
    store = SymbolicSessionStore("s1")
    brief = DelegationClaim(
        id="d1",
        claim_text="Plan a trip from A to B.",
        claim_force="directive",
        generated_by="l0",
        caller_agent="router",
        caller_call="a0",
        delegated_to="a1",
    )
    store.add("delegation_claims", brief, "a1")
    store.add("claims", claim("c1", "The trip is long.", "l1"), "l1")
    store.add("claims", claim("c2", "The first leg takes 3 hours.", "l1"), "l1")
    store.add("claims", claim("c3", "The second leg takes 2 hours.", "l1"), "l1")
    store.add("claims", claim("c4", "Now the total is 5 hours.", "l2"), "l2")
    store.add("claims", claim("c5", "Another agent's claim.", "l3"), "l3")
    store.add("facts", fact("f1", "t1"), "t1")
    store.add("facts", fact("f2", "t2", {"expression": "5 + 1"}), "t2")
    return store


def ids(claims):
    return [c.id for c in claims]


# --- the candidates -----------------------------------------------------------------------


def test_the_candidates_are_the_brief_and_the_claims_the_agent_call_made_before_the_tool_call():
    store = make_store()
    # t1 ran after l1 only: not l2 (later), not l3 (another agent call)
    assert ids(candidate_claims(store, TRAJECTORY, "t1")) == ["d1", "c1", "c2", "c3"]
    # t2 ran after l2 too
    assert ids(candidate_claims(store, TRAJECTORY, "t2")) == ["d1", "c1", "c2", "c3", "c4"]


def test_no_candidates_when_the_tool_call_or_its_agent_call_is_unknown():
    store = make_store()
    assert candidate_claims(store, TRAJECTORY, "nope") == []
    assert candidate_claims(store, TRAJECTORY, "l1") == []  # not a tool call
    assert candidate_claims(store, TRAJECTORY, "a1") == []
    assert candidate_claims(store, SessionTrajectory(session_id="s1", steps=[step("t1", "tool", "", 2)]), "t1") == []


def test_only_the_most_recent_candidates_are_kept():
    store = SymbolicSessionStore("s1")
    for i in range(MAX_CANDIDATES + 5):
        store.add("claims", claim(f"x{i}", f"Claim {i}.", "l1"), "l1")
    kept = candidate_claims(store, TRAJECTORY, "t1")
    assert len(kept) == MAX_CANDIDATES
    assert kept[-1].id == f"x{MAX_CANDIDATES + 4}" and kept[0].id == "x5"
    assert ids(candidate_claims(store, TRAJECTORY, "t1", limit=2)) == [f"x{MAX_CANDIDATES + 3}", f"x{MAX_CANDIDATES + 4}"]


def test_a_tool_call_with_no_claim_before_it_has_no_candidate():
    store = SymbolicSessionStore("s1")
    store.add("claims", claim("c5", "Another agent's claim.", "l3"), "l3")
    assert candidate_claims(store, TRAJECTORY, "t1") == []


# --- the prompt ---------------------------------------------------------------------------


def test_the_prompt_has_the_tool_the_arguments_and_the_numbered_claims_but_not_the_output():
    store = make_store()
    f1 = store.get("f1").element
    prompt = render_prompt(f1, candidate_claims(store, TRAJECTORY, "t1"), {"c3"}, {"d1"})
    assert "calculate" in prompt and '- expression = "3 + 2"' in prompt
    assert "C1. Plan a trip from A to B. [from the brief the agent received]" in prompt
    assert "C2. The trip is long." in prompt and "C2. The trip is long. [" not in prompt
    assert "C4. The second leg takes 2 hours. [announces this call]" in prompt
    assert OUTPUT not in prompt


def test_a_long_argument_value_is_cut():
    long = fact("f9", "t1", {"text": "x" * (MAX_VALUE_CHARS * 3)})
    prompt = render_prompt(long, [claim("c1", "A claim.", "l1")])
    # the cut applies to the JSON text of the value, which starts with a quote
    assert "x" * (MAX_VALUE_CHARS - 1) in prompt and "x" * MAX_VALUE_CHARS not in prompt
    assert "..." in prompt


# --- parsing ------------------------------------------------------------------------------


def test_each_argument_gets_a_verdict_and_what_cannot_be_trusted_is_none():
    claims = [claim("c1", "One.", "l1"), claim("c2", "Two.", "l1")]
    f = fact("f1", "t1", {"a": 3, "b": 2, "c": "x", "d": 1, "e": 7})
    data = {
        "arguments": [
            {"name": "a", "basis": "Derived", "from": ["c1", "C1", "C9", 2], "reasoning": " sum "},
            {"name": "b", "basis": "stated", "from": ["C2"], "reasoning": "stated"},
            {"name": "c", "basis": "guess", "from": ["C1"]},  # not a basis: none
            {"name": "e", "basis": "stated", "from": []},  # nothing named: none
            {"name": "zzz", "basis": "stated", "from": ["C1"]},  # not an argument: ignored
        ]
    }
    origins = {o.argument: o for o in parse_origins(data, f, claims)}

    assert list(origins) == ["a", "b", "c", "d", "e"]  # in the order of the arguments, "d" is not mentioned
    assert (origins["a"].basis, origins["a"].claim_ids, origins["a"].reasoning) == ("derived", ["c1", "c2"], "sum")
    assert origins["a"].value == 3
    assert (origins["b"].basis, origins["b"].claim_ids) == ("stated", ["c2"])
    for name in "cde":
        assert (origins[name].basis, origins[name].claim_ids) == ("none", [])


def test_a_reply_that_echoes_the_line_of_the_argument_is_still_read():
    f = fact("f1", "t1")  # expression = "3 + 2"
    claims = [claim("c1", "One.", "l1")]
    for name in ('expression = "3 + 2"', '- expression = "3 + 2"', " expression "):
        (origin,) = parse_origins({"arguments": [{"name": name, "basis": "derived", "from": ["C1"]}]}, f, claims)
        assert (origin.argument, origin.basis, origin.claim_ids) == ("expression", "derived", ["c1"])
    (origin,) = parse_origins({"arguments": [{"name": 'other = "x"', "basis": "derived", "from": ["C1"]}]}, f, claims)
    assert origin.basis == "none"


def test_a_reply_without_the_list_of_arguments_is_rejected():
    f = fact("f1", "t1")
    for data in ({}, {"arguments": "none"}, {"supports": ["C1"]}):
        with pytest.raises(ValueError):
            parse_origins(data, f, [claim("c1", "One.", "l1")])
    # an empty list is a valid answer: every argument is "none"
    (origin,) = parse_origins({"arguments": []}, f, [claim("c1", "One.", "l1")])
    assert origin.basis == "none"


# --- the extractor ------------------------------------------------------------------------


def test_the_arguments_are_judged_and_one_link_per_claim_is_recorded_with_what_it_explains():
    store = make_store()
    llm = OriginLLM(
        {
            "arguments": [
                {"name": "expression", "basis": "derived", "from": ["C3", "C4"], "reasoning": "3 and 2 hours"},
            ]
        }
    )
    extractor = ArgumentOriginExtractor(llm)

    (evaluation,) = extractor.extract(store, [store.get("f1").element], TRAJECTORY)

    assert (evaluation.tool_call_id, evaluation.fact_id, evaluation.tool_name) == ("t1", "f1", "calculate")
    assert evaluation.candidate_ids == ["d1", "c1", "c2", "c3"]
    assert [(o.argument, o.basis, o.claim_ids) for o in evaluation.origins] == [
        ("expression", "derived", ["c2", "c3"])
    ]
    assert [o.argument for o in evaluation.traced] == ["expression"]
    assert store.related(SUPPORTED_BY, "f1") == ["c2", "c3"]
    (edge,) = [e for e in store.edges() if e.target_id == "c2"]
    assert (edge.edge_type, edge.source_id) == (SUPPORTED_BY, "f1")
    assert edge.attributes == {"arguments": {"expression": "derived"}}
    system, user = llm.calls[0]
    assert system == SYSTEM_PROMPT and len(llm.calls) == 1
    assert OUTPUT not in user and "3 + 2" in user
    assert extractor.last_usage.calls == 1


def test_a_claim_that_explains_several_arguments_has_one_link_with_all_of_them():
    store = make_store()
    store.add("facts", fact("f3", "t1", {"a": 3, "b": 2, "c": 9}), "t1")
    llm = OriginLLM(
        {
            "arguments": [
                {"name": "a", "basis": "stated", "from": ["C3"]},
                {"name": "b", "basis": "derived", "from": ["C3", "C4"]},
                {"name": "c", "basis": "none", "from": []},
            ]
        }
    )
    ArgumentOriginExtractor(llm).extract(store, [store.get("f3").element], TRAJECTORY)

    edges = {e.target_id: e.attributes for e in store.edges() if e.edge_type == SUPPORTED_BY}
    assert edges == {"c2": {"arguments": {"a": "stated", "b": "derived"}}, "c3": {"arguments": {"b": "derived"}}}


def test_an_argument_traced_to_no_claim_gives_no_link():
    store = make_store()
    llm = OriginLLM({"arguments": [{"name": "expression", "basis": "none", "from": [], "reasoning": "user request"}]})
    (evaluation,) = ArgumentOriginExtractor(llm).extract(store, [store.get("f1").element], TRAJECTORY)
    assert evaluation.traced == [] and evaluation.origins[0].reasoning == "user request"
    assert store.related(SUPPORTED_BY, "f1") == []


def test_the_claims_that_announce_the_call_and_the_brief_are_marked_in_the_prompt():
    store = make_store()
    store.link(ANNOUNCES, "c3", "t1")
    llm = OriginLLM()
    ArgumentOriginExtractor(llm).extract(store, [store.get("f1").element], TRAJECTORY)
    user = llm.calls[0][1]
    assert "C1. Plan a trip from A to B. [from the brief the agent received]" in user
    assert "C4. The second leg takes 2 hours. [announces this call]" in user


def test_nothing_is_asked_without_arguments_candidates_or_trajectory():
    store = make_store()
    store.add("facts", fact("f4", "t1", {}), "t1")  # a tool with no argument
    llm = OriginLLM()
    extractor = ArgumentOriginExtractor(llm)
    assert extractor.extract(store, [store.get("f4").element], TRAJECTORY) == []
    assert extractor.extract(store, [store.get("f1").element]) == []  # no trajectory
    assert extractor.extract(store, [claim("c9", "Not a fact.", "l1")], TRAJECTORY) == []
    assert extractor.extract(store, [fact("f5", "t1")], TRAJECTORY) == []  # not in the store

    empty = SymbolicSessionStore("s1")  # no claim before the call
    empty.add("facts", fact("f1", "t1"), "t1")
    assert extractor.extract(empty, [empty.get("f1").element], TRAJECTORY) == []
    assert llm.calls == [] and extractor.last_usage.calls == 0
    assert store.related(SUPPORTED_BY, "f1") == []


def test_has_candidates_tells_whether_a_fact_is_ready_to_be_judged():
    store = make_store()
    extractor = ArgumentOriginExtractor(OriginLLM())
    assert extractor.has_candidates(store, TRAJECTORY, store.get("f1").element)
    assert not extractor.has_candidates(store, TRAJECTORY, fact("f4", "t1", {}))
    assert not extractor.has_candidates(SymbolicSessionStore("s1"), TRAJECTORY, store.get("f1").element)


def test_an_unusable_reply_is_asked_again_and_the_usage_of_both_requests_counts():
    store = make_store()
    good = {"arguments": [{"name": "expression", "basis": "stated", "from": ["C3"]}]}
    llm = OriginLLM({"oops": 1}, good)
    extractor = ArgumentOriginExtractor(llm)

    (evaluation,) = extractor.extract(store, [store.get("f1").element], TRAJECTORY)

    assert len(llm.calls) == 2 and "previous reply was not valid" in llm.calls[1][1]
    assert evaluation.origins[0].claim_ids == ["c2"]
    assert extractor.last_usage.calls == 2


def test_a_reply_that_is_not_json_is_asked_again_too():
    store = make_store()
    llm = OriginLLM(ValueError("not json"), {"arguments": []})
    (evaluation,) = ArgumentOriginExtractor(llm).extract(store, [store.get("f1").element], TRAJECTORY)
    assert len(llm.calls) == 2 and evaluation.traced == []


def test_two_unusable_replies_raise_and_record_nothing():
    store = make_store()
    llm = OriginLLM({"oops": 1})
    extractor = ArgumentOriginExtractor(llm)
    with pytest.raises(ValueError):
        extractor.extract(store, [store.get("f1").element], TRAJECTORY)
    assert len(llm.calls) == 2 and extractor.last_usage.calls == 2
    assert store.related(SUPPORTED_BY, "f1") == []


# --- in the pipeline ----------------------------------------------------------------------


def tool_node(call_id):
    return AnnotatedNode(node_type="ToolCall", node={"id": call_id}, symbolic_state_elements={})


def pipeline(llm, store):
    extractor = SymbolicExtractor(relation_extractors=[ArgumentOriginExtractor(llm)])
    extractor.store = store
    extractor.trajectory = TRAJECTORY
    return extractor


def test_a_tool_call_node_is_judged_once_when_its_relations_are_extracted():
    store = make_store()
    llm = OriginLLM({"arguments": [{"name": "expression", "basis": "derived", "from": ["C3", "C4"]}]})
    extractor = pipeline(llm, store)

    usage = extractor._extract_node_relations(tool_node("t1"), store)

    assert len(llm.calls) == 1 and usage.calls == 1
    assert store.related(SUPPORTED_BY, "f1") == ["c2", "c3"]
    assert [e.tool_call_id for e in extractor.argument_evaluations] == ["t1"]
    evaluations = extractor._evaluations()
    assert [e["tool_call_id"] for e in evaluations["argument_origins"]] == ["t1"]
    assert evaluations["argument_origins"][0]["origins"][0]["claim_ids"] == ["c2", "c3"]
    # the counters of live mode: only what was judged after them is returned
    assert extractor._evaluations(arguments=1)["argument_origins"] == []


def test_the_other_nodes_are_not_judged():
    store = make_store()
    llm = OriginLLM()
    extractor = pipeline(llm, store)
    for node_type, call_id in (("LLMCall", "l1"), ("AgentCall", "a1")):
        node = AnnotatedNode(node_type=node_type, node={"id": call_id}, symbolic_state_elements={})
        extractor._extract_node_relations(node, store)
    assert llm.calls == [] and extractor.argument_evaluations == []


def test_the_node_is_not_judged_without_a_trajectory_and_a_failing_request_does_not_stop_the_node():
    store = make_store()
    llm = OriginLLM()
    extractor = pipeline(llm, store)
    extractor.trajectory = None
    extractor._extract_node_relations(tool_node("t1"), store)
    assert llm.calls == []

    failing = OriginLLM({"oops": 1})
    extractor = pipeline(failing, store)
    usage = extractor._extract_node_relations(tool_node("t1"), store)  # logged, not raised
    assert usage.calls == 2 and extractor.argument_evaluations == []
    assert store.related(SUPPORTED_BY, "f1") == []
