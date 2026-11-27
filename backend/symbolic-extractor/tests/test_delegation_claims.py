"""``DelegationClaimExtractor``: the claims of the brief an agent call received, linked to its caller."""

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import DelegationClaim, DelegationClaimExtractor, find_caller
from symbolic_extractor.extractors.delegation_claims import (
    MAX_SOURCE_CLAIMS,
    Caller,
    brief_text,
    contains_blob,
    delegated_reply,
    is_delegation_output,
    reply_claims,
    reply_receiver,
    skip_reason,
    source_claims,
    without_speaker,
)
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AnnotatedNode, Claim, KGNode, SessionData, SessionTrajectory
from symbolic_extractor.relations.claim_claim_relations import ClaimRelationExtractor
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

BRIEF = "Root cause: a bad deploy of v2.18.3. All upstream services are healthy. Assess the rollback risk."
SNIPPET = "Root cause: a bad deploy of v2.18.3."


class FakeLLM:
    def __init__(self, claims=None):
        self.claims = (
            claims
            if claims is not None
            else [
                {
                    "claim_text": "The root cause is a bad deploy of v2.18.3.",
                    "claim_force": "assertive",
                    "claim_kind": "factual",
                    "verbatim_source": SNIPPET,
                },
            ]
        )
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(
            data={"claims": self.claims},
            usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20),
        )


# sre delegates to telemetry, then to backend and db (one LLM turn, run one after the other),
# is resumed, and delegates to risk_assessment with a second turn.
STEPS = [
    {
        "call_id": "a_sre1",
        "kind": "agent",
        "agent_id": "sre",
        "parent_id": "",
        "start_time": 0.0,
        "duration_ms": 2000,
        "output_text": "output:sre:delegate_to_telemetry",
    },
    {
        "call_id": "l_sre1",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre1",
        "start_time": 0.1,
        "duration_ms": 800,
    },
    {
        "call_id": "l_sre2",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre1",
        "start_time": 1.0,
        "duration_ms": 900,
    },
    {
        "call_id": "a_tel",
        "kind": "agent",
        "agent_id": "telemetry",
        "parent_id": "",
        "start_time": 2.0,
        "duration_ms": 3000,
        "output_text": "output:telemetry:findings",
    },
    {
        "call_id": "l_tel",
        "kind": "llm",
        "agent_id": "telemetry",
        "parent_id": "a_tel",
        "start_time": 2.1,
        "duration_ms": 2800,
    },
    {
        "call_id": "a_back",
        "kind": "agent",
        "agent_id": "backend",
        "parent_id": "",
        "start_time": 5.0,
        "duration_ms": 3000,
        "output_text": "output:backend:findings",
    },
    {
        "call_id": "a_sre2",
        "kind": "agent",
        "agent_id": "sre",
        "parent_id": "",
        "start_time": 5.0,
        "duration_ms": 9000,
        "output_text": "output:sre:delegate_to_risk_assessment",
    },
    {
        "call_id": "l_sre3",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre2",
        "start_time": 10.0,
        "duration_ms": 3000,
    },
    {
        "call_id": "a_risk",
        "kind": "agent",
        "agent_id": "risk_assessment",
        "parent_id": "",
        "start_time": 13.1,
        "duration_ms": 2000,
        "output_text": "output:risk_assessment:ok",
    },
    {
        "call_id": "a_sre3",
        "kind": "agent",
        "agent_id": "sre",
        "parent_id": "",
        "start_time": 16.0,
        "duration_ms": 1000,
        "output_text": "output:sre:final",
    },
]


def trajectory():
    return SessionTrajectory(session_id="s1", steps=STEPS)


def agent_node(call_id, text, agent="x"):
    return KGNode(
        node_type="AgentCall",
        node={"id": call_id, "agentName": agent, "inputContent": f"input:{agent}:{text}"},
    )


# --- who the caller is --------------------------------------------------------------------


def test_the_caller_is_the_last_delegating_llm_call_that_ended_before_the_call_started():
    assert find_caller(trajectory(), "a_tel") == ("l_sre2", "a_sre1", "sre")
    # backend runs after telemetry but is part of the same turn of sre: still the call that delegated
    assert find_caller(trajectory(), "a_back") == ("l_sre2", "a_sre1", "sre")
    assert find_caller(trajectory(), "a_risk") == ("l_sre3", "a_sre2", "sre")


def test_no_caller_for_the_first_call_or_an_unknown_call():
    assert (
        find_caller(trajectory(), "a_sre1") is None
    )  # the user request: nothing delegated before it
    assert find_caller(trajectory(), "nope") is None


def test_a_call_is_never_its_own_caller():
    steps = [
        {
            "call_id": "a",
            "kind": "agent",
            "agent_id": "sre",
            "parent_id": "",
            "start_time": 1.0,
            "duration_ms": 1000,
            "output_text": "output:sre:delegate_to_x",
        },
        {
            "call_id": "l",
            "kind": "llm",
            "agent_id": "sre",
            "parent_id": "a",
            "start_time": 0.0,
            "duration_ms": 500,
        },
    ]
    assert find_caller(SessionTrajectory(session_id="s", steps=steps), "a") is None


def test_delegation_outputs():
    assert is_delegation_output("output:sre:delegate_to_telemetry")
    assert is_delegation_output("transfer_to_billing")
    assert not is_delegation_output("output:sre:## Summary")
    assert not is_delegation_output(None)


# --- the brief ----------------------------------------------------------------------------


def test_only_a_written_brief_counts():
    assert brief_text(agent_node("a", BRIEF)) == BRIEF
    assert brief_text(agent_node("a", "sre-u1-exec-resume-2")) is None  # router woken up
    assert brief_text(agent_node("a", "")) is None
    # a conversation is a brief like any other text: the content decides (see skip_reason)
    conversation = '{"args": [{"messages": [{"name": "agent_a", "content": "hello"}]}]}'
    assert brief_text(agent_node("a", conversation)) == "[agent_a]: hello"


# --- the extractor ------------------------------------------------------------------------


def test_the_claims_of_a_brief_are_linked_to_the_caller():
    llm = FakeLLM()
    extractor = DelegationClaimExtractor(llm)

    (claim,) = extractor.extract(
        agent_node("a_risk", BRIEF, "risk_assessment"), None, trajectory=trajectory()
    )

    assert isinstance(claim, DelegationClaim)
    assert claim.claim_text == "The root cause is a bad deploy of v2.18.3."
    assert (claim.generated_by, claim.caller_call, claim.caller_agent) == (
        "l_sre3",
        "a_sre2",
        "sre",
    )
    assert claim.delegated_to == "a_risk"
    assert claim.verbatim_source == SNIPPET
    assert (
        "Caller agent: sre" in llm.calls[0][1]
        and "Receiving agent: risk_assessment" in llm.calls[0][1]
    )
    assert BRIEF in llm.calls[0][1]
    assert extractor.last_usage.calls == 1 and extractor.usage.calls == 1


def test_a_quote_that_is_not_in_the_brief_is_dropped_but_the_claim_is_kept():
    llm = FakeLLM(
        [
            {
                "claim_text": "It is a bad deploy.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "verbatim_source": "The deploy was bad.",
            }
        ]
    )
    (claim,) = DelegationClaimExtractor(llm).extract(
        agent_node("a_risk", BRIEF), None, trajectory=trajectory()
    )
    assert claim.verbatim_source is None


def test_no_request_when_there_is_nothing_to_extract_from():
    llm = FakeLLM()
    extractor = DelegationClaimExtractor(llm)
    assert extractor.extract(agent_node("a_risk", BRIEF), None) == []  # no trajectory
    assert (
        extractor.extract(agent_node("a_sre1", BRIEF), None, trajectory=trajectory()) == []
    )  # no caller
    assert (
        extractor.extract(
            agent_node("a_sre3", "sre-u1-exec-resume-2"), None, trajectory=trajectory()
        )
        == []
    )
    assert llm.calls == [] and extractor.last_usage.calls == 0


def test_the_ids_are_deterministic():
    a = DelegationClaimExtractor(FakeLLM()).extract(
        agent_node("a_risk", BRIEF), None, trajectory=trajectory()
    )[0]
    b = DelegationClaimExtractor(FakeLLM()).extract(
        agent_node("a_risk", BRIEF), None, trajectory=trajectory()
    )[0]
    assert a.id == b.id


def test_it_applies_to_agent_calls_only():
    extractor = DelegationClaimExtractor(FakeLLM())
    assert extractor.applies_to(agent_node("a", BRIEF))
    assert not extractor.applies_to(KGNode(node_type="LLMCall", node={"id": "l"}))
    assert extractor.uses_trajectory is True


# --- in the pipeline ----------------------------------------------------------------------


def test_the_pipeline_lists_the_claims_on_the_receiving_call_apart_from_the_claims():
    data = SessionData(
        session_id="s1",
        agent_spans=[
            {
                "id": "a_sre1",
                "startTime": 0.0,
                "agentName": "sre",
                "inputContent": "input:sre:the user request",
                "outputContent": "output:sre:delegate_to_telemetry",
            },
            {
                "id": "a_tel",
                "startTime": 2.0,
                "agentName": "telemetry",
                "inputContent": f"input:telemetry:{BRIEF}",
                "outputContent": "output:telemetry:findings",
            },
        ],
        llm_spans=[],
    )
    steps = [s for s in STEPS if s["call_id"] in {"a_sre1", "l_sre1", "l_sre2", "a_tel"}]
    llm = FakeLLM()
    pipeline = SymbolicExtractor(extractors=[DelegationClaimExtractor(llm)])

    result = pipeline.process(data, trajectory=SessionTrajectory(session_id="s1", steps=steps))

    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a_sre1"].symbolic_state_elements["delegation_claims"] == []
    (claim,) = by_id["a_tel"].symbolic_state_elements["delegation_claims"]
    assert claim["generated_by"] == "l_sre2" and claim["caller_agent"] == "sre"
    assert (
        "claims" not in by_id["a_tel"].symbolic_state_elements
    )  # not mixed with the claims of LLM calls
    assert result.llm_usage_by_element["delegation_claims"].calls == 1
    assert pipeline.store.by_kind("delegation_claims")[0].node_id == "a_tel"
    assert pipeline.store.by_kind("claims") == []


# --- content-based skip -------------------------------------------------------------------


def claim(claim_id, text="A claim.", source=None):
    return Claim(
        id=claim_id,
        claim_text=text,
        claim_force="assertive",
        claim_kind="factual",
        verbatim_source=source,
    )


HISTORY = " ".join(f"word{i}" for i in range(30))  # the input of the caller, long enough to count
RISK_CALLER = Caller("l_sre3", "a_sre2", "sre")


def with_input(call_id, text):
    return SessionTrajectory(
        session_id="s1",
        steps=[{**s, "input_text": text} if s["call_id"] == call_id else s for s in STEPS],
    )


def test_a_blob_is_found_through_what_changes_around_it():
    wrapped = f'{{"next": "b", "kwargs": {{}}, "messages": "{HISTORY}"}}'
    other = f'prefix text: "{HISTORY}" and one more step: done.'
    assert contains_blob(HISTORY, wrapped)
    assert contains_blob(HISTORY, other)
    assert contains_blob(HISTORY, HISTORY.upper())  # case does not matter


def test_a_blob_needs_to_be_long_and_whole():
    assert not contains_blob(
        "Root cause: a bad deploy.", "Root cause: a bad deploy. More text."
    )  # too short
    assert not contains_blob(HISTORY, " ".join(HISTORY.split()[::2]))  # only half of the words
    assert not contains_blob(HISTORY, "something unrelated " * 40)


def test_relay_when_every_claim_of_the_caller_is_in_the_brief():
    store = SymbolicSessionStore("s1")
    store.add("claims", claim("c1", source=SNIPPET), "l_sre3")
    assert skip_reason(BRIEF, RISK_CALLER, store, trajectory()) == "relay"
    store.add("claims", claim("c2", source="A finding the brief does not carry."), "l_sre3")
    assert skip_reason(BRIEF, RISK_CALLER, store, trajectory()) is None  # one is missing: read it
    # a claim with no excerpt cannot be found: not a relay either
    other = SymbolicSessionStore("s1")
    other.add("claims", claim("c3"), "l_sre3")
    assert skip_reason(BRIEF, RISK_CALLER, other, trajectory()) is None


def test_no_relay_without_claims_of_the_caller():
    assert skip_reason(BRIEF, RISK_CALLER, SymbolicSessionStore("s1"), trajectory()) is None
    assert skip_reason(BRIEF, RISK_CALLER, None, trajectory()) is None


def test_history_when_the_input_of_the_caller_is_in_the_brief():
    traj = with_input("a_sre2", f"input:sre:{HISTORY}")
    assert skip_reason(f"{HISTORY}\n\nNext: assess the risk.", RISK_CALLER, None, traj) == "history"
    assert skip_reason(BRIEF, RISK_CALLER, None, traj) is None
    # a short input of the caller proves nothing
    short = with_input("a_sre2", "input:sre:resume")
    assert skip_reason("resume the work, please", RISK_CALLER, None, short) is None


def test_a_skipped_brief_costs_no_request():
    llm = FakeLLM()
    store = SymbolicSessionStore("s1")
    store.add("claims", claim("c1", source=SNIPPET), "l_sre3")
    assert (
        DelegationClaimExtractor(llm).extract(
            agent_node("a_risk", BRIEF), store, trajectory=trajectory()
        )
        == []
    )
    assert llm.calls == []


# --- source claims ------------------------------------------------------------------------


def linked_store():
    """The claims of telemetry, of the caller itself and of its own brief."""
    store = SymbolicSessionStore("s1")
    store.add("claims", claim("early", "First turn of sre."), "l_sre1")
    store.add("claims", claim("tel", "Latency is normal."), "l_tel")
    store.add(
        "claims", claim("late", "Written after the delegation."), "a_sre3"
    )  # not an LLM call before it
    store.add("claims", claim("own", "The caller's own claim."), "l_sre3")
    store.add(
        "delegation_claims",
        DelegationClaim(
            id="inb",
            claim_text="From its brief.",
            claim_force="assertive",
            claim_kind="factual",
            caller_agent="user",
            caller_call="a_sre1",
            delegated_to="a_sre2",
            generated_by="l_sre1",
        ),
        "a_sre2",
    )
    return store


def test_the_source_claims_are_the_earlier_ones_the_brief_of_the_caller_and_its_own():
    found = [c.id for c in source_claims(linked_store(), trajectory(), RISK_CALLER)]
    assert found == ["early", "tel", "inb", "own"]


def test_the_brief_and_own_claims_are_kept_when_the_earlier_ones_are_capped():
    store = linked_store()
    for i in range(MAX_SOURCE_CLAIMS + 5):
        store.add("claims", claim(f"e{i}", f"Earlier {i}."), "l_tel")
    found = [c.id for c in source_claims(store, trajectory(), RISK_CALLER)]
    assert (
        len(found) == MAX_SOURCE_CLAIMS
        and found[-2:] == ["inb", "own"]
        and found[-3] == f"e{MAX_SOURCE_CLAIMS + 4}"
    )


def test_the_source_claims_are_capped_to_the_most_recent():
    store = linked_store()
    for i in range(MAX_SOURCE_CLAIMS + 5):
        store.add("claims", claim(f"x{i}", f"Claim {i}."), "l_sre3")
    found = [c.id for c in source_claims(store, trajectory(), RISK_CALLER)]
    assert len(found) == MAX_SOURCE_CLAIMS and found[-1] == f"x{MAX_SOURCE_CLAIMS + 4}"


def test_the_delegation_claims_are_related_to_the_source_claims_in_the_pipeline():
    data = SessionData(
        session_id="s1",
        agent_spans=[
            {
                "id": "a_sre1",
                "startTime": 0.0,
                "agentName": "sre",
                "inputContent": "input:sre:the user request",
                "outputContent": "output:sre:delegate_to_telemetry",
            },
            {
                "id": "a_tel",
                "startTime": 2.0,
                "agentName": "telemetry",
                "inputContent": f"input:telemetry:{BRIEF}",
                "outputContent": "output:telemetry:findings",
            },
        ],
        llm_spans=[],
    )
    steps = [s for s in STEPS if s["call_id"] in {"a_sre1", "l_sre1", "l_sre2", "a_tel"}]
    store = SymbolicSessionStore("s1")
    store.add(
        "claims",
        claim("c1", "The deploy broke it.", source="A finding the brief does not carry."),
        "l_sre1",
    )
    relations = FakeRelationLLM([{"new": "N1", "other": "E1", "relation": "same"}])
    pipeline = SymbolicExtractor(
        extractors=[DelegationClaimExtractor(FakeLLM())],
        relation_extractors=[ClaimRelationExtractor(relations)],
    )

    result = pipeline.process(
        data, store=store, trajectory=SessionTrajectory(session_id="s1", steps=steps)
    )

    node = {n.node_id: n for n in result.nodes}["a_tel"]
    (delegation,) = node.symbolic_state_elements["delegation_claims"]
    assert node.source_claims == ["c1"]
    assert (
        delegation["same_as"] == ["c1"]
        and delegation["conflicts_with"] == []
        and delegation["derived_from"] == []
    )
    assert pipeline.store.related("input_claim", "a_tel") == ["c1"]
    assert (
        "EARLIER CLAIMS" in relations.calls[0][1]
        and "The deploy broke it." in relations.calls[0][1]
    )
    assert result.llm_usage_by_element["relations"].calls == 1
    assert {n.node_id: n for n in result.nodes}["a_sre1"].source_claims is None


class FakeRelationLLM:
    def __init__(self, relations):
        self.relations = relations
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(
            data={"relations": self.relations},
            usage=LLMUsage(calls=1, prompt_tokens=50, completion_tokens=10),
        )


# --- quotes copied from a transcript ------------------------------------------------------

TRANSCRIPT = (
    "The other three said (verbatim):\n"
    "Finance: \"No. I'm holding 12% flat. Deal Desk at 15% is still above my gate. "
    "CSM's 25% is irreversible and off the table.\"\n"
)


def test_the_speaker_label_and_turn_quotes_are_removed_when_the_sentence_is_in_the_brief():
    assert without_speaker('Finance: "Deal Desk at 15% is still above my gate."', TRANSCRIPT) == (
        "Deal Desk at 15% is still above my gate."
    )
    assert without_speaker(
        'Finance: "CSM\'s 25% is irreversible and off the table."', TRANSCRIPT
    ) == ("CSM's 25% is irreversible and off the table.")


def test_nothing_is_accepted_that_is_not_in_the_brief():
    assert (
        without_speaker('Finance: "Deal Desk at 15% is above my gate."', TRANSCRIPT) is None
    )  # reworded
    assert (
        without_speaker("Deal Desk at 15% is still above my gate.", TRANSCRIPT) is None
    )  # no label: nothing to remove
    assert without_speaker('Finance: "I can\'t book a deal."', TRANSCRIPT) is None  # shortened


def test_a_claim_quoted_with_its_speaker_keeps_the_sentence_as_verbatim_source():
    llm = FakeLLM(
        [
            {
                "claim_text": "Deal Desk at 15% is above Finance's gate.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "verbatim_source": 'Finance: "Deal Desk at 15% is still above my gate."',
            }
        ]
    )
    (claim,) = DelegationClaimExtractor(llm).extract(
        agent_node("a_risk", TRANSCRIPT), None, trajectory=trajectory()
    )
    assert claim.verbatim_source == "Deal Desk at 15% is still above my gate."


# --- the brief in the context of the LLM calls of the receiving agent call -----------------

BRIEF_STEPS = [
    {
        "call_id": "a_x",
        "kind": "agent",
        "agent_id": "x",
        "parent_id": "",
        "start_time": 0.0,
        "duration_ms": 9000,
    },
    {
        "call_id": "l_x1",
        "kind": "llm",
        "agent_id": "x",
        "parent_id": "a_x",
        "start_time": 1.0,
        "duration_ms": 500,
    },
    {
        "call_id": "l_x2",
        "kind": "llm",
        "agent_id": "x",
        "parent_id": "a_x",
        "start_time": 2.0,
        "duration_ms": 500,
    },
    {
        "call_id": "l_x3",
        "kind": "llm",
        "agent_id": "x",
        "parent_id": "a_x",
        "start_time": 3.0,
        "duration_ms": 500,
    },
    {
        "call_id": "l_other",
        "kind": "llm",
        "agent_id": "y",
        "parent_id": "a_y",
        "start_time": 4.0,
        "duration_ms": 500,
    },
]


def brief_store():
    store = SymbolicSessionStore("s1")
    store.add(
        "delegation_claims",
        DelegationClaim(
            id="b1",
            claim_text="Rollback target was stable.",
            claim_force="assertive",
            claim_kind="factual",
            caller_agent="sre",
            caller_call="a_sre",
            delegated_to="a_x",
            generated_by="l_sre",
        ),
        "a_x",
    )
    store.add("claims", claim("k1", "v2.18.2 was stable for 5+ days."), "l_x1")
    store.add("claims", claim("k3", "Rollback looks safe."), "l_x3")
    store.add("claims", claim("ko", "Another agent."), "l_other")
    return store


def test_the_brief_claims_are_the_input_of_the_llm_calls_of_the_agent_call_that_generated_claims():
    store = brief_store()
    SymbolicExtractor._link_brief_to_llm_calls(
        store, SessionTrajectory(session_id="s1", steps=BRIEF_STEPS)
    )
    assert store.related("input_claim", "l_x1") == ["b1"]
    assert store.related("input_claim", "l_x3") == ["b1"]
    assert (
        store.related("input_claim", "l_x2") == []
    )  # no claim generated: nothing to relate, skipped
    assert store.related("input_claim", "l_other") == []  # another agent call


def test_the_llm_nodes_get_the_brief_claims_added_to_their_input_claims():
    store = brief_store()
    SymbolicExtractor._link_brief_to_llm_calls(
        store, SessionTrajectory(session_id="s1", steps=BRIEF_STEPS)
    )
    pipeline = SymbolicExtractor()
    pipeline.store = store
    first = AnnotatedNode(node_type="LLMCall", node={"id": "l_x1"}, input_claims=["k0"])
    skipped = AnnotatedNode(node_type="LLMCall", node={"id": "l_x2"}, input_claims=[])
    store.add("claims", claim("k0", "Earlier."), "l_early")

    pipeline.annotate_source_claims([first, skipped])

    assert first.input_claims == ["k0", "b1"]  # what the call already had, then the brief
    assert skipped.input_claims == []


# --- the claims of a delegated agent call that travel back to its caller -------------------

REPLY = "Root cause is v2.18.3. Rollback is safe."
REPLY_STEPS = [
    {
        "call_id": "a_sre",
        "kind": "agent",
        "agent_id": "sre",
        "parent_id": "",
        "start_time": 0.0,
        "duration_ms": 1500,
        "output_text": "output:sre:delegate_to_risk",
    },
    {
        "call_id": "l_sre1",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre",
        "start_time": 0.1,
        "duration_ms": 800,
    },
    {
        "call_id": "a_risk",
        "kind": "agent",
        "agent_id": "risk",
        "parent_id": "",
        "start_time": 2.0,
        "duration_ms": 4000,
        "output_text": f"output:risk:{REPLY}",
    },
    {
        "call_id": "l_r1",
        "kind": "llm",
        "agent_id": "risk",
        "parent_id": "a_risk",
        "start_time": 2.1,
        "duration_ms": 500,
    },
    {
        "call_id": "l_r2",
        "kind": "llm",
        "agent_id": "risk",
        "parent_id": "a_risk",
        "start_time": 3.0,
        "duration_ms": 2900,
    },
    {
        "call_id": "a_sre2",
        "kind": "agent",
        "agent_id": "sre",
        "parent_id": "",
        "start_time": 6.0,
        "duration_ms": 3000,
        "output_text": "output:sre:final",
    },
    {
        "call_id": "l_sre2",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre2",
        "start_time": 6.1,
        "duration_ms": 500,
    },
    {
        "call_id": "l_sre3",
        "kind": "llm",
        "agent_id": "sre",
        "parent_id": "a_sre2",
        "start_time": 7.0,
        "duration_ms": 500,
    },
]


def reply_trajectory():
    return SessionTrajectory(session_id="s1", steps=REPLY_STEPS)


def reply_store():
    store = SymbolicSessionStore("s1")
    store.add(
        "claims",
        claim("r_out", "The root cause is v2.18.3.", source="Root cause is v2.18.3."),
        "l_r2",
    )
    store.add(
        "claims",
        claim("r_in", "The dashboards were checked.", source="Checked the dashboards."),
        "l_r2",
    )
    store.add("claims", claim("r_noq", "No quote for this one."), "l_r2")
    store.add("claims", claim("s_final", "The deploy caused the incident.", source="x"), "l_sre3")
    return store


def test_a_call_is_delegated_to_when_its_output_is_a_reply_and_its_caller_is_another_agent():
    caller, reply = delegated_reply(reply_trajectory(), "a_risk")
    assert caller.agent == "sre" and reply == REPLY
    assert (
        delegated_reply(reply_trajectory(), "a_sre") is None
    )  # the first call: nobody delegated to it
    assert (
        delegated_reply(reply_trajectory(), "a_sre2") is None
    )  # the resumed caller itself, not a worker
    assert delegated_reply(reply_trajectory(), "l_r1") is None  # not an agent call


def test_only_the_claims_found_in_the_reply_travel_back():
    found = [c.id for c in reply_claims(reply_store(), reply_trajectory(), "a_risk", REPLY)]
    assert found == ["r_out"]  # the quote of r_in is not in the reply, r_noq has none


def test_the_reply_is_read_by_the_next_llm_call_of_the_caller_that_generated_claims():
    caller, _ = delegated_reply(reply_trajectory(), "a_risk")
    # l_sre2 has no claim, l_sre1 is before the reply: the receiver is l_sre3
    assert reply_receiver(reply_store(), reply_trajectory(), "a_risk", caller) == "l_sre3"
    assert reply_receiver(SymbolicSessionStore("s1"), reply_trajectory(), "a_risk", caller) is None


def test_the_reply_claims_are_the_input_of_the_receiving_call_and_the_others_stay():
    store = reply_store()
    pipeline = SymbolicExtractor()
    pipeline.store, pipeline.trajectory = store, reply_trajectory()

    pipeline._link_replies(store, reply_trajectory())

    assert store.related("input_claim", "l_sre3") == ["r_out"]
    assert store.related("reply_claim", "a_risk") == ["r_out"]

    risk = AnnotatedNode(
        node_type="LLMCall",
        node={"id": "l_r2"},
        symbolic_state_elements={"claims": [{"id": "r_out"}, {"id": "r_in"}, {"id": "r_noq"}]},
    )
    final = AnnotatedNode(
        node_type="LLMCall",
        node={"id": "l_sre3"},
        input_claims=[],
        symbolic_state_elements={"claims": [{"id": "s_final"}]},
    )
    pipeline.annotate_replies([risk, final])
    pipeline.annotate_source_claims([risk, final])

    assert [c["reply"] for c in risk.symbolic_state_elements["claims"]] == [True, False, False]
    assert "reply" not in final.symbolic_state_elements["claims"][0]  # sre was not delegated to
    assert final.input_claims == ["r_out"]


def test_no_reply_link_without_trajectory_or_claims():
    store = SymbolicSessionStore("s1")
    SymbolicExtractor()._link_replies(store, None)
    SymbolicExtractor()._link_replies(store, reply_trajectory())
    assert store.related("input_claim", "l_sre3") == []
