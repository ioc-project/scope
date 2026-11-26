"""``CapabilityTaskRelationExtractor``: which capabilities of an agent support a task (``supports``)."""

import json

import pytest

from symbolic_extractor import SymbolicExtractor
from symbolic_extractor.extractors import AgentSystemPromptExtractor, SystemCapabilityExtractor
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import (
    AgentCapability,
    ExtractionRequest,
    KGNode,
    SessionData,
    SessionTrajectory,
    SystemTask,
    Task,
)
from symbolic_extractor.relations import SUPPORTS, CapabilityTaskRelationExtractor
from symbolic_extractor.relations.task_capability_relations import SYSTEM_PROMPT, parse_evaluation, render_prompt
from symbolic_extractor.store import HAS_CAPABILITY, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

ROLE = "You are an itinerary specialist. Find routes and report their travel times."


class SupportsLLM:
    """Answers each request with the next of ``replies`` (the last one repeats)."""

    def __init__(self, *replies):
        self.replies = list(replies) or [{"supports": ["C1"], "reasoning": "it is a route."}]
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        data = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(data, Exception):
            raise data
        return LLMReply(data=data, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))


def capability(cid, text, agent="itinerary"):
    return AgentCapability(id=cid, capability_text=text, agent_id=agent, system_prompt_id="p1", read_from="l1")


def task(tid, call="a1", text="Find a route from A to B."):
    return Task(id=tid, task_text=text, acceptance_criteria=["Gives the travel time."], owned_by=call)


def system_task(tid, agent="itinerary", text="Report the travel time of each route."):
    return SystemTask(id=tid, task_text=text, agent_id=agent, system_prompt_id="p1", read_from="l1")


def store_with(*capabilities, tasks=(), system_tasks=(), call="a1"):
    store = SymbolicSessionStore("s1")
    for c in capabilities:
        store.add("capabilities", c, "l1")
        if c.agent_id == "itinerary":
            store.link(HAS_CAPABILITY, call, c.id)
    for t in tasks:
        store.add("tasks", t, t.owned_by)
    for t in system_tasks:
        store.add("system_tasks", t, "l1")
    return store


ROUTES = capability("c1", "Find the ways to reach a place.")
HOTELS = capability("c2", "Find hotels.")


# --- the extractor ------------------------------------------------------------------------


def test_a_task_is_judged_against_the_capabilities_of_its_agent_and_the_relation_recorded():
    llm = SupportsLLM({"supports": ["C2", "c1", "C9", "C2"], "reasoning": " both. "})
    store = store_with(ROUTES, HOTELS, tasks=[task("t1")])
    extractor = CapabilityTaskRelationExtractor(llm)

    (evaluation,) = extractor.extract(store, [store.get("t1").element])

    assert (evaluation.task_id, evaluation.capability_ids) == ("t1", ["c1", "c2"])
    assert evaluation.supports == ["c2", "c1"]  # C9 is not a capability, the repeated C2 counts once
    assert evaluation.supported and evaluation.reasoning == "both."
    assert store.related_from(SUPPORTS, "t1") == ["c2", "c1"]
    assert store.related(SUPPORTS, "c1") == ["t1"]
    system, user = llm.calls[0]
    assert system == SYSTEM_PROMPT and len(llm.calls) == 1
    assert "Find a route from A to B." in user and "- Gives the travel time." in user
    assert "C1. Find the ways to reach a place." in user and "C2. Find hotels." in user
    assert extractor.last_usage.calls == 1


def test_no_capability_supporting_the_task_gives_no_link():
    store = store_with(ROUTES, tasks=[task("t1", text="Book a hotel.")])
    (evaluation,) = CapabilityTaskRelationExtractor(SupportsLLM({"supports": [], "reasoning": "no"})).extract(
        store, [store.get("t1").element]
    )
    assert evaluation.supports == [] and not evaluation.supported
    assert store.related_from(SUPPORTS, "t1") == []


def test_a_task_whose_agent_has_no_capability_is_not_submitted_and_has_no_link():
    llm = SupportsLLM()
    store = store_with(tasks=[task("t1")])
    extractor = CapabilityTaskRelationExtractor(llm)
    assert extractor.extract(store, [store.get("t1").element]) == []
    assert llm.calls == [] and extractor.last_usage.calls == 0
    assert store.related_from(SUPPORTS, "t1") == []  # no capability, so no link


def test_the_capabilities_of_other_agents_are_not_candidates():
    moderator = capability("c3", "Decide who speaks next.", agent="moderator")
    store = store_with(ROUTES, moderator, tasks=[task("t1")])
    assert [c.id for c in CapabilityTaskRelationExtractor.candidates(store, task("t1"))] == ["c1"]


def test_a_system_task_is_judged_against_the_capabilities_with_the_agent_of_the_task():
    moderator = capability("c3", "Decide who speaks next.", agent="moderator")
    store = store_with(ROUTES, HOTELS, moderator, system_tasks=[system_task("s1"), system_task("s2", "moderator")])
    llm = SupportsLLM({"supports": ["C1"], "reasoning": "x"}, {"supports": ["C1"], "reasoning": "y"})

    evaluations = CapabilityTaskRelationExtractor(llm).extract(store, [e.element for e in store.by_kind("system_tasks")])

    assert [(e.task_id, e.capability_ids, e.supports) for e in evaluations] == [
        ("s1", ["c1", "c2"], ["c1"]),
        ("s2", ["c3"], ["c3"]),
    ]
    assert store.related_from(SUPPORTS, "s1") == ["c1"] and store.related_from(SUPPORTS, "s2") == ["c3"]


def test_other_elements_and_tasks_missing_from_the_store_are_ignored():
    llm = SupportsLLM()
    store = store_with(ROUTES, tasks=[task("t1")])
    extractor = CapabilityTaskRelationExtractor(llm)
    assert extractor.extract(store, [ROUTES, task("unknown")]) == [] and llm.calls == []


def test_an_unusable_reply_is_asked_again_and_the_usage_of_both_requests_counts():
    llm = SupportsLLM({"oops": 1}, {"supports": ["C1"], "reasoning": "ok"})
    store = store_with(ROUTES, tasks=[task("t1")])
    extractor = CapabilityTaskRelationExtractor(llm)

    (evaluation,) = extractor.extract(store, [store.get("t1").element])

    assert evaluation.supports == ["c1"] and len(llm.calls) == 2
    assert "Your previous reply was not valid" in llm.calls[1][1] and "Your previous reply" not in llm.calls[0][1]
    assert extractor.last_usage.calls == 2


def test_a_reply_that_is_not_json_is_asked_again_and_two_failures_raise():
    store = store_with(ROUTES, tasks=[task("t1")])
    ok = SupportsLLM(ValueError("not json"), {"supports": [], "reasoning": ""})
    (evaluation,) = CapabilityTaskRelationExtractor(ok).extract(store, [store.get("t1").element])
    assert evaluation.supports == [] and len(ok.calls) == 2

    bad = SupportsLLM({"nope": 1})
    with pytest.raises(ValueError):
        CapabilityTaskRelationExtractor(bad).extract(store, [store.get("t1").element])
    assert len(bad.calls) == 2


def test_parse_accepts_bare_numbers_and_needs_a_list():
    caps = [ROUTES, HOTELS]
    assert [c.id for c in parse_evaluation({"supports": [2, "1"]}, caps)[0]] == ["c2", "c1"]
    with pytest.raises(ValueError):
        parse_evaluation({"supports": "C1"}, caps)


def test_the_request_lists_the_task_even_without_criteria():
    text = render_prompt(system_task("s1"), [ROUTES])
    assert "(none stated)" in text and "Report the travel time of each route." in text


# --- in the pipeline ----------------------------------------------------------------------


class StubTasks:
    """One task per AgentCall node, as the real extractor would give (no LLM)."""

    element_key = "tasks"
    last_usage = LLMUsage()

    def applies_to(self, node):
        return node.node_type == "AgentCall"

    def extract(self, node, store=None):
        return [Task(id=f"t-{node.node_id}", task_text=f"Find a route ({node.node_id})", owned_by=node.node_id)]


class StubSystemTasks:
    """One standing task per agent, on the call that read its system prompt (no LLM)."""

    element_key = "system_tasks"
    last_usage = LLMUsage()

    def applies_to(self, node):
        return node.node_type == "LLMCall"

    def extract(self, node, store=None):
        return [
            SystemTask(
                id=f"st-{p.element.agent_id}",
                task_text=f"Report the travel times ({p.element.agent_id})",
                agent_id=p.element.agent_id,
                system_prompt_id=p.id,
                read_from=node.node_id,
            )
            for p in store.for_node(node.node_id, "system_prompts")
        ]


class PipelineLLM:
    """Capabilities for the capability requests, ``C1`` for every supports request."""

    def __init__(self):
        self.supports_requests = []

    def complete_json(self, system, user):
        if system == SYSTEM_PROMPT:
            self.supports_requests.append(user)
            data = {"supports": ["C1"], "reasoning": "it is a route."}
        else:
            data = {"capabilities": [{"capability_text": "Find the ways to reach a place."}]}
        return LLMReply(data=data, usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20))


def otel(system):
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


def pipeline(llm, with_relation=True):
    return SymbolicExtractor(
        extractors=[
            AgentSystemPromptExtractor(),
            StubSystemTasks(),
            SystemCapabilityExtractor(llm),
            StubTasks(),
        ],
        relation_extractors=[CapabilityTaskRelationExtractor(llm)] if with_relation else [],
    )


def test_process_judges_each_task_once_and_a_system_task_once_for_all_the_calls_of_its_agent():
    llm = PipelineLLM()
    p = pipeline(llm)

    result = p.process(session(), trajectory=trajectory())

    # 3 tasks (one per agent call) + 2 system tasks (one per agent, although itinerary has two calls)
    assert len(llm.supports_requests) == 5 == len(p.capability_evaluations)
    for text in ("(a1)", "(a2)", "(a3)", "(itinerary)", "(moderator)"):
        assert sum(text in r for r in llm.supports_requests) == 1
    by_id = {n.node_id: n for n in result.nodes}
    (itinerary_cap,) = by_id["l1"].symbolic_state_elements["capabilities"]
    (moderator_cap,) = by_id["l2"].symbolic_state_elements["capabilities"]
    assert by_id["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [itinerary_cap["id"]]
    assert by_id["a3"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [itinerary_cap["id"]]
    assert by_id["a2"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [moderator_cap["id"]]
    assert by_id["l1"].symbolic_state_elements["system_tasks"][0]["supported_by_capabilities"] == [itinerary_cap["id"]]
    assert p.store.related(SUPPORTS, itinerary_cap["id"]) == ["st-itinerary", "t-a1", "t-a3"]
    assert result.llm_usage_by_element["relations"].calls == 5
    assert result.llm_usage.calls == 2 + 5  # the two capability requests and the five supports requests


def test_nothing_is_judged_without_the_extractor_or_without_capabilities():
    llm = PipelineLLM()
    result = pipeline(llm, with_relation=False).process(session(), trajectory=trajectory())
    assert llm.supports_requests == []
    assert all("supported_by_capabilities" not in t for n in result.nodes for t in n.symbolic_state_elements.get("tasks", []))

    llm = PipelineLLM()
    p = SymbolicExtractor(
        extractors=[StubTasks()], relation_extractors=[CapabilityTaskRelationExtractor(llm)]
    )
    p.process(session(), trajectory=trajectory())
    assert llm.supports_requests == [] and p.capability_evaluations == []


def test_without_a_trajectory_nothing_is_judged():
    llm = PipelineLLM()
    p = pipeline(llm)
    p.process(session())  # no trajectory: the agent of a call is unknown, so no capability is read from the prompts
    assert llm.supports_requests == []


def test_a_failing_request_is_skipped_and_not_asked_again():
    class Failing(PipelineLLM):
        def complete_json(self, system, user):
            if system == SYSTEM_PROMPT and "(a1)" in user:
                raise RuntimeError("api down")
            return super().complete_json(system, user)

    llm = Failing()
    p = pipeline(llm)
    result = p.process(session(), trajectory=trajectory())

    assert len(llm.supports_requests) == 4  # a1 failed before the request was recorded
    assert "t-a1" not in {e.task_id for e in p.capability_evaluations}
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == []  # no link, like any task without one
    assert by_id["a3"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"]
    p._evaluate_capabilities(result, p.store)  # a second pass does not ask again
    assert len(llm.supports_requests) == 4


def test_live_process_judges_the_task_of_a_call_once_the_capabilities_of_its_agent_arrive():
    llm = PipelineLLM()
    live = pipeline(llm)
    nodes = {}
    for kind, spans in (("AgentCall", session().agent_spans), ("LLMCall", session().llm_spans)):
        for span in spans:
            nodes[span["id"]] = (span["startTime"], KGNode(node_type=kind, node=span))
    returned, requests_after = {}, {}
    for call_id, (_, node) in sorted(nodes.items(), key=lambda kv: kv[1][0]):
        returned[call_id] = live.live_process(node, ExtractionRequest(session_id="s1"), trajectory()).nodes[0]
        requests_after[call_id] = len(llm.supports_requests)

    assert requests_after["a1"] == 0  # its agent has no capability yet
    assert requests_after["l1"] == 2  # the capabilities arrive: the system task and the task of a1 are judged
    assert requests_after["a2"] == 2 and requests_after["l2"] == 4
    assert requests_after["a3"] == 5 and requests_after["l3"] == 5  # a3 comes after: judged when its node arrives
    # a1 was returned before its capabilities were read: [] when returned, but the link is in the store
    # and live_process put it on the node at the step that recorded it (l1)
    assert returned["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [
        live.store.related_from(SUPPORTS, "t-a1")[0]
    ]
    assert returned["a3"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"]
    assert returned["l1"].symbolic_state_elements["system_tasks"][0]["supported_by_capabilities"]
    assert live.store.related_from(SUPPORTS, "t-a1")

    whole = pipeline(PipelineLLM())
    whole.process(session(), trajectory=trajectory())
    relations = lambda p: sorted((e.task_id, tuple(e.supports)) for e in p.capability_evaluations)  # noqa: E731
    assert relations(live) == relations(whole)


def replay(live):
    """Feed the nodes of ``session()`` to ``live`` in start time order; returns the results in that order."""
    nodes = [KGNode(node_type="AgentCall", node=s) for s in session().agent_spans]
    nodes += [KGNode(node_type="LLMCall", node=s) for s in session().llm_spans]
    nodes.sort(key=lambda n: n.node["startTime"])
    return [live.live_process(n, ExtractionRequest(session_id="s1"), trajectory()) for n in nodes]


def test_process_returns_every_edge_of_the_store():
    p = pipeline(PipelineLLM())
    result = p.process(session(), trajectory=trajectory())

    assert result.edges == p.store.edges()
    supports = [e for e in result.edges if e.edge_type == SUPPORTS]
    assert len(supports) == 5  # one capability per task or system task, five of them
    assert {(e.source_id, e.target_id) for e in supports} >= {
        (p.store.related_from(SUPPORTS, "t-a1")[0], "t-a1"),
        (p.store.related_from(SUPPORTS, "st-moderator")[0], "st-moderator"),
    }
    assert all(e.attributes == {} for e in supports)


def test_live_process_reports_the_late_link_as_an_edge_of_the_step_that_made_it():
    live = pipeline(PipelineLLM())
    results = replay(live)  # a1, l1, a2, l2, a3, l3
    supports_in = [[(e.source_id, e.target_id) for e in r.edges if e.edge_type == SUPPORTS] for r in results]

    assert supports_in[0] == []  # a1: its agent has no capability yet
    # l1 reads the capabilities: the system task, and the task of a1 returned one step before
    assert sorted(t for _, t in supports_in[1]) == ["st-itinerary", "t-a1"]
    assert [t for _, t in supports_in[2]] == []  # a2: moderator has none yet
    assert sorted(t for _, t in supports_in[3]) == ["st-moderator", "t-a2"]
    assert [t for _, t in supports_in[4]] == ["t-a3"]
    assert supports_in[5] == []
    # the edges of the steps, put together, are the edges of the store, each once
    assert [e for r in results for e in r.edges] == live.store.edges()


def test_annotate_brings_the_nodes_returned_earlier_up_to_date_with_the_final_store():
    live = pipeline(PipelineLLM())
    results = replay(live)
    nodes = [r.nodes[0] for r in results]
    by_id = {n.node_id: n for n in nodes}
    cap = live.store.by_kind("capabilities")[0].id  # itinerary's: first extracted
    # live_process already refreshed the nodes at each step
    assert by_id["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [cap]
    assert by_id["a1"].capabilities == [cap]

    # a stale copy of a node is brought up to date by annotate
    by_id["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] = []
    by_id["a1"].capabilities = []
    live.annotate(nodes)
    assert by_id["a1"].symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [cap]
    assert by_id["a1"].capabilities == [cap]
    before = [n.model_dump(mode="json") for n in nodes]
    live.annotate(nodes)  # a plain read of the store: nothing changes the second time
    assert [n.model_dump(mode="json") for n in nodes] == before

    whole = pipeline(PipelineLLM())
    expected = whole.process(session(), trajectory=trajectory())
    keys = ("system_tasks", "capabilities")
    for node, other in zip(sorted(nodes, key=lambda n: n.node_id), sorted(expected.nodes, key=lambda n: n.node_id)):
        assert [getattr(node, k) for k in keys] == [getattr(other, k) for k in keys]
        for kind in ("tasks", "system_tasks"):
            assert [t["supported_by_capabilities"] for t in node.symbolic_state_elements.get(kind, [])] == [
                t["supported_by_capabilities"] for t in other.symbolic_state_elements.get(kind, [])
            ]


def test_live_process_updates_the_earlier_node_at_the_step_that_records_the_late_link():
    live = pipeline(PipelineLLM())
    results = replay(live)  # a1, l1, a2, l2, a3, l3
    updated = [sorted(n.node_id for n in r.updated_nodes) for r in results]

    assert updated[0] == []  # a1: nothing before it
    # l1 reads the capabilities of itinerary: the AgentCall a1 gets them and its task is now supported
    assert updated[1] == ["a1"]
    assert updated[2] == []  # a2: the moderator has no capability yet
    assert updated[3] == ["a2"]  # l2 reads those of the moderator
    assert updated[4] == []  # a3: itinerary is known, a3 comes with its capabilities
    assert updated[5] == []
    a1 = results[1].updated_nodes[0]
    assert a1 is results[0].nodes[0]  # the node already returned, modified in place
    cap = live.store.by_kind("capabilities")[0].id
    assert a1.capabilities == [cap]
    assert a1.symbolic_state_elements["tasks"][0]["supported_by_capabilities"] == [cap]


def test_the_nodes_of_a_live_session_equal_the_ones_of_process_without_any_final_pass():
    live = pipeline(PipelineLLM())
    live_nodes = [r.nodes[0] for r in replay(live)]
    expected = pipeline(PipelineLLM()).process(session(), trajectory=trajectory())

    def annotations(nodes):
        return {
            n.node_id: (n.system_tasks, n.capabilities, n.symbolic_state_elements.get("tasks"), n.symbolic_state_elements.get("system_tasks"))
            for n in nodes
        }

    got, want = annotations(live_nodes), annotations(expected.nodes)
    for node_id, (system_tasks, capabilities, tasks, system_task_elements) in got.items():
        assert (system_tasks, capabilities) == want[node_id][:2]
        for mine, theirs in ((tasks, want[node_id][2]), (system_task_elements, want[node_id][3])):
            assert [t.get("supported_by_capabilities") for t in mine or []] == [
                t.get("supported_by_capabilities") for t in theirs or []
            ]
