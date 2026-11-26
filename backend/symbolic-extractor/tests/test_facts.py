import json
import logging

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.embeddings import TextEmbedder, embed_result
from symbolic_extractor.extractors import ToolResultFactExtractor
from symbolic_extractor.extractors.facts import tool_output_text
from symbolic_extractor.models import AnnotatedNode, ExtractionResult, KGNode, SessionData


def tool(node_id="t1", **fields):
    return KGNode(node_type="ToolCall", node={"id": node_id, **fields})


def test_fact_from_a_tool_output_is_the_decoded_text():
    # the KG stores a string result JSON-encoded, with quotes and escapes
    stored = json.dumps("Fare: $75\nUpgrade: +$15")
    (fact,) = ToolResultFactExtractor().extract(tool(outputContent=stored))

    assert fact.fact_text == "Fare: $75\nUpgrade: +$15"
    assert fact.verbatim_source == fact.fact_text
    assert fact.fact_source_type == "tool_result"
    assert fact.grounded_in == "t1"


def test_tool_output_key_is_preferred_and_non_string_outputs_are_kept_as_stored():
    assert tool_output_text({"toolOutput": '"a"', "outputContent": '"b"'}) == "a"
    assert tool_output_text({"outputContent": '{"price": 75}'}) == '{"price": 75}'
    assert tool_output_text({"outputContent": "not json at all"}) == "not json at all"
    assert tool_output_text({"outputContent": {"price": 75}}) == '{"price": 75}'
    assert tool_output_text({"outputContent": None, "toolOutput": '"x"'}) == "x"


def test_no_output_gives_no_fact():
    extractor = ToolResultFactExtractor()
    assert extractor.extract(tool()) == []
    assert extractor.extract(tool(outputContent="")) == []
    assert extractor.extract(tool(outputContent='"   "')) == []
    assert extractor.extract(KGNode(node_type="ToolCall", node={"outputContent": '"x"'})) == []


def test_applies_to_tool_calls_only_and_costs_nothing():
    extractor = ToolResultFactExtractor()
    assert extractor.applies_to(tool())
    assert not extractor.applies_to(KGNode(node_type="LLMCall", node={"id": "l1"}))
    assert not extractor.applies_to(KGNode(node_type="AgentCall", node={"id": "a1"}))
    extractor.extract(tool(outputContent='"x"'))
    assert extractor.last_usage.calls == 0


def test_fact_ids_are_deterministic_and_depend_on_call_and_text():
    extractor = ToolResultFactExtractor()
    first = extractor.extract(tool(outputContent='"x"'))[0].id
    assert first == extractor.extract(tool(outputContent='"x"'))[0].id
    assert first != extractor.extract(tool(outputContent='"y"'))[0].id
    assert first != extractor.extract(tool("t2", outputContent='"x"'))[0].id


def test_pipeline_stores_facts_without_llm_usage():
    data = SessionData(
        session_id="s1",
        tool_spans=[
            {"id": "t1", "outputContent": json.dumps("Fare: $75")},
            {"id": "t2"},  # no output
        ],
    )
    pipeline = SymbolicExtractor(extractors=[ToolResultFactExtractor()])

    result = pipeline.process(data)

    store = pipeline.store
    assert [s.kind for s in store] == ["facts"]
    (stored,) = store.for_node("t1", "facts")
    assert stored.element.fact_text == "Fare: $75"
    node = next(n for n in result.nodes if n.node_id == "t1")
    assert node.symbolic_state_elements["facts"][0]["id"] == stored.id
    assert node.symbolic_state_elements["facts"][0]["grounded_in"] == "t1"
    assert node.llm_usage == {}
    assert result.llm_usage.calls == 0 and result.llm_usage_by_element == {}


def test_facts_are_not_embedded_and_do_not_warn(caplog):
    class Backend:
        model_id = "fake"

        def encode(self, sentences):
            return [[1.0, 0.0] for _ in sentences]

    result = ExtractionResult(
        session_id="s1",
        nodes=[
            AnnotatedNode(
                node_type="ToolCall",
                node={"id": "t1"},
                symbolic_state_elements={"facts": [{"id": "f1", "fact_text": "x"}]},
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        embeddings = embed_result(result, TextEmbedder(Backend()))

    assert embeddings.elements == []
    assert "facts" not in caplog.text


# ---- input_facts -------------------------------------------------------------------------

from symbolic_extractor import SymbolicSessionStore  # noqa: E402
from symbolic_extractor.symbolic_state import track_symbolic_state, track_input_facts  # noqa: E402
from symbolic_extractor.extractors import Fact  # noqa: E402

_SYSTEM = json.dumps([{"type": "text", "content": "system prompt"}])


def _input(*texts, tool_responses=()):
    messages = [{"role": "user", "parts": [{"type": "text", "content": t}]} for t in texts]
    messages += [
        {"role": "tool", "parts": [{"type": "tool_call_response", "id": "x", "response": r}]}
        for r in tool_responses
    ]
    return f"{_SYSTEM}\n\n{json.dumps(messages)}"


def _llm(call_id, owner, start, input_text, duration_ms=100.0):
    return {"call_id": call_id, "kind": "llm", "parent_id": owner, "start_time": start,
            "duration_ms": duration_ms, "input_text": input_text}


def _tool(call_id, owner, start, duration_ms=100.0):
    return {"call_id": call_id, "kind": "tool", "parent_id": owner, "start_time": start,
            "duration_ms": duration_ms}


def _facts(**facts):
    """facts: fact_id=(tool_call_id, text)."""
    store = SymbolicSessionStore("s")
    for fid, (tool_id, text) in facts.items():
        store.add("facts", Fact(id=fid, fact_text=text, verbatim_source=text, grounded_in=tool_id), tool_id)
    return store


def test_fact_is_input_of_the_later_llm_calls_of_its_agent_call():
    steps = [
        _llm("l1", "a1", 1.0, _input("q")),
        _tool("t1", "a1", 2.0),
        _llm("l2", "a1", 3.0, _input("q", tool_responses=["Fare: $75\nUpgrade: +$15"])),
        _llm("l3", "a1", 4.0, _input("q", tool_responses=["Fare: $75\nUpgrade: +$15"])),
    ]
    got = track_input_facts(steps, _facts(f1=("t1", "Fare: $75\nUpgrade: +$15")))
    assert got == {"l1": [], "l2": ["f1"], "l3": ["f1"]}


def test_fact_does_not_cross_agent_calls():
    # same agent, another agent call: the tool result is not handed over
    steps = [
        _tool("t1", "a1", 1.0),
        _llm("l1", "a1", 2.0, _input("q", tool_responses=["Fare: $75"])),
        _llm("l2", "a2", 5.0, _input("q", tool_responses=["Fare: $75"])),
    ]
    got = track_input_facts(steps, _facts(f1=("t1", "Fare: $75")))
    assert got == {"l1": ["f1"], "l2": []}


def test_fact_must_be_in_the_input_and_the_tool_call_must_have_ended():
    steps = [
        _llm("early", "a1", 1.0, _input("Fare: $75")),  # text present but tool not run yet
        _tool("t1", "a1", 2.0, duration_ms=3000.0),  # ends at 5.0
        _llm("running", "a1", 3.0, _input("Fare: $75")),  # tool still running
        _llm("absent", "a1", 6.0, _input("something else")),  # tool done, output not in input
        _llm("present", "a1", 7.0, _input("x", tool_responses=["Fare: $75"])),
    ]
    got = track_input_facts(steps, _facts(f1=("t1", "Fare: $75")))
    assert got == {"early": [], "running": [], "absent": [], "present": ["f1"]}


def test_fact_in_the_system_prompt_is_not_an_input_fact():
    steps = [_tool("t1", "a1", 1.0), _llm("l1", "a1", 2.0, _input("q"))]
    steps[1]["input_text"] = f"{json.dumps([{'type': 'text', 'content': 'Fare: $75'}])}\n\n" + json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "q"}]}]
    )
    assert track_input_facts(steps, _facts(f1=("t1", "Fare: $75"))) == {"l1": []}


def test_several_facts_keep_tool_order_and_whitespace_is_normalised():
    steps = [
        _tool("t2", "a1", 2.0),
        _tool("t1", "a1", 1.0),
        _llm("l1", "a1", 3.0, _input("q", tool_responses=["A  line\n\nB line", "Other   result"])),
    ]
    store = _facts(f2=("t2", "Other result"), f1=("t1", "A line B line"))
    assert track_input_facts(steps, store) == {"l1": ["f1", "f2"]}


def test_llm_call_without_agent_call_is_left_out_and_tracking_is_part_of_the_state_trace():
    steps = [
        {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
        _tool("t1", "a1", 1.0),
        _llm("l1", "a1", 2.0, _input("q", tool_responses=["Fare: $75"])),
        _llm("orphan", "", 2.0, _input("Fare: $75")),
    ]
    trace = track_symbolic_state(steps, _facts(f1=("t1", "Fare: $75")))
    assert trace.input_facts == {"l1": ["f1"]}
    assert "orphan" not in trace.input_facts


def test_pipeline_sets_input_facts_on_llm_calls():
    from symbolic_extractor.models import SessionTrajectory

    response = "Fare: $75"
    data = SessionData(
        session_id="s1",
        tool_spans=[{"id": "t1", "startTime": 2.0, "outputContent": json.dumps(response)}],
        llm_spans=[
            {"id": "l1", "startTime": 1.0, "completion": "x", "prompt": _input("q")},
            {"id": "l2", "startTime": 3.0, "completion": "y",
             "prompt": _input("q", tool_responses=[response])},
        ],
    )
    trajectory = SessionTrajectory(
        session_id="s1",
        steps=[
            {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
            _llm("l1", "a1", 1.0, _input("q")),
            _tool("t1", "a1", 2.0),
            _llm("l2", "a1", 3.0, _input("q", tool_responses=[response])),
        ],
    )
    pipeline = SymbolicExtractor(extractors=[ToolResultFactExtractor()])

    result = pipeline.process(data, trajectory=trajectory)

    by_id = {n.node_id: n for n in result.nodes}
    (fact_id,) = [f["id"] for f in by_id["t1"].symbolic_state_elements["facts"]]
    assert by_id["l1"].input_facts == []
    assert by_id["l2"].input_facts == [fact_id]
    assert by_id["t1"].input_facts is None  # only LLM calls have it
    assert pipeline.store.related("input_fact", "l2") == [fact_id]
    assert result.symbolic_state.input_facts == {"l1": [], "l2": [fact_id]}


def test_without_fact_match_every_finished_fact_of_the_agent_call_is_an_input_fact():
    steps = [
        _llm("early", "a1", 1.0, _input("q")),  # tool not finished yet
        _tool("t1", "a1", 2.0),
        _llm("absent", "a1", 3.0, _input("something else")),  # output not in the input
        _llm("other", "a2", 3.0, _input("something else")),  # another agent call
    ]
    store = _facts(f1=("t1", "Fare: $75"))
    assert track_input_facts(steps, store, match_facts=False) == {
        "early": [], "absent": ["f1"], "other": []
    }
    assert track_input_facts(steps, store) == {"early": [], "absent": [], "other": []}
    assert track_symbolic_state(steps, store, match_facts=False).input_facts["absent"] == ["f1"]


def test_pipeline_match_input_facts_flag():
    from symbolic_extractor.models import SessionTrajectory

    data = SessionData(
        session_id="s1",
        tool_spans=[{"id": "t1", "startTime": 2.0, "outputContent": json.dumps("Fare: $75")}],
        llm_spans=[{"id": "l2", "startTime": 3.0, "completion": "y", "prompt": _input("q")}],
    )
    trajectory = SessionTrajectory(
        session_id="s1",
        steps=[
            {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
            _tool("t1", "a1", 2.0),
            _llm("l2", "a1", 3.0, _input("q")),
        ],
    )
    default = SymbolicExtractor(extractors=[ToolResultFactExtractor()]).process(data, trajectory=trajectory)
    assert {n.node_id: n for n in default.nodes}["l2"].input_facts == []

    pipeline = SymbolicExtractor(extractors=[ToolResultFactExtractor()], match_input_facts=False)
    result = pipeline.process(data, trajectory=trajectory)
    l2 = {n.node_id: n for n in result.nodes}["l2"]
    assert len(l2.input_facts) == 1
    assert pipeline.store.related("input_fact", "l2") == l2.input_facts
