"""``SystemTaskExtractor``: the standing tasks of an agent, from its system prompt (one request per agent)."""

import json

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import (
    AgentSystemPromptExtractor,
    SystemTask,
    SystemTaskExtractor,
    agent_system_tasks,
)
from symbolic_extractor.extractors.system_tasks import render_system_task_prompt
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import AgentSystemPrompt, ExtractionRequest, KGNode, SessionData, SessionTrajectory
from symbolic_extractor.store import HAS_SYSTEM_TASK, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

ROLE = "You are an itinerary specialist. Find routes and report their travel times."


class FakeLLM:
    def __init__(self, tasks=None, data=None):
        self.data = data if data is not None else {
            "tasks": tasks if tasks is not None else [{"task_text": "Report the travel times of the routes.", "acceptance_criteria": ["  Use the graph. ", " "]}]
        }
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(data=self.data, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))


def otel(system=ROLE):
    parts = json.dumps([{"type": "text", "content": system}])
    return f"{parts}\n\n{json.dumps([{'role': 'user', 'parts': [{'type': 'text', 'content': 'q'}]}])}"


STEPS = [
    {"call_id": "a1", "kind": "agent", "agent_id": "itinerary", "parent_id": "", "start_time": 0.5},
    {"call_id": "l1", "kind": "llm", "agent_id": "itinerary", "parent_id": "a1", "start_time": 1.0},
    {"call_id": "a2", "kind": "agent", "agent_id": "moderator", "parent_id": "", "start_time": 1.5},
    {"call_id": "l2", "kind": "llm", "agent_id": "moderator", "parent_id": "a2", "start_time": 2.0},
    {"call_id": "a3", "kind": "agent", "agent_id": "itinerary", "parent_id": "", "start_time": 3.0},
    {"call_id": "l3", "kind": "llm", "agent_id": "itinerary", "parent_id": "a3", "start_time": 3.5},
]


def trajectory():
    return SessionTrajectory(session_id="s1", steps=STEPS)


def session():
    def agent(call_id, start):
        return {"id": call_id, "startTime": start, "inputContent": "input:x:do it", "outputContent": "output:x:done"}

    return SessionData(
        session_id="s1",
        agent_spans=[agent("a1", 0.5), agent("a2", 1.5), agent("a3", 3.0)],
        llm_spans=[
            {"id": "l1", "startTime": 1.0, "prompt": otel(ROLE), "completion": "a"},
            {"id": "l2", "startTime": 2.0, "prompt": otel("You are a moderator."), "completion": "b"},
            {"id": "l3", "startTime": 3.5, "prompt": otel(ROLE), "completion": "c"},
        ],
    )


def llm_node(call_id):
    return KGNode(node_type="LLMCall", node={"id": call_id, "prompt": otel(ROLE)})


def store_with_prompt(call_id="l1", agent="itinerary", truncated=False):
    store = SymbolicSessionStore("s1")
    prompt = AgentSystemPrompt(id="p1", agent_id=agent, system_prompt=ROLE, truncated=truncated, read_from=call_id)
    store.add("system_prompts", prompt, call_id)
    return store, prompt


# --- the extractor ------------------------------------------------------------------------


def test_the_system_prompt_just_read_gives_the_standing_tasks_of_its_agent():
    llm, (store, prompt) = FakeLLM(), store_with_prompt()
    extractor = SystemTaskExtractor(llm)

    (task,) = extractor.extract(llm_node("l1"), store)

    assert isinstance(task, SystemTask)
    assert task.task_text == "Report the travel times of the routes."
    assert task.acceptance_criteria == ["Use the graph."]  # blank criteria dropped
    assert (task.agent_id, task.system_prompt_id, task.read_from, task.truncated) == ("itinerary", "p1", "l1", False)
    assert len(llm.calls) == 1 and ROLE in llm.calls[0][1] and "Agent: itinerary" in llm.calls[0][1]
    assert extractor.last_usage.calls == 1 and extractor.usage.calls == 1


def test_no_request_on_a_call_that_did_not_read_the_prompt():
    llm, (store, _) = FakeLLM(), store_with_prompt("l1")
    extractor = SystemTaskExtractor(llm)

    assert extractor.extract(llm_node("l2"), store) == []  # a later call of the same agent
    assert extractor.extract(llm_node("l1"), None) == []
    assert extractor.extract(llm_node("l1"), SymbolicSessionStore("s1")) == []  # nothing read (webex)
    assert llm.calls == [] and extractor.last_usage.calls == 0


def test_the_ids_are_deterministic_and_depend_on_the_prompt_and_the_text():
    store, _ = store_with_prompt()
    a = SystemTaskExtractor(FakeLLM()).extract(llm_node("l1"), store)[0]
    b = SystemTaskExtractor(FakeLLM()).extract(llm_node("l1"), store)[0]
    c = SystemTaskExtractor(FakeLLM([{"task_text": "Another duty."}])).extract(llm_node("l1"), store)[0]
    assert a.id == b.id != c.id


def test_a_truncated_prompt_marks_its_tasks_and_the_request():
    llm, (store, _) = FakeLLM(), store_with_prompt(truncated=True)
    (task,) = SystemTaskExtractor(llm).extract(llm_node("l1"), store)
    assert task.truncated is True
    assert "System prompt looks cut off: yes" in llm.calls[0][1]


def test_invalid_items_are_dropped_and_a_prompt_without_duty_gives_no_task():
    store, _ = store_with_prompt()
    mixed = FakeLLM([{"task_text": ""}, {"task_text": "A duty."}, {"acceptance_criteria": ["x"]}])
    assert [t.task_text for t in SystemTaskExtractor(mixed).extract(llm_node("l1"), store)] == ["A duty."]
    assert SystemTaskExtractor(FakeLLM([])).extract(llm_node("l1"), store) == []
    assert SystemTaskExtractor(FakeLLM(data={"tasks": "not a list"})).extract(llm_node("l1"), store) == []


def test_it_applies_to_llm_calls_only():
    extractor = SystemTaskExtractor(FakeLLM())
    assert extractor.applies_to(llm_node("l1"))
    assert not extractor.applies_to(KGNode(node_type="AgentCall", node={"id": "a1"}))


def test_the_request_marks_a_clean_prompt_as_not_cut():
    _, prompt = store_with_prompt()
    assert "cut off" not in render_system_task_prompt(prompt)


def test_agent_system_tasks_selects_the_tasks_of_one_agent():
    store, _ = store_with_prompt()
    (task,) = SystemTaskExtractor(FakeLLM()).extract(llm_node("l1"), store)
    store.add("system_tasks", task, "l1")
    assert agent_system_tasks(store, "itinerary") == [task] and agent_system_tasks(store, "moderator") == []


# --- in the pipeline ----------------------------------------------------------------------


def pipeline(llm):
    return SymbolicExtractor(extractors=[AgentSystemPromptExtractor(), SystemTaskExtractor(llm)])


def test_process_asks_once_per_agent_and_links_the_tasks_to_every_call_of_the_agent():
    llm = FakeLLM()
    p = pipeline(llm)

    result = p.process(session(), trajectory=trajectory())

    assert len(llm.calls) == 2  # itinerary (read on l1, again on l3: no request) and moderator
    by_id = {n.node_id: n for n in result.nodes}
    (task,) = by_id["l1"].symbolic_state_elements["system_tasks"]
    assert by_id["l3"].symbolic_state_elements["system_tasks"] == []
    assert by_id["l2"].symbolic_state_elements["system_tasks"][0]["agent_id"] == "moderator"
    # the first call of the agent ran before the prompt was read, and is linked all the same
    assert by_id["a1"].system_tasks == by_id["a3"].system_tasks == [task["id"]]
    assert len(by_id["a2"].system_tasks) == 1 and by_id["a2"].system_tasks != by_id["a1"].system_tasks
    assert p.store.related(HAS_SYSTEM_TASK, "a1") == [task["id"]]
    assert by_id["l1"].system_tasks is None  # only agent calls have the key
    assert result.llm_usage.calls == 2 and result.llm_usage_by_element["system_tasks"].calls == 2


def test_system_tasks_are_not_tasks_of_the_task_machinery():
    p = pipeline(FakeLLM())
    p.process(session(), trajectory=trajectory())
    assert p.store.by_kind("tasks") == []
    assert len(p.store.by_kind("system_tasks")) == 2


def test_without_a_trajectory_nothing_is_extracted_or_linked():
    llm = FakeLLM()
    result = pipeline(llm).process(session())
    assert llm.calls == [] and all(n.system_tasks is None for n in result.nodes)


def test_without_the_extractor_the_agent_calls_have_no_key():
    result = SymbolicExtractor(extractors=[AgentSystemPromptExtractor()]).process(session(), trajectory=trajectory())
    assert all(n.system_tasks is None for n in result.nodes)


def test_live_process_gives_the_same_tasks_and_annotates_the_calls_that_come_after_the_prompt():
    llm = FakeLLM()
    live = pipeline(llm)
    nodes = {}
    for kind, spans in (("AgentCall", session().agent_spans), ("LLMCall", session().llm_spans)):
        for span in spans:
            nodes[span["id"]] = (span["startTime"], KGNode(node_type=kind, node=span))
    returned, at_return = {}, {}
    for call_id, (_, node) in sorted(nodes.items(), key=lambda kv: kv[1][0]):
        returned[call_id] = live.live_process(node, ExtractionRequest(session_id="s1"), trajectory()).nodes[0]
        at_return[call_id] = list(returned[call_id].system_tasks or [])

    assert len(llm.calls) == 2
    assert at_return["a1"] == []  # arrived before the prompt of its agent was read
    assert len(at_return["a3"]) == 1  # arrived after
    assert len(returned["a1"].system_tasks) == 1  # put on the node, in place, when its agent's prompt was read
    assert len(live.store.related(HAS_SYSTEM_TASK, "a1")) == 1

    whole = pipeline(FakeLLM())
    whole.process(session(), trajectory=trajectory())
    ids = lambda p: sorted(s.id for s in p.store.by_kind("system_tasks"))  # noqa: E731
    assert ids(live) == ids(whole)
