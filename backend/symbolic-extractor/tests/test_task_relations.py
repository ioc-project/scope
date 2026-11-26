"""``TaskRelationExtractor``: a task implements a task of an earlier agent call, or achieves the goal."""

import json

import pytest

from symbolic_extractor import SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.extractors import (
    ElementExtractor,
    GoalExtractionPrompt,
    TaskExtractionPrompt,
)
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import Goal, SessionData, SessionTrajectory, Task
from symbolic_extractor.relations import ACHIEVES, IMPLEMENTS, SAME_AS, TaskRelationExtractor
from symbolic_extractor.relations.task_relations import GOAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from symbolic_extractor.store import PRECEDED_BY
from symbolic_extractor.usage import LLMUsage


class RelationLLM:
    """Answers the replies given, in order, to the requests of the relation extractor."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.systems = []

    def complete_json(self, system, user):
        self.systems.append(system)
        self.prompts.append(user)
        return LLMReply(data=self.replies[len(self.prompts) - 1], usage=LLMUsage(calls=1, prompt_tokens=10))


def _store(*tasks, preceded_by=()):
    """A store with the goal, the tasks (id, text, call) and the ``preceded_by`` of the tasks of
    each (call, previous call) pair: the tasks of the call follow the tasks of the previous one."""
    store = SymbolicSessionStore("s1")
    store.add("goals", Goal(id="g1", goal_text="Fix the outage.", success_criteria=["Service is up"]), "conversation:s1")
    for task_id, text, call in tasks:
        store.add("tasks", Task(id=task_id, task_text=text, owned_by=call), call)
    for call, previous in preceded_by:
        for task in store.for_node(call, "tasks"):
            for earlier in store.for_node(previous, "tasks"):
                store.link(PRECEDED_BY, task.id, earlier.id)
    return store


def _task(store, task_id):
    return store.get(task_id).element


def test_a_top_level_task_is_compared_with_the_goal():
    store = _store(("t1", "Find the cause.", "a1"))
    candidates = TaskRelationExtractor.candidates(store, _task(store, "t1"))
    assert [c.id for c in candidates] == ["g1"]


def test_a_task_is_compared_with_the_tasks_of_the_preceding_call_only():
    store = _store(
        ("t1", "Find the cause.", "a1"),
        ("t2", "Check the database.", "a2"),
        ("t2b", "Check the cache.", "a2"),
        ("t3", "Check the logs.", "a3"),
        preceded_by=[("a2", "a1"), ("a3", "a2")],
    )
    assert [c.id for c in TaskRelationExtractor.candidates(store, _task(store, "t3"))] == ["t2", "t2b"]  # not t1


def test_a_task_without_preceded_by_is_compared_with_the_goal():
    # no task before it (the root was reached without one), even if the store has other tasks
    store = _store(("t1", "Find the cause.", "a1"), ("t3", "Check the logs.", "a3"))
    assert [c.id for c in TaskRelationExtractor.candidates(store, _task(store, "t3"))] == ["g1"]


def test_an_identical_task_of_the_predecessor_needs_no_request():
    store = _store(("t1", "Check the logs.", "a1"), ("t2", "check the logs", "a2"), preceded_by=[("a2", "a1")])
    store.link(SAME_AS, "t2", "t1")
    store.link(SAME_AS, "t1", "t2")
    assert TaskRelationExtractor.candidates(store, _task(store, "t2")) == []

    llm = RelationLLM()
    extractor = TaskRelationExtractor(llm)
    assert extractor.extract(store, [_task(store, "t2")]) == []
    assert llm.prompts == [] and extractor.last_usage.calls == 0
    assert store.related(IMPLEMENTS, "t2") == []


def test_without_goal_and_predecessor_there_is_nothing_to_ask():
    store = SymbolicSessionStore("s1")
    store.add("tasks", Task(id="t1", task_text="x", owned_by="a1"), "a1")
    llm = RelationLLM()
    assert TaskRelationExtractor(llm).extract(store, [_task(store, "t1")]) == []
    assert llm.prompts == []


def test_the_implemented_tasks_are_recorded_in_the_store():
    store = _store(
        ("t1", "Find the cause.", "a1"),
        ("t2", "Check the database.", "a2"),
        ("t2b", "Write the report.", "a2"),
        ("t3", "Check the logs.", "a3"),
        preceded_by=[("a2", "a1"), ("a3", "a2")],
    )
    # labels are C1, C2 (the two tasks of a2); the reply also names an unknown one, ignored
    llm = RelationLLM({"implements": ["c1", "C9"], "reasoning": "logs are part of the check"})
    extractor = TaskRelationExtractor(llm)
    (relation,) = extractor.extract(store, [_task(store, "t3")])

    assert (relation.task_id, relation.target_id, relation.target) == ("t3", "t2", "task")
    assert relation.reasoning == "logs are part of the check"
    assert store.related(IMPLEMENTS, "t3") == ["t2"]
    assert store.related(ACHIEVES, "t3") == []
    assert relation.relation == "implements"
    assert llm.systems == [SYSTEM_PROMPT]
    prompt = llm.prompts[0]
    assert "Check the logs." in prompt and "C1. Check the database." in prompt and "C2. Write the report." in prompt
    assert "Find the cause." not in prompt  # only the tasks it is preceded_by
    assert extractor.last_usage.calls == 1


def test_a_top_level_task_that_achieves_the_goal():
    store = _store(("t1", "Find the cause.", "a1"))
    llm = RelationLLM({"achieves": [1]})
    (relation,) = TaskRelationExtractor(llm).extract(store, [_task(store, "t1")])
    assert (relation.target_id, relation.target, relation.relation) == ("g1", "goal", "achieves")
    assert "THE GOAL OF THE SESSION" in llm.prompts[0] and "Service is up" in llm.prompts[0]
    assert llm.systems == [GOAL_SYSTEM_PROMPT]
    assert store.related(ACHIEVES, "t1") == ["g1"]
    assert store.related(IMPLEMENTS, "t1") == []  # task -> task only


def test_a_task_that_achieves_nothing_has_no_relation():
    store = _store(("t1", "Find the cause.", "a1"))
    assert TaskRelationExtractor(RelationLLM({"achieves": []})).extract(store, [_task(store, "t1")]) == []
    assert store.related(ACHIEVES, "t1") == [] and store.related(IMPLEMENTS, "t1") == []


def test_a_reply_without_the_list_is_asked_again_once():
    store = _store(("t1", "Find the cause.", "a1"))
    llm = RelationLLM({"answer": "yes"}, {"achieves": ["C1"]})
    extractor = TaskRelationExtractor(llm)
    assert len(extractor.extract(store, [_task(store, "t1")])) == 1
    assert len(llm.prompts) == 2 and "not valid" in llm.prompts[1] and '"achieves"' in llm.prompts[1] and extractor.last_usage.calls == 2

    with pytest.raises(ValueError):
        TaskRelationExtractor(RelationLLM({"x": 1}, {"x": 2})).extract(store, [_task(store, "t1")])


# --- through the pipeline -------------------------------------------------------------------

TASKS = {
    "a1": "Resolve the outage.",
    "a2": "Check the logs.",
    "a3": "check the logs",
    "r": "route",
    "r2": "route",
    "m1": "route",
    "m2": "route",
    "m3": "route",
    "w1": "Find the cause.",
    "w2": "Fix the config.",
}
CALLS = ("a1", "a2", "a3")


class ScriptedLLM:
    """The goal, the task of each agent call (none for the calls in ``no_task``), and the relation
    replies (every task implements C1)."""

    def __init__(self, no_task=()):
        self.relation_prompts = []
        self.no_task = set(no_task)

    def complete_json(self, system, user):
        if "extract the GOAL" in system:
            data = {"goals": [{"goal_text": "The outage is resolved.", "success_criteria": ["Service is up"]}]}
        elif "extract TASKS" in system:
            call = next(c for c in TASKS if f"Agent call id: {c}\n" in user)
            data = {"tasks": [] if call in self.no_task else [{"task_text": TASKS[call]}]}
        else:
            self.relation_prompts.append(user)
            data = {"achieves" if system == GOAL_SYSTEM_PROMPT else "implements": ["C1"]}
        return LLMReply(data=data, usage=LLMUsage(calls=1))


def _session(calls=CALLS):
    spans = [
        {"id": call, "agentName": call, "startTime": float(i), "inputContent": f"input:{call}:{TASKS[call]}", "outputContent": "output:x:ok"}
        for i, call in enumerate(calls, start=1)
    ]
    return SessionData(session_id="s1", input_text="Fix it", output_text="Fixed", agent_spans=spans)


def _trajectory(calls=CALLS):
    steps = [
        {"call_id": call, "kind": "agent", "agent_id": call, "parent_id": "", "start_time": float(i)}
        for i, call in enumerate(calls, start=1)
    ]
    return SessionTrajectory(session_id="s1", steps=steps)


def _pipeline(llm):
    return SymbolicExtractor(
        extractors=[ElementExtractor(llm, GoalExtractionPrompt()), ElementExtractor(llm, TaskExtractionPrompt())],
        relation_extractors=[TaskRelationExtractor(llm)],
    )


def _tasks_by_call(result):
    return {
        n.node_id: n.symbolic_state_elements["tasks"][0]
        for n in result.nodes
        if n.node_type == "AgentCall" and n.symbolic_state_elements.get("tasks")
    }


def test_calls_without_task_are_passed_over_up_to_the_first_task_or_the_goal():
    # r (no task), a1 (task), r and r2 (no task), a3 (task): a3 climbs two calls up to a1
    calls = ("r", "a1", "r2", "a2", "a3")
    llm = ScriptedLLM(no_task=("r", "r2", "a2"))
    extractor = _pipeline(llm)
    result = extractor.process(_session(calls), trajectory=_trajectory(calls))
    tasks = _tasks_by_call(result)
    (goal,) = [g for n in result.nodes for g in n.symbolic_state_elements.get("goals", [])]

    assert set(tasks) == {"a1", "a3"}
    assert tasks["a1"]["preceded_by"] == []  # only a task-less call before it: the root
    assert tasks["a1"]["implements"] == [] and tasks["a1"]["achieves"] == [goal["id"]]
    assert tasks["a3"]["preceded_by"] == [tasks["a1"]["id"]]  # a2 and r2 have none: up to a1
    assert tasks["a3"]["implements"] == [tasks["a1"]["id"]] and tasks["a3"]["achieves"] == []
    assert "Resolve the outage." in llm.relation_prompts[1]


def test_a_worker_is_preceded_by_the_worker_that_returned_to_the_task_less_router_before_it():
    # hub and spoke: m1 -route-> w1 -return-> m2 -route-> w2 -return-> m3 (end). Only workers have a task.
    calls = ("m1", "w1", "m2", "w2", "m3")
    goto = {"m1": "w1", "m2": "w2", "m3": "FINISH"}
    steps = [
        {
            "call_id": call,
            "kind": "agent",
            "agent_id": call,
            "parent_id": "",
            "start_time": float(i),
            "output_text": json.dumps({"goto": goto[call]}) if call in goto else "done",
        }
        for i, call in enumerate(calls, start=1)
    ]
    llm = ScriptedLLM(no_task=("m1", "m2", "m3"))
    extractor = _pipeline(llm)
    result = extractor.process(_session(calls), trajectory=SessionTrajectory(session_id="s1", steps=steps))
    tasks = _tasks_by_call(result)

    assert set(tasks) == {"w1", "w2"}
    assert tasks["w1"]["preceded_by"] == []  # m1 has no task and nothing before it: the goal
    assert tasks["w1"]["achieves"] != [] and tasks["w1"]["implements"] == []
    # w2 <- m2 (no task) <- w1, which handed control back to m2
    assert tasks["w2"]["preceded_by"] == [tasks["w1"]["id"]]
    assert tasks["w2"]["implements"] == [tasks["w1"]["id"]] and tasks["w2"]["achieves"] == []
    assert extractor.store.related(PRECEDED_BY, tasks["w2"]["id"]) == [tasks["w1"]["id"]]


def test_implements_is_added_to_the_tasks_of_the_result_and_the_store():
    llm = ScriptedLLM()
    extractor = _pipeline(llm)
    result = extractor.process(_session(), trajectory=_trajectory())
    tasks = _tasks_by_call(result)
    (goal,) = [g for n in result.nodes for g in n.symbolic_state_elements.get("goals", [])]

    assert tasks["a1"]["achieves"] == [goal["id"]] and tasks["a1"]["implements"] == []  # top level: the goal
    assert tasks["a1"]["preceded_by"] == []
    assert tasks["a2"]["preceded_by"] == [tasks["a1"]["id"]] and tasks["a3"]["preceded_by"] == [tasks["a2"]["id"]]
    assert extractor.store.related(PRECEDED_BY, tasks["a3"]["id"]) == [tasks["a2"]["id"]]  # not a1
    assert tasks["a2"]["implements"] == [tasks["a1"]["id"]]  # its predecessor's task
    # a3 repeats a2 (same_as, no request): nothing more is said
    assert tasks["a3"]["same_as"] == [tasks["a2"]["id"]] and tasks["a3"]["implements"] == []
    assert len(llm.relation_prompts) == 2
    assert extractor.store.related(IMPLEMENTS, tasks["a2"]["id"]) == [tasks["a1"]["id"]]
    assert result.llm_usage_by_element["relations"].calls == 2


def test_without_a_trajectory_the_predecessors_are_unknown_and_no_task_relation_is_made():
    llm = ScriptedLLM()
    extractor = _pipeline(llm)
    result = extractor.process(_session())
    assert llm.relation_prompts == []
    assert all("implements" not in t and "preceded_by" not in t for t in _tasks_by_call(result).values())
    assert extractor.store.by_kind("tasks") and not any(extractor.store.related(PRECEDED_BY, s.id) for s in extractor.store.by_kind("tasks"))


def test_preceded_by_is_recorded_without_any_relation_extractor():
    extractor = SymbolicExtractor(
        extractors=[ElementExtractor(ScriptedLLM(), GoalExtractionPrompt()), ElementExtractor(ScriptedLLM(), TaskExtractionPrompt())]
    )
    tasks = _tasks_by_call(extractor.process(_session(), trajectory=_trajectory()))
    assert tasks["a2"]["preceded_by"] == [tasks["a1"]["id"]]
    assert "implements" not in tasks["a2"]
