"""The enrichment of a fact: the tool, its arguments, and the claim that announced the call."""

import json

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import ToolResultFactExtractor
from symbolic_extractor.extractors.facts import tool_arguments, tool_name
from symbolic_extractor.llm import TextReply
from symbolic_extractor.models import AnnotatedNode, Claim, Fact, KGNode, SessionTrajectory
from symbolic_extractor.relations.fact_claim_links import (
    SYSTEM_PROMPT,
    BatchFactClaimLinker,
    FactClaimLinker,
    describe_fact,
)
from symbolic_extractor.store import ANNOUNCES, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage


def _tool(**fields):
    return KGNode(node_type="ToolCall", node={"id": "t1", "outputContent": '"5"', **fields})


def test_the_fact_records_the_tool_and_its_arguments():
    (fact,) = ToolResultFactExtractor().extract(
        _tool(toolName="calculate", toolArguments=json.dumps({"args": [], "kwargs": {"expression": "3 + 2"}}))
    )
    assert fact.tool_name == "calculate"
    assert fact.tool_arguments == {"expression": "3 + 2"}
    assert fact.fact_text == "5"  # the output is untouched: it is what agents quote


def test_the_tool_name_falls_back_to_the_span_name():
    assert tool_name({"toolName": "calculate", "name": "Tool call (other) 1"}) == "calculate"
    assert tool_name({"name": "Tool call (read_document) ce5d06af"}) == "read_document"
    assert tool_name({"name": "something else"}) is None
    assert tool_name({}) is None


def test_arguments_come_from_the_first_key_that_has_them():
    assert tool_arguments({"inputContent": '{"args": [], "kwargs": {}}'}) == {}
    assert tool_arguments({"inputParams": '{"args": [], "kwargs": {"file_path": "a"}}'}) == {"file_path": "a"}
    assert tool_arguments({"toolArguments": '{"args": [3], "kwargs": {"x": 1}}'}) == {"x": 1, "args": [3]}
    assert tool_arguments({"toolArguments": '{"city": "Luminos"}'}) == {"city": "Luminos"}
    assert tool_arguments({"toolArguments": "not json"}) == {}
    assert tool_arguments({"toolArguments": "[1, 2]"}) == {}
    assert tool_arguments({}) == {}


def _store():
    store = SymbolicSessionStore("s1")
    store.add(
        "claims",
        Claim(id="c1", claim_text="The agent will calculate the total travel time.", claim_force="committing",
              generated_by="llmcall:1"),
        "llmcall:1",
    )
    fact = Fact(id="f1", fact_text="5", verbatim_source="5", grounded_in="toolcall:1", tool_name="calculate",
                tool_arguments={"expression": "3 + 2"})
    store.add("facts", fact, "toolcall:1")
    return store, fact


def test_describe_fact_gives_the_tool_arguments_and_reason_before_the_output():
    store, fact = _store()
    store.link(ANNOUNCES, "c1", "toolcall:1")

    assert describe_fact(fact, store) == (
        "Tool: calculate\n"
        'Arguments: {"expression": "3 + 2"}\n'
        "Reason given by the agent: The agent will calculate the total travel time.\n"
        "Output:\n5"
    )
    # without the relation, or without a store, the reason is left out
    assert "Reason" not in describe_fact(fact, _store()[0])
    assert describe_fact(fact) == 'Tool: calculate\nArguments: {"expression": "3 + 2"}\nOutput:\n5'


def test_a_fact_without_context_is_just_its_text():
    fact = Fact(id="f", fact_text="Fare: $75", verbatim_source="Fare: $75", grounded_in="t")
    assert describe_fact(fact, SymbolicSessionStore("s")) == "Fare: $75"


class RecordingLLM:
    def __init__(self):
        self.prompts = []

    def complete_text(self, system, user):
        self.prompts.append((system, user))
        if "PAIR P1" in user:
            return TextReply(text="P1: YES", usage=LLMUsage(calls=1))
        return TextReply(text="YES", usage=LLMUsage(calls=1))


def test_the_linker_shows_the_model_the_enriched_fact():
    store, fact = _store()
    store.link(ANNOUNCES, "c1", "toolcall:1")
    claim = Claim(id="c9", claim_text="3 + 2 equals 5.", claim_force="assertive", claim_kind="factual",
                  generated_by="llmcall:2")
    llm = RecordingLLM()

    FactClaimLinker(llm).extract(store, [fact, claim])
    (system, user) = llm.prompts[0]
    assert "Tool: calculate" in user and 'Arguments: {"expression": "3 + 2"}' in user
    assert "Reason given by the agent: The agent will calculate" in user
    assert "never evidence" in system

    batch = RecordingLLM()
    BatchFactClaimLinker(batch).link([(fact, claim)], store)
    assert "Tool: calculate" in batch.prompts[0][1]


def test_the_system_prompt_still_asks_for_one_word():
    assert SYSTEM_PROMPT.rstrip().endswith("Reply with a single word: YES or NO. Nothing else.")


def test_announced_by_is_set_on_the_facts_of_a_node():
    store, fact = _store()
    store.link(ANNOUNCES, "c1", "toolcall:1")
    extractor = SymbolicExtractor()
    extractor.store = store
    extractor.trajectory = SessionTrajectory(session_id="s1", steps=[])
    node = AnnotatedNode(
        node_type="ToolCall",
        node={"id": "toolcall:1"},
        symbolic_state_elements={"facts": [fact.model_dump(mode="json")]},
    )
    extractor.annotate_announces([node])
    assert node.symbolic_state_elements["facts"][0]["announced_by"] == ["c1"]

    store2, fact2 = _store()  # no relation: the key is there, empty
    extractor.store = store2
    node2 = AnnotatedNode(
        node_type="ToolCall", node={"id": "toolcall:1"}, symbolic_state_elements={"facts": [fact2.model_dump(mode="json")]}
    )
    extractor.annotate_announces([node2])
    assert node2.symbolic_state_elements["facts"][0]["announced_by"] == []
