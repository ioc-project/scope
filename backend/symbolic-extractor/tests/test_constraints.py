"""Constraints: one class, two origins. ``user`` (from the request of the session, linked to the goal) and
``system`` (from the system prompt of each agent, once per agent, linked to the tasks of the agent)."""

import json

import pytest
from pydantic import ValidationError

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import (
    AgentSystemPromptExtractor,
    Constraint,
    ElementExtractor,
    GoalExtractionPrompt,
    SystemConstraintExtractor,
    TaskExtractionPrompt,
    UserConstraintPrompt,
    agent_constraints,
)
from symbolic_extractor.extractors.constraints import (
    SYSTEM_SYSTEM_PROMPT,
    USER_SYSTEM_PROMPT,
    render_system_constraint_prompt,
)
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import (
    AgentSystemPrompt,
    ExtractionRequest,
    KGNode,
    SessionData,
    SessionTrajectory,
)
from symbolic_extractor.store import BOUNDED_BY, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

ROLE = "You are an itinerary specialist. Find routes and report their travel times. Never invent a route."
REQUEST = "Plan a weekend in Lisbon for 2, max 800 EUR, no flights."
HUMAN_INPUT = json.dumps({"args": [{"messages": [{"type": "HumanMessage", "content": REQUEST}]}], "kwargs": {}})
FINAL_RESPONSE = json.dumps({"next": "FINISH", "final_answer": "Here is your Lisbon itinerary."})

USER_REPLY = [
    {"constraint_text": "The total cost must stay under EUR 800.", "constraint_strength": "hard"},
    {"constraint_text": "The trip should preferably be near the river.", "constraint_strength": "soft"},
]
SYSTEM_REPLY = [{"constraint_text": "The agent must never invent a route.", "constraint_strength": "hard"}]
GOAL_REPLY = {"goals": [{"goal_text": "A weekend plan.", "success_criteria": ["Costs at most 800 EUR"], "status": "set"}]}
TASK_REPLY = {"tasks": [{"task_text": "Find the routes and report the travel times.", "status": "complete"}]}


class FakeLLM:
    """Answers by the system prompt it is given, and records every request."""

    def __init__(self, user=None, system=None):
        self.user = USER_REPLY if user is None else user
        self.system = SYSTEM_REPLY if system is None else system
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if system == USER_SYSTEM_PROMPT:
            data = {"constraints": self.user}
        elif system == SYSTEM_SYSTEM_PROMPT:
            data = {"constraints": self.system}
        elif "GOAL" in system:
            data = GOAL_REPLY
        else:
            data = TASK_REPLY
        return LLMReply(data=data, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))

    def requests(self, system):
        return [u for s, u in self.calls if s == system]


CONVERSATION = KGNode(
    node_type="Conversation", node={"id": "conversation:s1", "query": HUMAN_INPUT, "response": FINAL_RESPONSE}
)


def otel(system=ROLE):
    parts = json.dumps([{"type": "text", "content": system}])
    return f"{parts}\n\n{json.dumps([{'role': 'user', 'parts': [{'type': 'text', 'content': 'q'}]}])}"


def llm_node(call_id):
    return KGNode(node_type="LLMCall", node={"id": call_id, "prompt": otel(ROLE)})


def store_with_prompt(call_id="l1", agent="itinerary", truncated=False):
    store = SymbolicSessionStore("s1")
    prompt = AgentSystemPrompt(id="p1", agent_id=agent, system_prompt=ROLE, truncated=truncated, read_from=call_id)
    store.add("system_prompts", prompt, call_id)
    return store, prompt


# --- the model ----------------------------------------------------------------------------


def test_a_system_constraint_needs_its_agent_and_the_strength_defaults_to_hard():
    with pytest.raises(ValidationError):
        Constraint(id="c", constraint_text="x", origin="system", read_from="l1")
    c = Constraint(id="c", constraint_text="x", origin="user", read_from="conversation:s1")
    assert c.constraint_strength == "hard" and c.agent_id is None
    with pytest.raises(ValidationError):
        Constraint(id="c", constraint_text="x", constraint_strength="maybe", origin="user", read_from="n")


# --- the constraints of the user ----------------------------------------------------------


def test_the_user_constraints_are_read_from_the_request_with_their_own_request():
    llm = FakeLLM()
    extractor = ElementExtractor(llm, UserConstraintPrompt())

    hard, soft = extractor.extract(CONVERSATION)

    assert (hard.origin, hard.read_from, hard.agent_id) == ("user", "conversation:s1", None)
    assert (hard.constraint_strength, soft.constraint_strength) == ("hard", "soft")
    (request,) = llm.requests(USER_SYSTEM_PROMPT)
    assert REQUEST in request
    assert "Lisbon itinerary" not in request  # the response is the goal's business: not sent
    assert extractor.last_usage.calls == 1


def test_the_goal_and_the_user_constraints_are_separate_requests_on_the_same_node():
    llm = FakeLLM()
    ElementExtractor(llm, GoalExtractionPrompt()).extract(CONVERSATION)
    ElementExtractor(llm, UserConstraintPrompt()).extract(CONVERSATION)
    assert [s == USER_SYSTEM_PROMPT for s, _ in llm.calls] == [False, True]


def test_no_request_without_a_request_of_the_user_and_only_the_conversation_applies():
    llm = FakeLLM()
    extractor = ElementExtractor(llm, UserConstraintPrompt())
    assert extractor.extract(KGNode(node_type="Conversation", node={"id": "c", "response": "x"})) == []
    assert llm.calls == []
    assert extractor.applies_to(CONVERSATION)
    assert not extractor.applies_to(llm_node("l1"))


def test_the_user_constraint_ids_are_deterministic_and_depend_on_the_text():
    a = ElementExtractor(FakeLLM(), UserConstraintPrompt()).extract(CONVERSATION)[0]
    b = ElementExtractor(FakeLLM(), UserConstraintPrompt()).extract(CONVERSATION)[0]
    c = ElementExtractor(FakeLLM(user=[{"constraint_text": "Another."}]), UserConstraintPrompt()).extract(CONVERSATION)[0]
    assert a.id == b.id != c.id


# --- the constraints of an agent ----------------------------------------------------------


def test_the_system_prompt_just_read_gives_the_constraints_of_its_agent():
    llm, (store, prompt) = FakeLLM(), store_with_prompt()
    extractor = SystemConstraintExtractor(llm)

    (constraint,) = extractor.extract(llm_node("l1"), store)

    assert isinstance(constraint, Constraint)
    assert constraint.constraint_text == "The agent must never invent a route."
    assert (constraint.origin, constraint.agent_id, constraint.system_prompt_id) == ("system", "itinerary", "p1")
    assert (constraint.read_from, constraint.truncated) == ("l1", False)
    (request,) = llm.requests(SYSTEM_SYSTEM_PROMPT)
    assert ROLE in request and "Agent: itinerary" in request and "cut off" not in request
    assert extractor.last_usage.calls == 1 and extractor.usage.calls == 1


def test_no_request_on_a_call_that_did_not_read_the_prompt():
    llm, (store, _) = FakeLLM(), store_with_prompt("l1")
    extractor = SystemConstraintExtractor(llm)
    assert extractor.extract(llm_node("l2"), store) == []  # a later call of the same agent
    assert extractor.extract(llm_node("l1"), None) == []
    assert extractor.extract(llm_node("l1"), SymbolicSessionStore("s1")) == []  # nothing read (webex)
    assert llm.calls == [] and extractor.last_usage.calls == 0


def test_a_truncated_prompt_marks_its_constraints_and_the_request():
    llm, (store, _) = FakeLLM(), store_with_prompt(truncated=True)
    (constraint,) = SystemConstraintExtractor(llm).extract(llm_node("l1"), store)
    assert constraint.truncated is True
    assert "System prompt looks cut off: yes" in llm.calls[0][1]


def test_invalid_items_are_dropped_and_a_prompt_without_rule_gives_none():
    store, _ = store_with_prompt()
    mixed = FakeLLM(system=[{"constraint_text": ""}, {"constraint_text": "A rule."}, {"constraint_strength": "soft"}])
    assert [c.constraint_text for c in SystemConstraintExtractor(mixed).extract(llm_node("l1"), store)] == ["A rule."]
    assert SystemConstraintExtractor(FakeLLM(system=[])).extract(llm_node("l1"), store) == []
    broken = FakeLLM()
    broken.complete_json = lambda s, u: LLMReply(data={"constraints": "no"}, usage=LLMUsage())
    assert SystemConstraintExtractor(broken).extract(llm_node("l1"), store) == []


def test_the_system_constraint_ids_depend_on_the_prompt_and_the_text():
    store, _ = store_with_prompt()
    a = SystemConstraintExtractor(FakeLLM()).extract(llm_node("l1"), store)[0]
    b = SystemConstraintExtractor(FakeLLM()).extract(llm_node("l1"), store)[0]
    assert a.id == b.id  # same prompt, same text
    other = SymbolicSessionStore("s1")  # same text read from the prompt of another agent
    other.add("system_prompts", AgentSystemPrompt(id="p2", agent_id="moderator", system_prompt=ROLE, read_from="l1"), "l1")
    c = SystemConstraintExtractor(FakeLLM()).extract(llm_node("l1"), other)[0]
    assert c.agent_id == "moderator" and c.id != a.id
    d = SystemConstraintExtractor(FakeLLM(system=[{"constraint_text": "Another rule."}])).extract(llm_node("l1"), store)[0]
    assert d.id != a.id


def test_agent_constraints_selects_the_system_constraints_of_one_agent():
    store, _ = store_with_prompt()
    (constraint,) = SystemConstraintExtractor(FakeLLM()).extract(llm_node("l1"), store)
    store.add("constraints", constraint, "l1")
    (user,) = ElementExtractor(FakeLLM(user=USER_REPLY[:1]), UserConstraintPrompt()).extract(CONVERSATION)
    store.add("constraints", user, "conversation:s1")
    assert agent_constraints(store, "itinerary") == [constraint] and agent_constraints(store, "moderator") == []


def test_the_request_marks_a_clean_prompt_as_not_cut():
    _, prompt = store_with_prompt()
    assert "cut off" not in render_system_constraint_prompt(prompt)


# --- in the pipeline ----------------------------------------------------------------------

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
    def agent(call_id, start, name):
        return {
            "id": call_id, "startTime": start, "agentName": name,
            "inputContent": f"input:{name}:Find routes between the airport and the hotel and report the travel times.",
            "outputContent": f"output:{name}:Done.",
        }

    return SessionData(
        session_id="s1",
        input_text=HUMAN_INPUT,
        output_text=FINAL_RESPONSE,
        agent_spans=[agent("a1", 0.5, "itinerary"), agent("a2", 1.5, "moderator"), agent("a3", 3.0, "itinerary")],
        llm_spans=[
            {"id": "l1", "startTime": 1.0, "prompt": otel(ROLE), "completion": "a"},
            {"id": "l2", "startTime": 2.0, "prompt": otel("You are a moderator."), "completion": "b"},
            {"id": "l3", "startTime": 3.5, "prompt": otel(ROLE), "completion": "c"},
        ],
    )


def pipeline(llm, with_constraints=True):
    extractors = [
        AgentSystemPromptExtractor(),
        *([SystemConstraintExtractor(llm)] if with_constraints else []),
        ElementExtractor(llm, GoalExtractionPrompt()),
        *([ElementExtractor(llm, UserConstraintPrompt())] if with_constraints else []),
        ElementExtractor(llm, TaskExtractionPrompt()),
    ]
    return SymbolicExtractor(extractors=extractors)


def by_id(result):
    return {n.node_id: n for n in result.nodes}


def test_user_constraints_bound_the_goal_and_system_constraints_bound_the_tasks_of_the_agent():
    llm = FakeLLM()
    p = pipeline(llm)

    result = p.process(session(), trajectory=trajectory())

    nodes = by_id(result)
    user = [c for c in nodes["conversation:s1"].symbolic_state_elements["constraints"]]
    assert [c["origin"] for c in user] == ["user", "user"]
    (goal,) = nodes["conversation:s1"].symbolic_state_elements["goals"]
    assert goal["bounded_by"] == [c["id"] for c in user]

    itinerary = nodes["l1"].symbolic_state_elements["constraints"]
    moderator = nodes["l2"].symbolic_state_elements["constraints"]
    assert nodes["l3"].symbolic_state_elements["constraints"] == []  # same agent again: not asked again
    assert [c["agent_id"] for c in itinerary + moderator] == ["itinerary", "moderator"]
    task = lambda call: nodes[call].symbolic_state_elements["tasks"][0]["bounded_by"]  # noqa: E731
    # the first call of the agent ran before its prompt was read, and is linked all the same
    assert task("a1") == task("a3") == [itinerary[0]["id"]]
    assert task("a2") == [moderator[0]["id"]]
    assert p.store.related(BOUNDED_BY, goal["id"]) == goal["bounded_by"]


def test_system_constraints_also_bound_the_system_tasks_of_the_agent():
    from symbolic_extractor.extractors import SystemTaskExtractor

    llm = FakeLLM()
    extractors = [
        AgentSystemPromptExtractor(),
        SystemTaskExtractor(llm),
        SystemConstraintExtractor(llm),
        ElementExtractor(llm, GoalExtractionPrompt()),
        ElementExtractor(llm, UserConstraintPrompt()),
        ElementExtractor(llm, TaskExtractionPrompt()),
    ]
    p = SymbolicExtractor(extractors=extractors)
    nodes = by_id(p.process(session(), trajectory=trajectory()))
    for call, agent in (("l1", "itinerary"), ("l2", "moderator")):
        (constraint,) = nodes[call].symbolic_state_elements["constraints"]
        (system_task,) = nodes[call].symbolic_state_elements["system_tasks"]
        assert constraint["agent_id"] == system_task["agent_id"] == agent
        assert system_task["bounded_by"] == [constraint["id"]]
        assert p.store.related(BOUNDED_BY, system_task["id"]) == [constraint["id"]]


def test_one_request_per_origin_and_agent_and_the_usage_is_counted_under_constraints():
    llm = FakeLLM()
    result = pipeline(llm).process(session(), trajectory=trajectory())
    assert len(llm.requests(USER_SYSTEM_PROMPT)) == 1
    assert len(llm.requests(SYSTEM_SYSTEM_PROMPT)) == 2  # itinerary (read on l1, again on l3: no request) and moderator
    assert result.llm_usage_by_element["constraints"].calls == 3


def test_the_user_constraints_are_still_linked_without_a_trajectory_but_the_tasks_have_no_key():
    result = pipeline(FakeLLM()).process(session())
    nodes = by_id(result)
    (goal,) = nodes["conversation:s1"].symbolic_state_elements["goals"]
    assert len(goal["bounded_by"]) == 2
    assert all("bounded_by" not in t for n in result.nodes for t in n.symbolic_state_elements.get("tasks", []))


def test_without_a_constraint_extractor_there_is_no_key():
    result = pipeline(FakeLLM(), with_constraints=False).process(session(), trajectory=trajectory())
    assert all("bounded_by" not in e for n in result.nodes for k in ("goals", "tasks") for e in n.symbolic_state_elements.get(k, []))


def test_no_constraint_gives_empty_lists():
    result = pipeline(FakeLLM(user=[], system=[])).process(session(), trajectory=trajectory())
    nodes = by_id(result)
    assert nodes["conversation:s1"].symbolic_state_elements["goals"][0]["bounded_by"] == []
    assert nodes["a1"].symbolic_state_elements["tasks"][0]["bounded_by"] == []


def test_live_process_gives_the_same_constraints():
    live = pipeline(FakeLLM())
    nodes = [(0.0, CONVERSATION)]
    for kind, spans in (("AgentCall", session().agent_spans), ("LLMCall", session().llm_spans)):
        nodes += [(span["startTime"], KGNode(node_type=kind, node=span)) for span in spans]
    for _, node in sorted(nodes, key=lambda item: item[0]):
        live.live_process(node, ExtractionRequest(session_id="s1"), trajectory())

    whole = pipeline(FakeLLM())
    whole.process(session(), trajectory=trajectory())
    ids = lambda p: sorted(s.id for s in p.store.by_kind("constraints"))  # noqa: E731
    assert ids(live) == ids(whole) and len(ids(whole)) == 4
    # the relation is complete in the store, even for the agent call that came before the prompt was read
    bounded = lambda p: sorted(t.id for t in p.store.by_kind("tasks") if p.store.related(BOUNDED_BY, t.id))  # noqa: E731
    assert len(bounded(live)) == len(bounded(whole)) == 3
