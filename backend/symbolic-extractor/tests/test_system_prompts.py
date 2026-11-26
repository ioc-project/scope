"""``AgentSystemPromptExtractor``: the system prompt of each agent, read from the input of its LLM calls."""

import json

from symbolic_extractor import ExtractionRequest, SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.extractors import (
    AgentSystemPrompt,
    AgentSystemPromptExtractor,
    agent_system_prompt,
    split_system_prompt,
)
from symbolic_extractor.models import KGNode, SessionData, SessionTrajectory

ROLE = "You are an itinerary specialist."
LONG = "[role] You are an autonomous SRE incident-response agent.\n\n## Using skills\n" + "Skills help. " * 400


def otel(system=ROLE, *messages):
    """Trip planner shape: system parts JSON, a blank line, then the messages JSON."""
    parts = json.dumps([{"type": "text", "content": system}])
    msgs = [{"role": "user", "parts": [{"type": "text", "content": m}]} for m in messages or ("q",)]
    return f"{parts}\n\n{json.dumps(msgs)}"


def openai(system=ROLE, user="q"):
    return json.dumps([{"role": "system", "content": system}, {"role": "user", "content": user}])


def cut(raw, size=4000):
    """What the KG does to a long SRE input: the first characters only."""
    return raw[:size]


# --- the split ----------------------------------------------------------------------------


def test_otel_system_parts_are_read_and_the_conversation_is_not():
    assert split_system_prompt(otel(ROLE, "Plan a trip")) == (ROLE, False)


def test_several_otel_system_parts_are_joined_by_a_newline():
    parts = json.dumps([{"type": "text", "content": "Part one."}, {"type": "text", "content": "Part two."}])
    assert split_system_prompt(f"{parts}\n\n{json.dumps([{'role': 'user', 'parts': []}])}") == (
        "Part one.\nPart two.",
        False,
    )


def test_openai_system_message_of_a_complete_input():
    assert split_system_prompt(openai(ROLE)) == (ROLE, False)


def test_openai_system_message_of_a_cut_input_is_complete_when_its_string_is_closed():
    raw = cut(openai("[role] Short role.\n\nBe brief.", user="x" * 5000))
    assert len(raw) == 4000
    assert split_system_prompt(raw) == ("[role] Short role.\n\nBe brief.", False)


def test_openai_system_message_cut_inside_is_read_as_far_as_it_goes_and_flagged():
    raw = cut(openai(LONG, user="q"))
    text, truncated = split_system_prompt(raw)
    assert truncated is True
    assert LONG.startswith(text) and len(text) > 3900
    assert text.startswith("[role] You are an autonomous SRE incident-response agent.\n\n")


def test_a_cut_in_the_middle_of_an_escape_sequence_is_still_decoded():
    system = "Role.\n" + "é" * 3000  # json.dumps escapes it as \u00e9
    full = openai(system)
    for size in range(len(full) - 1, len(full) - 40, -1):
        # every possible cut position of the system string must give a prefix of it, or nothing
        found = split_system_prompt(full[: size if size < 200 else 200])
        assert found is None or system.startswith(found[0])
    raw = full[: len('[{"role": "system", "content": "Role.\\n') + 7]  # ends inside a \\u00e9
    text, truncated = split_system_prompt(raw)
    assert truncated and system.startswith(text) and text.startswith("Role.\n")


def test_inputs_with_nothing_to_read_give_none():
    assert split_system_prompt(None) is None
    assert split_system_prompt("") is None
    assert split_system_prompt("llm-input:moderator:f12ac8ce-0fcf-4022-a00e-88bfdc695d7d") is None  # webex reference
    assert split_system_prompt("just some text that is not JSON") is None
    assert split_system_prompt(json.dumps([{"role": "user", "content": "no system message"}])) is None
    assert split_system_prompt(json.dumps({"args": [{"messages": []}]})) is None
    assert split_system_prompt('[{"role": "user", "content": "cut before any system') is None
    assert split_system_prompt(openai("   ")) is None


# --- the extractor ------------------------------------------------------------------------

STEPS = [
    {"call_id": "a1", "kind": "agent", "agent_id": "itinerary", "parent_id": "", "start_time": 0.5},
    {"call_id": "l1", "kind": "llm", "agent_id": "itinerary", "parent_id": "a1", "start_time": 1.0},
    {"call_id": "l2", "kind": "llm", "agent_id": "itinerary", "parent_id": "a1", "start_time": 2.0},
    {"call_id": "a2", "kind": "agent", "agent_id": "moderator", "parent_id": "", "start_time": 3.0},
    {"call_id": "l3", "kind": "llm", "agent_id": "moderator", "parent_id": "a2", "start_time": 3.5},
    {"call_id": "l4", "kind": "llm", "agent_id": "", "parent_id": "a2", "start_time": 4.0},
]


def trajectory():
    return SessionTrajectory(session_id="s1", steps=STEPS)


def llm(call_id, prompt, start):
    return KGNode(node_type="LLMCall", node={"id": call_id, "startTime": start, "prompt": prompt, "completion": "x"})


def test_the_system_prompt_is_extracted_for_the_agent_of_the_call():
    extractor = AgentSystemPromptExtractor()
    (prompt,) = extractor.extract(llm("l1", otel(ROLE), 1.0), SymbolicSessionStore("s1"), trajectory=trajectory())

    assert isinstance(prompt, AgentSystemPrompt)
    assert (prompt.agent_id, prompt.system_prompt, prompt.truncated, prompt.read_from) == ("itinerary", ROLE, False, "l1")
    assert extractor.last_usage.calls == 0


def test_the_id_is_deterministic_and_depends_on_the_call_and_the_text():
    first = AgentSystemPromptExtractor().extract(llm("l1", otel(ROLE), 1.0), None, trajectory=trajectory())[0]
    again = AgentSystemPromptExtractor().extract(llm("l1", otel(ROLE), 1.0), None, trajectory=trajectory())[0]
    other = AgentSystemPromptExtractor().extract(llm("l1", otel("Another role."), 1.0), None, trajectory=trajectory())[0]
    assert first.id == again.id != other.id


def test_an_agent_that_has_a_system_prompt_in_the_store_gets_no_second_one():
    extractor, store = AgentSystemPromptExtractor(), SymbolicSessionStore("s1")
    (first,) = extractor.extract(llm("l1", otel(ROLE), 1.0), store, trajectory=trajectory())
    store.add("system_prompts", first, "l1")

    assert extractor.extract(llm("l2", otel(ROLE), 2.0), store, trajectory=trajectory()) == []
    (moderator,) = extractor.extract(llm("l3", otel("You are a moderator."), 3.5), store, trajectory=trajectory())
    assert moderator.agent_id == "moderator"
    assert agent_system_prompt(store, "itinerary") is first and agent_system_prompt(store, "moderator") is None


def test_a_call_without_a_readable_prompt_leaves_the_agent_open_for_its_next_call():
    extractor, store = AgentSystemPromptExtractor(), SymbolicSessionStore("s1")
    assert extractor.extract(llm("l1", "llm-input:itinerary:f12ac8ce-0fcf-4022-a00e-88bfdc695d7d", 1.0), store, trajectory=trajectory()) == []
    (prompt,) = extractor.extract(llm("l2", otel(ROLE), 2.0), store, trajectory=trajectory())
    assert prompt.read_from == "l2"


def test_nothing_is_extracted_when_the_agent_is_unknown():
    extractor = AgentSystemPromptExtractor()
    assert extractor.extract(llm("l1", otel(ROLE), 1.0), None, trajectory=None) == []
    assert extractor.extract(llm("l9", otel(ROLE), 1.0), None, trajectory=trajectory()) == []  # not in the trajectory
    assert extractor.extract(llm("l4", otel(ROLE), 4.0), None, trajectory=trajectory()) == []  # step without agent
    assert extractor.extract(KGNode(node_type="LLMCall", node={"prompt": otel(ROLE)}), None, trajectory=trajectory()) == []


def test_it_applies_to_llm_calls_only():
    extractor = AgentSystemPromptExtractor()
    assert extractor.applies_to(llm("l1", "x", 1.0))
    assert not extractor.applies_to(KGNode(node_type="AgentCall", node={"id": "a1"}))
    assert not extractor.applies_to(KGNode(node_type="ToolCall", node={"id": "t1"}))


def test_inputcontent_is_used_when_there_is_no_prompt():
    node = KGNode(node_type="LLMCall", node={"id": "l1", "inputContent": otel(ROLE)})
    (prompt,) = AgentSystemPromptExtractor().extract(node, None, trajectory=trajectory())
    assert prompt.system_prompt == ROLE


# --- in the pipeline ----------------------------------------------------------------------


def session():
    return SessionData(
        session_id="s1",
        llm_spans=[
            {"id": "l1", "startTime": 1.0, "prompt": otel(ROLE), "completion": "a"},
            {"id": "l2", "startTime": 2.0, "prompt": otel(ROLE, "q", "more"), "completion": "b"},
            {"id": "l3", "startTime": 3.5, "prompt": otel("You are a moderator."), "completion": "c"},
        ],
    )


def test_process_stores_one_system_prompt_per_agent_on_the_first_call_that_has_it():
    pipeline = SymbolicExtractor(extractors=[AgentSystemPromptExtractor()])

    result = pipeline.process(session(), trajectory=trajectory())

    by_id = {n.node_id: n for n in result.nodes}
    assert [p["system_prompt"] for p in by_id["l1"].symbolic_state_elements["system_prompts"]] == [ROLE]
    assert by_id["l2"].symbolic_state_elements["system_prompts"] == []  # itinerary already has one
    assert by_id["l3"].symbolic_state_elements["system_prompts"][0]["agent_id"] == "moderator"
    stored = {s.element.agent_id: s.element for s in pipeline.store.by_kind("system_prompts")}
    assert set(stored) == {"itinerary", "moderator"} and stored["itinerary"].read_from == "l1"
    assert result.llm_usage.calls == 0


def test_process_without_a_trajectory_extracts_nothing():
    result = SymbolicExtractor(extractors=[AgentSystemPromptExtractor()]).process(session())
    assert all(n.symbolic_state_elements["system_prompts"] == [] for n in result.nodes)


def test_live_process_gives_the_same_prompts_node_by_node():
    live = SymbolicExtractor(extractors=[AgentSystemPromptExtractor()])
    for span in session().llm_spans:
        live.live_process(KGNode(node_type="LLMCall", node=span), ExtractionRequest(session_id="s1"), trajectory())

    whole = SymbolicExtractor(extractors=[AgentSystemPromptExtractor()])
    whole.process(session(), trajectory=trajectory())
    prompts = lambda p: {s.element.agent_id: (s.element.system_prompt, s.element.read_from, s.element.id) for s in p.store.by_kind("system_prompts")}  # noqa: E731
    assert prompts(live) == prompts(whole) and len(prompts(live)) == 2


def test_other_extractors_still_run_without_a_trajectory_argument():
    class Plain:
        element_key = "plain"

        def __init__(self):
            self.args = None
            from symbolic_extractor.usage import LLMUsage

            self.last_usage = LLMUsage()

        def applies_to(self, node):
            return True

        def extract(self, node, store=None):  # no trajectory parameter
            self.args = (node.node_id, store is not None)
            return []

    plain = Plain()
    SymbolicExtractor(extractors=[plain, AgentSystemPromptExtractor()]).process(session(), trajectory=trajectory())
    assert plain.args is not None
