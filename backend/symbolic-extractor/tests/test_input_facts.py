import json

from symbolic_extractor import SessionData, SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.extractors import Fact, ToolResultFactExtractor
from symbolic_extractor.models import SessionTrajectory
from symbolic_extractor.symbolic_state import track_input_facts, track_symbolic_state

SYSTEM = json.dumps([{"type": "text", "content": "system prompt"}])


def llm_input(*texts, tool_responses=()):
    messages = [{"role": "user", "parts": [{"type": "text", "content": t}]} for t in texts]
    messages += [
        {"role": "tool", "parts": [{"type": "tool_call_response", "id": "x", "response": r}]}
        for r in tool_responses
    ]
    return f"{SYSTEM}\n\n{json.dumps(messages)}"


def llm(call_id, owner, start, input_text, duration_ms=100.0):
    return {"call_id": call_id, "kind": "llm", "parent_id": owner, "start_time": start,
            "duration_ms": duration_ms, "input_text": input_text}


def tool(call_id, owner, start, duration_ms=100.0):
    return {"call_id": call_id, "kind": "tool", "parent_id": owner, "start_time": start,
            "duration_ms": duration_ms}


def facts(**items):
    """items: fact_id=(tool_call_id, text)."""
    store = SymbolicSessionStore("s")
    for fid, (tool_id, text) in items.items():
        store.add("facts", Fact(id=fid, fact_text=text, verbatim_source=text, grounded_in=tool_id), tool_id)
    return store


def test_fact_is_input_of_the_later_llm_calls_of_its_agent_call():
    steps = [
        llm("l1", "a1", 1.0, llm_input("q")),
        tool("t1", "a1", 2.0),
        llm("l2", "a1", 3.0, llm_input("q", tool_responses=["Fare: $75\nUpgrade: +$15"])),
        llm("l3", "a1", 4.0, llm_input("q", tool_responses=["Fare: $75\nUpgrade: +$15"])),
    ]
    got = track_input_facts(steps, facts(f1=("t1", "Fare: $75\nUpgrade: +$15")))
    assert got == {"l1": [], "l2": ["f1"], "l3": ["f1"]}


def test_fact_does_not_cross_agent_calls():
    # even if the text were present, the tool result belongs to its own agent call only
    steps = [
        tool("t1", "a1", 1.0),
        llm("l1", "a1", 2.0, llm_input("q", tool_responses=["Fare: $75"])),
        llm("l2", "a2", 5.0, llm_input("q", tool_responses=["Fare: $75"])),
    ]
    got = track_input_facts(steps, facts(f1=("t1", "Fare: $75")))
    assert got == {"l1": ["f1"], "l2": []}


def test_fact_must_be_in_the_input_and_the_tool_call_must_have_ended():
    steps = [
        llm("early", "a1", 1.0, llm_input("Fare: $75")),  # tool not run yet
        tool("t1", "a1", 2.0, duration_ms=3000.0),  # ends at 5.0
        llm("running", "a1", 3.0, llm_input("Fare: $75")),  # tool still running
        llm("absent", "a1", 6.0, llm_input("something else")),  # tool done, output not in input
        llm("present", "a1", 7.0, llm_input("x", tool_responses=["Fare: $75"])),
    ]
    got = track_input_facts(steps, facts(f1=("t1", "Fare: $75")))
    assert got == {"early": [], "running": [], "absent": [], "present": ["f1"]}


def test_fact_in_the_system_prompt_is_not_an_input_fact():
    system = json.dumps([{"type": "text", "content": "Fare: $75"}])
    messages = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "q"}]}])
    steps = [tool("t1", "a1", 1.0), llm("l1", "a1", 2.0, f"{system}\n\n{messages}")]
    assert track_input_facts(steps, facts(f1=("t1", "Fare: $75"))) == {"l1": []}


def test_several_facts_keep_tool_order_and_whitespace_is_normalised():
    steps = [
        tool("t2", "a1", 2.0),
        tool("t1", "a1", 1.0),
        llm("l1", "a1", 3.0, llm_input("q", tool_responses=["A  line\n\nB line", "Other   result"])),
    ]
    store = facts(f2=("t2", "Other result"), f1=("t1", "A line B line"))
    assert track_input_facts(steps, store) == {"l1": ["f1", "f2"]}


def test_llm_call_without_agent_call_is_left_out_and_tracking_is_part_of_the_state_trace():
    steps = [
        {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
        tool("t1", "a1", 1.0),
        llm("l1", "a1", 2.0, llm_input("q", tool_responses=["Fare: $75"])),
        llm("orphan", "", 2.0, llm_input("Fare: $75")),
    ]
    trace = track_symbolic_state(steps, facts(f1=("t1", "Fare: $75")))
    assert trace.input_facts == {"l1": ["f1"]}


def test_pipeline_sets_input_facts_on_llm_calls():
    response = "Fare: $75"
    data = SessionData(
        session_id="s1",
        tool_spans=[{"id": "t1", "startTime": 2.0, "outputContent": json.dumps(response)}],
        llm_spans=[
            {"id": "l1", "startTime": 1.0, "completion": "x", "prompt": llm_input("q")},
            {"id": "l2", "startTime": 3.0, "completion": "y",
             "prompt": llm_input("q", tool_responses=[response])},
        ],
    )
    trajectory = SessionTrajectory(
        session_id="s1",
        steps=[
            {"call_id": "a1", "kind": "agent", "agent_id": "a", "parent_id": "", "start_time": 0.5},
            llm("l1", "a1", 1.0, llm_input("q")),
            tool("t1", "a1", 2.0),
            llm("l2", "a1", 3.0, llm_input("q", tool_responses=[response])),
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
