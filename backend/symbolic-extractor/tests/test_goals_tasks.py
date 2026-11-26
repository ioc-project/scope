import json

import pytest

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import (
    ElementExtractor,
    GoalExtractionPrompt,
    TaskExtractionPrompt,
)
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.extractors.tasks import skip_reason
from symbolic_extractor.models import KGNode, SessionData
from symbolic_extractor.usage import LLMUsage


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(data=self.reply, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))


AGENT_NODE = KGNode(
    node_type="AgentCall",
    node={
        "id": "a1",
        "agentName": "transport_agent",
        "inputContent": "input:transport_agent:Find round-trip transport to Lisbon, Fri evening to Sun evening, for 2.",
        "outputContent": "output:transport_agent:Two TAP flights found for 240 EUR each.",
        "success": "True",
        "contains_error": "False",
    },
)

HUMAN_INPUT = json.dumps(
    {"args": [{"messages": [{"type": "HumanMessage", "content": "Plan a weekend in Lisbon for 2, max 800 EUR."}]}], "kwargs": {}}
)
FINAL_RESPONSE = json.dumps({"next": "FINISH", "final_answer": "Here is your Lisbon itinerary: ..."})

CONVERSATION_NODE = KGNode(
    node_type="Conversation",
    node={"id": "conversation:s1", "query": HUMAN_INPUT, "response": FINAL_RESPONSE},
)

GOAL_REPLY = {
    "goals": [
        {
            "goal_text": "A complete weekend itinerary for 2 in Lisbon, with transport and lodging.",
            "success_criteria": ["Includes transport", "Includes lodging", "  ", "Total cost is at most 800 EUR"],
            "rationale": None,
            "inferred": False,
            "status": "achieved",
        }
    ]
}

TASK_REPLY = {
    "tasks": [
        {
            "task_text": "Find round-trip transport to Lisbon, Fri evening to Sun evening, for 2.",
            "acceptance_criteria": ["Fri evening to Sun evening", ""],
            "status": "complete",
        }
    ]
}


def test_goal_is_extracted_from_the_conversation():
    llm = FakeLLM(GOAL_REPLY)
    (goal,) = ElementExtractor(llm, GoalExtractionPrompt()).extract(CONVERSATION_NODE)

    assert goal.goal_text.startswith("A complete weekend itinerary")
    # blank criteria are dropped
    assert goal.success_criteria == ["Includes transport", "Includes lodging", "Total cost is at most 800 EUR"]
    assert goal.inferred is False and goal.status == "achieved"
    # the human message and the final answer are unwrapped from their JSON envelopes
    user = llm.calls[0][1]
    assert "Plan a weekend in Lisbon for 2, max 800 EUR." in user
    assert "Here is your Lisbon itinerary" in user
    assert "HumanMessage" not in user and "FINISH" not in user


def test_goal_prompt_passes_the_full_text_when_it_cannot_be_unwrapped():
    for query in ("input:sre:Restart the cache.", '{"args": []}', '{"args": [{"messages": [{"type": "AIMessage", "content": "x"}]}]}', "[1, 2]"):
        node = KGNode(
            node_type="Conversation",
            node={"id": "c", "query": query, "response": '{"type": "Command", "goto": "__end__"}'},
        )
        user = GoalExtractionPrompt().render(node)
        assert query in user
        assert '"goto": "__end__"' in user


def test_goal_prompt_needs_a_query():
    prompt = GoalExtractionPrompt()
    assert prompt.render(KGNode(node_type="Conversation", node={"id": "c", "response": "x"})) is None
    no_response = prompt.render(KGNode(node_type="Conversation", node={"id": "c", "query": "Do x"}))
    assert "(empty)" in no_response


def test_goal_defaults_and_deterministic_ids():
    reply = {"goals": [{"goal_text": "Ship v2", "success_criteria": ["v2 is deployed"]}]}
    extractor = ElementExtractor(FakeLLM(reply), GoalExtractionPrompt())
    first, second = extractor.extract(CONVERSATION_NODE), extractor.extract(CONVERSATION_NODE)

    assert first[0].status == "set" and first[0].inferred is False and first[0].rationale is None
    assert first[0].id == second[0].id


def test_goal_without_success_criteria_is_dropped():
    reply = {
        "goals": [
            {"goal_text": "no criteria", "success_criteria": []},
            {"goal_text": "blank criteria", "success_criteria": [" "]},
            {"goal_text": "bad status", "success_criteria": ["x"], "status": "pending"},
            {"goal_text": "ok", "success_criteria": ["x"]},
        ]
    }
    goals = ElementExtractor(FakeLLM(reply), GoalExtractionPrompt()).extract(CONVERSATION_NODE)
    assert [g.goal_text for g in goals] == ["ok"]


def test_task_is_owned_by_the_agent_call():
    llm = FakeLLM(TASK_REPLY)
    (task,) = ElementExtractor(llm, TaskExtractionPrompt()).extract(AGENT_NODE)

    assert task.owned_by == "a1"
    assert task.status == "complete"
    assert task.acceptance_criteria == ["Fri evening to Sun evening"]
    user = llm.calls[0][1]
    assert "transport_agent" in user and "Call success: True" in user


def test_task_defaults_and_deterministic_ids():
    reply = {"tasks": [{"task_text": "Check the database health."}]}
    extractor = ElementExtractor(FakeLLM(reply), TaskExtractionPrompt())
    first, second = extractor.extract(AGENT_NODE), extractor.extract(AGENT_NODE)

    assert first[0].status == "assigned" and first[0].acceptance_criteria == []
    assert first[0].id == second[0].id


def test_task_inferred_flag_defaults_to_false_and_is_kept_when_set():
    reply = {
        "tasks": [
            {"task_text": "Respond to the positions of the other agents.", "inferred": True},
            {"task_text": "Check the database health."},
        ]
    }
    inferred, stated = ElementExtractor(FakeLLM(reply), TaskExtractionPrompt()).extract(AGENT_NODE)

    assert inferred.inferred is True
    assert stated.inferred is False
    # the id does not depend on the flag
    assert inferred.id == ElementExtractor(FakeLLM(reply), TaskExtractionPrompt()).extract(AGENT_NODE)[0].id


def test_task_prompt_asks_for_the_inferred_flag():
    from symbolic_extractor.extractors.tasks import SYSTEM_PROMPT

    assert '"inferred": true|false' in SYSTEM_PROMPT and "cut off" in SYSTEM_PROMPT


_LONG = "The other agents said: " + "word " * 300


@pytest.mark.parametrize(
    "text, cut",
    [
        (_LONG + "Keep to 4-8 sentences.", False),  # long but ends with its instruction
        (_LONG + "and I can't rate a pac", True),  # cut in the middle of a word
        (_LONG + 'it ends here."', True),  # ends with a closing quote: the instruction after it is missing
        ("Find the cheapest train", False),  # short and terse: never taken as cut
        ("", False),
    ],
)
def test_looks_cut_off(text, cut):
    from symbolic_extractor.extractors.tasks import looks_cut_off

    assert looks_cut_off(text) is cut


def test_cut_off_input_is_told_to_the_llm_and_forces_inferred():
    node = KGNode(
        node_type="AgentCall",
        node={"id": "a7", "agentName": "finance", "inputContent": "input:finance:" + _LONG + "still hoping"},
    )
    reply = {"tasks": [{"task_text": "Respond to the other agents.", "inferred": False}]}  # the LLM did not notice
    llm = FakeLLM(reply)
    (task,) = ElementExtractor(llm, TaskExtractionPrompt()).extract(node)

    assert "Input looks cut off: yes" in llm.calls[0][1]
    assert task.inferred is True


def test_complete_input_is_not_flagged_as_cut_off():
    llm = FakeLLM({"tasks": [{"task_text": "Do it."}]})
    (task,) = ElementExtractor(llm, TaskExtractionPrompt()).extract(AGENT_NODE)

    assert "cut off" not in llm.calls[0][1] and task.inferred is False


def test_invalid_tasks_are_dropped():
    reply = {
        "tasks": [
            {"task_text": "", "status": "assigned"},
            {"task_text": "bad status", "status": "done"},
            {"task_text": "ok"},
        ]
    }
    tasks = ElementExtractor(FakeLLM(reply), TaskExtractionPrompt()).extract(AGENT_NODE)
    assert [t.task_text for t in tasks] == ["ok"]


def test_goals_only_apply_to_the_conversation_and_tasks_to_agent_calls():
    llm_node = KGNode(node_type="LLMCall", node={"id": "l1", "prompt": "p", "completion": "c"})
    goals = ElementExtractor(FakeLLM({}), GoalExtractionPrompt())
    tasks = ElementExtractor(FakeLLM({}), TaskExtractionPrompt())
    assert goals.applies_to(CONVERSATION_NODE) and not goals.applies_to(AGENT_NODE)
    assert tasks.applies_to(AGENT_NODE) and not tasks.applies_to(CONVERSATION_NODE)
    assert not goals.applies_to(llm_node) and not tasks.applies_to(llm_node)


def test_tasks_need_an_input_and_an_id():
    empty_input = KGNode(node_type="AgentCall", node={"id": "a2", "agentName": "x", "inputContent": " "})
    no_id = KGNode(node_type="AgentCall", node={"inputContent": "Do something"})
    llm = FakeLLM({})
    extractor = ElementExtractor(llm, TaskExtractionPrompt())
    assert extractor.extract(empty_input) == []
    assert llm.calls == []  # nothing to extract from: no request
    assert extractor.extract(no_id) == []


def _agent(input_content, output_content="output:x:done", node_id="a9"):
    return KGNode(
        node_type="AgentCall",
        node={"id": node_id, "agentName": "x", "inputContent": input_content, "outputContent": output_content},
    )


def test_task_input_is_cleaned_before_the_llm_sees_it():
    prompt = TaskExtractionPrompt()

    sre = prompt.render(_agent("input:telemetry:Analyze the anomalies. Establish: (1) error rate."))
    assert "Analyze the anomalies. Establish: (1) error rate." in sre and "input:telemetry" not in sre

    debate = prompt.render(
        _agent("input:finance:ROUND=1;TARGET=Finance;\n\nUser message:\nState only your opening.")
    )
    assert "ROUND=1" not in debate and "State only your opening." in debate

    history = json.dumps(
        {
            "args": [
                {
                    "messages": [
                        {"type": "HumanMessage", "content": "Plan a trip"},
                        {"type": "HumanMessage", "content": "Routes: A-B 3h", "name": "itinerary_agent"},
                    ],
                    "next": "schedule_agent",
                }
            ],
            "kwargs": {},
        }
    )
    chat = prompt.render(_agent(history, '{"role": "user", "content": "Schedule"}'))
    assert "[user]: Plan a trip\n[itinerary_agent]: Routes: A-B 3h" in chat
    assert "HumanMessage" not in chat

    # the output prefix is dropped, and an output that was not recorded is said so
    assert "done" in prompt.render(_agent("input:x:Do it", "output:x:done"))
    assert "(no output recorded)" in prompt.render(_agent("input:x:Do it", "(no content recorded)"))


@pytest.mark.parametrize(
    "input_content, output_content, reason",
    [
        ("input:sre:sre-u1-exec-resume-2", "output:sre:delegate_to_backend", "resume marker"),
        ("input:sre:sre-u1-exec-resume-2-resume-3-resume-4", "output:sre:long report", "resume marker"),
        ("input:moderator:moderator-u1-exec-resume-2", "output:moderator:x", "resume marker"),
        (" ", "output:x:y", "empty input"),
        ("input:x:", "output:x:y", "empty input"),
        (
            '{"args": [{"messages": [{"type": "HumanMessage", "content": "Q"}]}], "kwargs": {}}',
            '{"type": "Command", "goto": "itinerary_agent", "update": {"next": "itinerary_agent"}}',
            "routing command",
        ),
        (
            '{"args": [{"messages": [{"type": "HumanMessage", "content": "Q"}]}], "kwargs": {}}',
            '{"next": "schedule_agent", "final_answer": ""}',
            "routing command",
        ),
    ],
)
def test_calls_without_an_assignment_are_skipped(input_content, output_content, reason):
    node = _agent(input_content, output_content)
    assert skip_reason(node) == reason
    llm = FakeLLM(TASK_REPLY)
    assert ElementExtractor(llm, TaskExtractionPrompt()).extract(node) == []
    assert llm.calls == []  # no LLM request for them


@pytest.mark.parametrize(
    "input_content, output_content",
    [
        # the entry agent receiving the request, even though it only routes (SRE style)
        ("input:sre:Edge-gateway shows anomalies. Identify the remediation.", "output:sre:delegate_to_telemetry"),
        # the final turn of the moderator holds the answer for the user
        ('{"args": [{"messages": [{"type": "HumanMessage", "content": "Q"}]}]}', '{"next": "FINISH", "final_answer": "The answer"}'),
        # a specialist whose output was not recorded
        ('{"args": [{"messages": [{"type": "HumanMessage", "content": "Q"}]}]}', "(no content recorded)"),
    ],
)
def test_calls_with_an_assignment_are_not_skipped(input_content, output_content):
    assert skip_reason(_agent(input_content, output_content)) is None


def test_session_data_conversation_node():
    data = SessionData(session_id="s1", input_text="q", output_text="r", agent_spans=[AGENT_NODE.node])
    nodes = data.nodes()
    assert [n.node_type for n in nodes] == ["Conversation", "AgentCall"]
    assert nodes[0].node == {"id": "conversation:s1", "sessionId": "s1", "query": "q", "response": "r"}
    # no session-level text: no Conversation node
    assert [n.node_type for n in SessionData(session_id="s1", agent_spans=[AGENT_NODE.node]).nodes()] == ["AgentCall"]


def test_pipeline_stores_goals_and_tasks_under_their_keys():
    pipeline = SymbolicExtractor(
        extractors=[
            ElementExtractor(FakeLLM(GOAL_REPLY), GoalExtractionPrompt()),
            ElementExtractor(FakeLLM(TASK_REPLY), TaskExtractionPrompt()),
        ]
    )
    data = SessionData(
        session_id="s1", input_text=HUMAN_INPUT, output_text=FINAL_RESPONSE, agent_spans=[AGENT_NODE.node]
    )
    result = pipeline.process(data)

    conversation, agent = result.nodes
    assert conversation.node_type == "Conversation"
    assert [g["goal_text"][:10] for g in conversation.symbolic_state_elements["goals"]] == ["A complete"]
    assert set(conversation.llm_usage) == {"goals"}
    assert agent.symbolic_state_elements["tasks"][0]["owned_by"] == "a1"
    assert set(agent.llm_usage) == {"tasks"}
    assert pipeline.store.by_kind("goals") and pipeline.store.by_kind("tasks")


def test_earlier_tasks_of_the_session_are_offered_for_reuse():
    from symbolic_extractor.store import SymbolicSessionStore

    store = SymbolicSessionStore("s1")
    llm = FakeLLM(TASK_REPLY)
    extractor = ElementExtractor(llm, TaskExtractionPrompt())

    # first call of the session: nothing to reuse, no extra section
    (first,) = extractor.extract(AGENT_NODE, store)
    assert "TASKS ALREADY EXTRACTED" not in llm.calls[0][1]
    store.add("tasks", first, AGENT_NODE.node_id)

    # next call: the task of the first one is listed, with its criteria
    other = _agent("input:y:Find round-trip transport to Lisbon, Fri evening to Sun evening, for 2.", node_id="a2")
    extractor.extract(other, store)
    user = llm.calls[1][1]
    assert "TASKS ALREADY EXTRACTED" in user
    assert f"task_text: {first.task_text}" in user and "Fri evening to Sun evening" in user
    assert "exactly" in llm.calls[1][0].lower() or "EXACTLY" in llm.calls[1][0]

    # re-running the same call does not offer its own task back
    extractor.extract(AGENT_NODE, store)
    assert "TASKS ALREADY EXTRACTED" not in llm.calls[2][1]


def test_earlier_tasks_are_deduplicated_and_capped():
    from symbolic_extractor.extractors.tasks import MAX_EARLIER_TASKS, earlier_tasks
    from symbolic_extractor.models import Task
    from symbolic_extractor.store import SymbolicSessionStore

    store = SymbolicSessionStore("s1")
    for i in range(MAX_EARLIER_TASKS + 5):
        store.add("tasks", Task(id=f"t{i}", task_text=f"task {i}", owned_by=f"a{i}"), f"a{i}")
    store.add("tasks", Task(id="dup", task_text="task 0", owned_by="a99"), "a99")

    found = earlier_tasks(store, "other")
    assert len(found) == MAX_EARLIER_TASKS
    assert [t.task_text for t in found].count("task 0") == 1 and found[-1].task_text == "task 0"


def test_the_same_task_of_several_agents_is_listed_once_with_its_first_wording():
    from symbolic_extractor.extractors.tasks import earlier_tasks
    from symbolic_extractor.models import Task
    from symbolic_extractor.store import SymbolicSessionStore

    store = SymbolicSessionStore("s1")
    store.add("tasks", Task(id="t1", task_text="Find the cheapest train.", acceptance_criteria=["by 5pm"], owned_by="a"), "a")
    store.add("tasks", Task(id="t2", task_text="find the  cheapest train", owned_by="b"), "b")  # T2 = T1
    store.add("tasks", Task(id="t3", task_text="Book a hotel.", owned_by="c"), "c")

    found = earlier_tasks(store, "d")  # agent D sees T1 once, and the other task
    assert [t.id for t in found] == ["t1", "t3"]
    assert found[0].acceptance_criteria == ["by 5pm"]


def _agent_with_task(node_id, agent):
    return KGNode(
        node_type="AgentCall",
        node={"id": node_id, "agentName": agent, "inputContent": "input:x:Find the cheapest train.", "outputContent": "output:x:ok"},
    )


def test_identical_tasks_of_different_agent_calls_are_linked_with_same_as():
    from symbolic_extractor.store import SymbolicSessionStore

    texts = {"a": "Find the cheapest train.", "b": "find the cheapest  train", "c": "Book a hotel."}

    class PerNodeLLM:
        def complete_json(self, system, user):
            node = next(k for k in texts if f"Agent call id: {k}\n" in user)
            return LLMReply(data={"tasks": [{"task_text": texts[node]}]}, usage=LLMUsage())

    nodes = [_agent_with_task(k, k) for k in texts]
    extractor = SymbolicExtractor(extractors=[ElementExtractor(PerNodeLLM(), TaskExtractionPrompt())])
    result = extractor.process(SessionData(session_id="s1", agent_spans=[n.node for n in nodes]))

    tasks = {n.node_id: n.symbolic_state_elements["tasks"][0] for n in result.nodes if n.node_type == "AgentCall"}
    assert tasks["a"]["same_as"] == []
    assert tasks["b"]["same_as"] == [tasks["a"]["id"]]  # B = A: B points to the earlier task
    assert tasks["c"]["same_as"] == []

    store = extractor.store
    assert store.related("same_as", tasks["b"]["id"]) == [tasks["a"]["id"]]
    assert store.related("same_as", tasks["a"]["id"]) == [tasks["b"]["id"]]  # both directions
    assert store.related("same_as", tasks["c"]["id"]) == []
