"""Tool calls an LLM asked for: the claims that announce them and their link to the ToolCall nodes."""

import json

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import ClaimExtractionPrompt, ElementExtractor
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AnnotatedNode, Claim, ClaimItem, KGNode, SessionTrajectory
from symbolic_extractor.store import ANNOUNCES, SymbolicSessionStore
from symbolic_extractor.tool_calls import match_tool_calls, requested_tool_calls
from symbolic_extractor.usage import LLMUsage


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(data=self.reply, usage=LLMUsage(calls=1))


def _output(text, *calls):
    parts = [{"type": "text", "content": text}] if text else []
    parts += [{"type": "tool_call", "id": i, "name": n, "arguments": a} for i, n, a in calls]
    return json.dumps([{"role": "assistant", "parts": parts, "finish_reason": "tool_call"}])


def _kwargs(**kwargs):
    return json.dumps({"args": [], "kwargs": kwargs})


OUTPUT = _output(
    "Now I will calculate the total travel time.",
    ("call_a", "calculate", {"expression": "3 + 2"}),
    ("call_b", "calculate", {"expression": "5"}),
)

STEPS = [
    {"call_id": "agentcall:1", "kind": "agent", "parent_id": "", "start_time": "10"},
    {
        "call_id": "llmcall:1",
        "kind": "llm",
        "parent_id": "agentcall:1",
        "start_time": "11",
        "output_text": OUTPUT,
    },
    # run in the opposite order of the requests: the arguments tell them apart
    {
        "call_id": "toolcall:5",
        "kind": "tool",
        "parent_id": "agentcall:1",
        "start_time": "12",
        "name": "calculate",
        "input_text": _kwargs(expression="5"),
    },
    {
        "call_id": "toolcall:32",
        "kind": "tool",
        "parent_id": "agentcall:1",
        "start_time": "12.5",
        "name": "calculate",
        "input_text": _kwargs(expression="3 + 2"),
    },
    {
        "call_id": "llmcall:2",
        "kind": "llm",
        "parent_id": "agentcall:1",
        "start_time": "13",
        "output_text": "done",
    },
    # a tool call of the next turn must not be taken by the first one
    {
        "call_id": "toolcall:late",
        "kind": "tool",
        "parent_id": "agentcall:1",
        "start_time": "14",
        "name": "calculate",
        "input_text": _kwargs(expression="5"),
    },
]


def test_requested_tool_calls_are_read_from_the_output():
    calls = requested_tool_calls(OUTPUT)
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("call_a", "calculate", {"expression": "3 + 2"}),
        ("call_b", "calculate", {"expression": "5"}),
    ]
    assert requested_tool_calls("llm-output:sre:1234") == []
    assert requested_tool_calls(_output("just text")) == []


def test_arguments_given_as_a_json_string_are_read():
    (call,) = requested_tool_calls(
        _output("t", ("call_a", "read_document", '{"file_path": "a.txt"}'))
    )
    assert call.arguments == {"file_path": "a.txt"}


def test_tool_calls_are_matched_by_arguments_inside_the_turn():
    trajectory = SessionTrajectory(session_id="s", steps=STEPS)
    assert match_tool_calls(trajectory, "llmcall:1") == {
        "call_a": "toolcall:32",
        "call_b": "toolcall:5",
    }


def test_an_extended_tool_name_matches_and_a_missing_tool_does_not():
    steps = [
        {"call_id": "agentcall:1", "kind": "agent", "parent_id": ""},
        {
            "call_id": "llmcall:1",
            "kind": "llm",
            "parent_id": "agentcall:1",
            "start_time": "1",
            "output_text": _output(
                "t",
                ("call_a", "list_documents", {}),
                ("call_b", "read_document", {"file_path": "x"}),
            ),
        },
        {
            "call_id": "toolcall:1",
            "kind": "tool",
            "parent_id": "agentcall:1",
            "start_time": "2",
            "name": "list_documents_schedule_agent",
            "input_text": _kwargs(),
        },
    ]
    # the read_document step has not run yet (session in progress): only the first request is matched
    assert match_tool_calls(SessionTrajectory(session_id="s", steps=steps), "llmcall:1") == {
        "call_a": "toolcall:1"
    }


def test_the_same_request_twice_gets_two_steps():
    steps = [
        {
            "call_id": "llmcall:1",
            "kind": "llm",
            "parent_id": "agentcall:1",
            "start_time": "1",
            "output_text": _output("t", ("call_a", "read", {"p": 1}), ("call_b", "read", {"p": 1})),
        },
        {
            "call_id": "toolcall:1",
            "kind": "tool",
            "parent_id": "agentcall:1",
            "start_time": "2",
            "name": "read",
            "input_text": _kwargs(p=1),
        },
        {
            "call_id": "toolcall:2",
            "kind": "tool",
            "parent_id": "agentcall:1",
            "start_time": "3",
            "name": "read",
            "input_text": _kwargs(p=1),
        },
    ]
    result = match_tool_calls(SessionTrajectory(session_id="s", steps=steps), "llmcall:1")
    assert result == {"call_a": "toolcall:1", "call_b": "toolcall:2"}


def test_the_tool_calls_are_shown_to_the_claim_extractor():
    llm = FakeLLM({"claims": []})
    node = KGNode(node_type="LLMCall", node={"id": "llmcall:1", "completion": OUTPUT})
    ElementExtractor(llm, ClaimExtractionPrompt()).extract(node)

    (_, user) = llm.calls[0]
    assert "TOOL CALLS REQUESTED BY THE LLM" in user
    assert 'id call_a: calculate({"expression": "3 + 2"})' in user
    # the text is the output; the JSON wrapper is not
    assert "Now I will calculate the total travel time." in user and "finish_reason" not in user


def test_an_output_with_only_tool_calls_is_not_sent_to_the_llm():
    llm = FakeLLM({"claims": []})
    node = KGNode(
        node_type="LLMCall",
        node={"id": "llmcall:1", "completion": _output("", ("call_a", "calculate", {}))},
    )
    assert ElementExtractor(llm, ClaimExtractionPrompt()).extract(node) == []
    assert llm.calls == []


def test_only_ids_of_the_requests_are_kept_and_only_for_commitments():
    reply = {
        "claims": [
            {
                "claim_text": "The agent will calculate the travel time.",
                "claim_force": "committing",
                "tool_call_ids": ["call_a", "call_zzz", "call_a"],
            },
            {
                "claim_text": "The trip takes 5 hours.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "tool_call_ids": ["call_b"],
            },
            {"claim_text": "Check it.", "claim_force": "directive", "tool_call_ids": None},
        ]
    }
    node = KGNode(node_type="LLMCall", node={"id": "llmcall:1", "completion": OUTPUT})
    claims = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(node)

    assert [c.tool_call_ids for c in claims] == [["call_a"], [], []]


def test_claim_item_accepts_a_single_id_or_null():
    base = {"claim_text": "x", "claim_force": "committing"}
    assert ClaimItem(**base, tool_call_ids="call_a").tool_call_ids == ["call_a"]
    assert ClaimItem(**base, tool_call_ids=None).tool_call_ids == []
    assert ClaimItem(**base).tool_call_ids == []


def _setup():
    store = SymbolicSessionStore("s1")
    store.add(
        "claims",
        Claim(
            id="c1",
            claim_text="I will calculate",
            claim_force="committing",
            generated_by="llmcall:1",
            tool_call_ids=["call_a", "call_b"],
        ),
        "llmcall:1",
    )
    store.add(
        "claims",
        Claim(id="c2", claim_text="other", claim_force="committing", generated_by="llmcall:2"),
        "llmcall:2",
    )
    extractor = SymbolicExtractor()
    extractor.store = store
    return extractor, store


def test_a_claim_announces_the_tool_calls_it_names():
    extractor, store = _setup()
    trajectory = SessionTrajectory(session_id="s1", steps=STEPS)
    extractor.trajectory = trajectory
    extractor._link_announces(store, trajectory)

    assert store.related(ANNOUNCES, "c1") == ["toolcall:32", "toolcall:5"]
    assert store.related(ANNOUNCES, "c2") == []

    node = AnnotatedNode(
        node_type="LLMCall",
        node={"id": "llmcall:1"},
        symbolic_state_elements={"claims": [{"id": "c1"}, {"id": "c2"}]},
    )
    extractor.annotate_announces([node])
    assert [c["announces"] for c in node.symbolic_state_elements["claims"]] == [
        ["toolcall:32", "toolcall:5"],
        [],
    ]


def test_a_tool_call_that_runs_later_is_linked_at_a_later_step():
    extractor, store = _setup()
    early = SessionTrajectory(session_id="s1", steps=STEPS[:2])
    extractor._link_announces(store, early)
    assert store.related(ANNOUNCES, "c1") == []

    extractor._link_announces(store, SessionTrajectory(session_id="s1", steps=STEPS))
    assert store.related(ANNOUNCES, "c1") == ["toolcall:32", "toolcall:5"]


def test_nothing_is_linked_without_a_trajectory():
    extractor, store = _setup()
    extractor._link_announces(store, None)
    assert store.related(ANNOUNCES, "c1") == []
