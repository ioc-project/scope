"""Functional test: read a set of traces from the KG, run the full extraction on each (the worker's, without
writing), and check that the output of every trace is correct (see ``CHECKS``).

It needs the KG and a real LLM (``.env``) and costs LLM calls, so the test is marked ``live``:

    uv run pytest --live tests/test_functional_traces.py            # the sessions of functional_sessions.txt
    uv run pytest --live tests/test_functional_traces.py --sessions <id>,<id>

The traces are read and extracted exactly as the worker does (embeddings off: no check needs ``same_claims``);
the outputs are saved in a temporary folder. At the end a list of PASS / FAIL per trace and check is printed, headed by the name of the
MAS of the trace.

The checks themselves are plain functions on the output JSON and are tested below without KG or
LLM (they run in every ``pytest``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from symbolic_extractor.extractors.claims import _first_text, is_verbatim
from symbolic_extractor.extractors.facts import tool_output_text
from symbolic_extractor.extractors.tasks import skip_reason
from symbolic_extractor.models import KGNode, _epoch

logger = logging.getLogger(__name__)

SESSIONS_FILE = Path(__file__).with_name("functional_sessions.txt")

Result = dict[str, Any]
#: A check returns the problems it found in the output of one trace, empty when it passes.
Check = Callable[[Result], list[str]]


# --------------------------------------------------------------------------------------------
# Checks on the output JSON of one trace (``ExtractionResult``)
# --------------------------------------------------------------------------------------------


def _elements(result: Result, kind: str) -> list[dict[str, Any]]:
    return [
        element
        for node in result.get("nodes", [])
        for element in node.get("symbolic_state_elements", {}).get(kind, [])
    ]


def _short(text: Any, size: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[: size - 3] + "..."


def check_extracted(result: Result) -> list[str]:
    """The extraction found something of each kind: a goal, tasks and claims."""
    return [f"no {kind} extracted" for kind in ("goals", "tasks", "claims") if not _elements(result, kind)]


def check_tasks_connected(result: Result) -> list[str]:
    """Every task is connected to the rest: it is the same as another task (``same_as``), it follows
    one (``preceded_by``) or it achieves the goal (``achieves``)."""
    return [
        f"task {t['id'][:8]} ({_short(t.get('task_text'))}) has no same_as, preceded_by or achieves"
        for t in _elements(result, "tasks")
        if not (t.get("same_as") or t.get("preceded_by") or t.get("achieves"))
    ]


def check_goal_linked_to_a_task(result: Result) -> list[str]:
    """Every goal is achieved by at least one task (``achieves`` of a task points to it)."""
    goals = _elements(result, "goals")
    if not goals:
        return ["no goal in the session"]
    achieved = {goal_id for t in _elements(result, "tasks") for goal_id in t.get("achieves") or []}
    return [
        f"goal {g['id'][:8]} ({_short(g.get('goal_text'))}) is not achieved by any task"
        for g in goals
        if g["id"] not in achieved
    ]


def check_goal_answered_by_claims(result: Result) -> list[str]:
    """Every goal is answered by at least one claim, a claim of the final answer: the ``answers`` of
    a claim (the ``mas:answers`` relation, Claim -> Goal) points to it. The claims have no such key
    when the KG gave no final agent call for the session."""
    goals = _elements(result, "goals")
    if not goals:
        return ["no goal in the session"]
    answered = {goal_id for c in _elements(result, "claims") for goal_id in c.get("answers") or []}
    return [
        f"goal {g['id'][:8]} ({_short(g.get('goal_text'))}) is answered by no claim of the final answer"
        for g in goals
        if g["id"] not in answered
    ]


#: relation key -> kind of element it points to, for each kind of element.
_LINKS = {
    "tasks": {
        "preceded_by": "tasks",
        "same_as": "tasks",
        "implements": "tasks",
        "achieves": "goals",
        "bounded_by": "constraints",
    },
    "goals": {"bounded_by": "constraints"},
    "system_tasks": {"bounded_by": "constraints"},
    "claims": {
        "supported_by": "facts",
        "derived_from": "claims",
        "conflicts_with": "claims",
        "same_as": "claims",
        "answers": "goals",
        "fulfils": "goals",
        "produced_by": "tasks",
        "fulfils_task": "tasks",
    },
    # a delegation claim relates to the claims it restates (``ids`` below holds claims and delegation claims)
    "delegation_claims": {"derived_from": "claims", "conflicts_with": "claims", "same_as": "claims"},
}


def check_relations_resolve(result: Result) -> list[str]:
    """Every relation points to an element of the output, and none to the element itself: the ids of
    the task and claim relations, and the ``input_claims`` / ``input_facts`` of the LLM calls."""
    ids = {kind: {e["id"] for e in _elements(result, kind)} for kind in ("goals", "tasks", "claims", "facts", "constraints")}
    ids["claims"] |= {e["id"] for e in _elements(result, "delegation_claims")}
    problems: list[str] = []
    for kind, links in _LINKS.items():
        for element in _elements(result, kind):
            for key, target_kind in links.items():
                for target in element.get(key) or []:
                    if target == element["id"]:
                        problems.append(f"{kind[:-1]} {element['id'][:8]}: {key} points to itself")
                    elif target not in ids[target_kind]:
                        problems.append(
                            f"{kind[:-1]} {element['id'][:8]}: {key} {target[:8]} is not a {target_kind[:-1]} of the output"
                        )
    for node in result.get("nodes", []):
        for key, kind in (("input_claims", "claims"), ("input_facts", "facts"), ("source_claims", "claims")):
            for target in node.get(key) or []:
                if target not in ids[kind]:
                    problems.append(f"call {str(node['node'].get('id'))[:8]}: {key} {target[:8]} is not in the output")
    return problems


def check_task_order_acyclic(result: Result) -> list[str]:
    """The ``preceded_by`` relation between tasks has no cycle (an earlier task cannot follow a later one)."""
    graph = {t["id"]: list(t.get("preceded_by") or []) for t in _elements(result, "tasks")}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    cycles: list[str] = []

    def visit(task_id: str) -> None:
        state[task_id] = 1
        for previous in graph.get(task_id, []):
            if state.get(previous) == 1:
                cycles.append(f"task {task_id[:8]} and {previous[:8]} are in a preceded_by cycle")
            elif previous not in state:
                visit(previous)
        state[task_id] = 2

    for task_id in graph:
        if task_id not in state:
            visit(task_id)
    return cycles


def _nodes_by_id(result: Result) -> dict[str, dict[str, Any]]:
    return {n["node"]["id"]: n for n in result.get("nodes", []) if n.get("node", {}).get("id")}


def check_claims_generated_by_a_node(result: Result) -> list[str]:
    """The ``generated_by`` of every claim is a node of the output."""
    nodes = _nodes_by_id(result)
    problems = []
    for claim in _elements(result, "claims"):
        generated_by = claim.get("generated_by")
        if not generated_by:
            problems.append(f"claim {claim['id'][:8]} has no generated_by")
        elif generated_by not in nodes:
            problems.append(f"claim {claim['id'][:8]}: generated_by {generated_by[:8]} is not a node of the output")
    return problems


def check_claim_quotes_in_source(result: Result) -> list[str]:
    """The ``verbatim_source`` of every claim occurs in the output of the LLM call that generated it.

    Compared as the extractor does (``is_verbatim``): whitespace runs collapsed, in the raw output or
    in the decoded text of its message parts (the raw form has JSON escapes, which would break any
    quote with a quotation mark or a line break). A claim with no ``verbatim_source`` is not checked,
    nor is one whose LLM call is not in the output (``check_claims_generated_by_a_node`` reports it).
    """
    nodes = _nodes_by_id(result)
    problems = []
    for claim in _elements(result, "claims"):
        quote, source = claim.get("verbatim_source"), nodes.get(claim.get("generated_by"))
        if not quote or source is None:
            continue
        if not is_verbatim(quote, _first_text(source["node"], "completion", "outputContent")):
            problems.append(f"claim {claim['id'][:8]}: the quote {_short(quote)!r} is not in the output of its LLM call")
    return problems


#: kind of element -> (key of the node it comes from, relations that must point to an earlier element).
#: The time of an element is the start time of that node: the AgentCall that owns a task, the LLMCall
#: that generated a claim, the ToolCall a fact is grounded in.
_EARLIER_LINKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tasks": ("owned_by", ("preceded_by", "same_as", "implements")),
    "claims": ("generated_by", ("derived_from", "same_as", "supported_by")),
    "facts": ("grounded_in", ()),
}


def check_relations_point_back_in_time(result: Result) -> list[str]:
    """``preceded_by``, ``same_as`` and ``implements`` of a task, and ``derived_from``, ``same_as`` and
    ``supported_by`` of a claim, point to an element that does not start after it (equal times are
    fine: two claims of one call). A relation that cannot be compared because a node is missing or has
    no ``startTime`` is reported once per node. Dangling ids are left to ``check_relations_resolve``.
    """
    nodes = _nodes_by_id(result)
    start: dict[str, tuple[float | None, str]] = {}  # element id -> (start of its node, id of its node)
    for kind, (owner_key, _) in _EARLIER_LINKS.items():
        for element in _elements(result, kind):
            owner = element.get(owner_key)
            node = nodes.get(owner)
            start[element["id"]] = (_epoch(node["node"].get("startTime")) if node else None, str(owner))
    problems: list[str] = []
    undated: set[str] = set()
    for kind, (_, keys) in _EARLIER_LINKS.items():
        for element in _elements(result, kind):
            own_time, own_owner = start[element["id"]]
            for key in keys:
                for target in element.get(key) or []:
                    if target not in start:
                        continue
                    target_time, target_owner = start[target]
                    if own_time is None or target_time is None:
                        undated.update(o for o, t in ((own_owner, own_time), (target_owner, target_time)) if t is None)
                    elif target_time > own_time:
                        problems.append(
                            f"{kind[:-1]} {element['id'][:8]}: {key} {target[:8]} starts {target_time - own_time:.2f}s after it"
                        )
    problems += [f"cannot compare times: node {o[:8]} is not in the output or has no startTime" for o in sorted(undated)]
    return problems


def check_task_relation_rules(result: Result) -> list[str]:
    """The rules the task relations are built with (``relations/task_relations.py``):

    * ``implements`` is a subset of ``preceded_by``;
    * ``achieves`` only on a task with an empty ``preceded_by``;
    * a task does not implement a task it is ``same_as`` (identical tasks are not judged);
    * every task with an empty ``preceded_by`` was judged against the goal: it has an ``achieves``
      list (``[]`` when it does not achieve it), which the extractor only sets once it ran on the task.
    """
    has_goal = bool(_elements(result, "goals"))
    problems = []
    for task in _elements(result, "tasks"):
        label = f"task {task['id'][:8]}"
        previous = task.get("preceded_by")
        if previous is None:
            problems.append(f"{label} has no preceded_by (the order of the agent calls is unknown)")
            continue
        implements, same = task.get("implements") or [], task.get("same_as") or []
        problems += [f"{label} implements {t[:8]} but is not preceded_by it" for t in implements if t not in previous]
        problems += [f"{label} implements {t[:8]}, the task it is also the same as" for t in implements if t in same]
        if task.get("achieves") and previous:
            problems.append(
                f"{label} achieves a goal but is preceded_by {len(previous)} task(s): only a task with no predecessor is compared with the goal"
            )
        if not previous:
            if not has_goal:
                problems.append(f"{label} has no predecessor but there is no goal to judge it against")
            elif not isinstance(task.get("achieves"), list):
                problems.append(f"{label} has no predecessor and was not judged against the goal (no achieves)")
    return problems


def check_agent_calls_not_lost(result: Result) -> list[str]:
    """Every agent call has a task, or is skipped for a reason ``skip_reason`` can name: an empty input,
    a bare resume marker or a routing command. A call that is neither has silently lost its task."""
    problems = []
    for node in result.get("nodes", []):
        if node.get("node_type") != "AgentCall" or node.get("symbolic_state_elements", {}).get("tasks"):
            continue
        if skip_reason(KGNode(node_type="AgentCall", node=node["node"])) is None:
            raw = node["node"]
            name = raw.get("agentName") or raw.get("name") or "agent call"
            problems.append(f"{name} ({str(raw.get('id'))[:8]}) has no task and no reason to skip it")
    return problems


def _agent_name(node: dict[str, Any]) -> str:
    raw = node.get("node", {})
    return str(raw.get("agentName") or raw.get("name") or "agent call")


def check_agent_calls_have_a_task(result: Result) -> list[str]:
    """Every agent call has at least one task: a task extracted from its input (``tasks``) or a standing
    task of its agent, read from the system prompt (``system_tasks``). Stricter than
    ``check_agent_calls_not_lost``: a call that is skipped for the task extraction (a router turn, a
    resume marker) still has the standing tasks of its agent, so it fails here only when its agent has
    none, which is the case when the system prompt of the agent could not be read."""
    return [
        f"{_agent_name(node)} ({str(node['node'].get('id'))[:8]}) has no task and no system task"
        for node in result.get("nodes", [])
        if node.get("node_type") == "AgentCall"
        and not node.get("symbolic_state_elements", {}).get("tasks")
        and not node.get("system_tasks")
    ]


def check_agents_have_a_system_prompt(result: Result) -> list[str]:
    """Every agent of the session (the ``agentName`` of its agent calls) has a system prompt in the
    output (``system_prompts`` of an LLM call, one per agent). It is read from the input of the LLM
    calls of the agent, so it fails when that input holds none that can be read: for the webex MAS the
    KG records only a reference (``llm-input:<agent>:<uuid>``). A prompt cut by the KG (SRE, 4000
    characters) is read as far as it goes and flagged ``truncated``: that is not a failure."""
    agents = {_agent_name(n) for n in result.get("nodes", []) if n.get("node_type") == "AgentCall"}
    with_prompt = {p.get("agent_id") for p in _elements(result, "system_prompts")}
    return [f"agent {agent} has no system prompt" for agent in sorted(agents - with_prompt)]


def check_tool_results_give_facts(result: Result) -> list[str]:
    """Every tool call with a result has exactly one fact, grounded in it, and tool calls without a
    result have none. The fact extractor has no LLM, so the count is exact. Facts under a node that
    is not a tool call are reported too. A tool call with no id cannot be keyed and is not checked."""
    problems = []
    for node in result.get("nodes", []):
        facts = node.get("symbolic_state_elements", {}).get("facts", [])
        raw = node.get("node", {})
        label = f"{node.get('node_type', 'node')} {str(raw.get('id'))[:8]}"
        if node.get("node_type") != "ToolCall":
            if facts:
                problems.append(f"{label} has {len(facts)} fact(s) but is not a tool call")
            continue
        if not raw.get("id"):
            continue
        expected = 1 if tool_output_text(raw) else 0
        if len(facts) != expected:
            problems.append(f"{label} has {len(facts)} fact(s), expected {expected}")
        problems += [
            f"{label}: fact {f['id'][:8]} is grounded in {str(f.get('grounded_in'))[:8]}, not in this call"
            for f in facts
            if f.get("grounded_in") != raw["id"]
        ]
    return problems


#: name -> check, in the order they are run and reported.
CHECKS: dict[str, Check] = {
    "extracted": check_extracted,
    "tasks_connected": check_tasks_connected,
    "goal_linked_to_task": check_goal_linked_to_a_task,
    "goal_answered_by_claims": check_goal_answered_by_claims,
    "relations_resolve": check_relations_resolve,
    "task_order_acyclic": check_task_order_acyclic,
    "claim_quotes_in_source": check_claim_quotes_in_source,
    "claims_generated_by_a_node": check_claims_generated_by_a_node,
    "relations_point_back_in_time": check_relations_point_back_in_time,
    "task_relation_rules": check_task_relation_rules,
    "agent_calls_have_task": check_agent_calls_not_lost,
    "agent_calls_have_a_task_or_system_task": check_agent_calls_have_a_task,
    "agents_have_system_prompt": check_agents_have_a_system_prompt,
    "tool_results_give_facts": check_tool_results_give_facts,
}


# --------------------------------------------------------------------------------------------
# Tests of the checks (no KG, no LLM)
# --------------------------------------------------------------------------------------------


def _node(node_type: str, node_id: str, start: float | None, elements: dict | None = None,
          input_claims: list[str] | None = None, **raw: Any) -> dict[str, Any]:
    """A node of the output: the KG node (``id``, ``startTime`` and ``raw``) plus its elements."""
    kg_node = {"id": node_id, **({"startTime": start} if start is not None else {}), **raw}
    return {
        "node_type": node_type,
        "node": kg_node,
        "input_claims": input_claims,
        "input_facts": None,
        "symbolic_state_elements": elements or {},
    }


COMPLETION = "Fare is 75. It is cheap."


def _result(tasks: list[dict], goals: list[dict] | None = None, claims: list[dict] | None = None,
            facts: list[dict] | None = None, input_claims: list[str] | None = None) -> Result:
    """A well-formed output: a Conversation node with the goals; one AgentCall node per task owner
    (start 1, 2, ...) with its tasks and the standing tasks of its agent (``system_tasks``); a ToolCall
    (start 5) with the facts; an LLMCall ``llm1`` (start 10) with the claims and the system prompt of
    every agent."""
    goals = [{"id": "g1", "goal_text": "Know the fastest trip"}] if goals is None else goals
    claims = [
        {"id": "c1", "generated_by": "llm1", "verbatim_source": "Fare is 75.", "supported_by": ["f1"]},
        {"id": "c2", "generated_by": "llm1", "verbatim_source": "It is cheap.", "derived_from": ["c1"], "answers": ["g1"]},
    ] if claims is None else claims
    facts = [{"id": "f1", "grounded_in": "tool1"}] if facts is None else facts
    owners = list(dict.fromkeys(t.get("owned_by") or "a0" for t in tasks))
    nodes = [_node("Conversation", "conv", None, {"goals": goals})]
    for index, owner in enumerate(owners, start=1):
        owned = [t for t in tasks if (t.get("owned_by") or "a0") == owner]
        nodes.append(_node("AgentCall", owner, float(index), {"tasks": owned}, agentName=owner,
                           inputContent="input:a:Do the work", outputContent="output:a:done"))
    for tool_id in dict.fromkeys(f.get("grounded_in", "tool1") for f in facts):
        nodes.append(_node("ToolCall", tool_id, 5.0,
                           {"facts": [f for f in facts if f.get("grounded_in", "tool1") == tool_id]},
                           toolOutput="Fare: $75"))
    prompts = [{"id": f"sp_{owner}", "agent_id": owner} for owner in owners]
    nodes.append(_node("LLMCall", "llm1", 10.0, {"claims": claims, "system_prompts": prompts}, input_claims or [],
                       completion=COMPLETION))
    return {"nodes": nodes}


def _node_of(result: Result, node_id: str) -> dict[str, Any]:
    return next(n for n in result["nodes"] if n["node"].get("id") == node_id)


GOOD_TASKS = [
    {"id": "t1", "owned_by": "a1", "task_text": "Find the route", "achieves": ["g1"], "preceded_by": [], "same_as": [], "implements": []},
    {"id": "t2", "owned_by": "a2", "task_text": "Find the route", "achieves": [], "preceded_by": ["t1"], "same_as": ["t1"], "implements": []},
    {"id": "t3", "owned_by": "a3", "task_text": "Check fares", "achieves": [], "preceded_by": ["t2"], "same_as": [], "implements": ["t2"]},
]


def _failing(result: Result) -> list[str]:
    return [name for name, check in CHECKS.items() if check(result)]


def test_a_complete_output_passes_every_check():
    assert _failing(_result(GOOD_TASKS)) == []


def test_goal_answered_by_no_claim_fails_and_a_dangling_answers_is_reported():
    claims = [{"id": "c1", "generated_by": "llm1", "verbatim_source": "Fare is 75.", "supported_by": ["f1"]}]
    assert _failing(_result(GOOD_TASKS, claims=claims)) == ["goal_answered_by_claims"]
    claims[0]["answers"] = ["nope"]
    assert "not a goal of the output" in check_relations_resolve(_result(GOOD_TASKS, claims=claims))[0]


def test_a_dangling_bounded_by_is_reported():
    goals = [{"id": "g1", "goal_text": "Know the fastest trip", "bounded_by": ["k9"]}]
    problems = check_relations_resolve(_result(GOOD_TASKS, goals=goals))
    assert problems == ["goal g1: bounded_by k9 is not a constraint of the output"]


def test_task_without_same_as_preceded_by_or_achieves_is_not_connected():
    tasks = GOOD_TASKS + [{"id": "t4", "task_text": "Alone", "achieves": [], "preceded_by": [], "same_as": []}]
    assert [p[:7] for p in check_tasks_connected(_result(tasks))] == ["task t4"]
    # only implements does not connect it
    tasks[-1]["implements"] = ["t1"]
    assert check_tasks_connected(_result(tasks))


def test_goal_nobody_achieves_is_not_linked_to_a_task():
    tasks = [dict(GOOD_TASKS[0], achieves=[])] + GOOD_TASKS[1:]
    assert "goal g1" in check_goal_linked_to_a_task(_result(tasks))[0]
    assert check_goal_linked_to_a_task(_result(GOOD_TASKS, goals=[])) == ["no goal in the session"]


def test_empty_extraction_is_reported():
    assert check_extracted(_result([], goals=[], claims=[])) == [
        "no goals extracted",
        "no tasks extracted",
        "no claims extracted",
    ]


def test_dangling_and_self_relations_are_reported():
    tasks = [dict(GOOD_TASKS[0]), dict(GOOD_TASKS[1], preceded_by=["t1", "ghost"]), dict(GOOD_TASKS[2], implements=["t3"])]
    problems = check_relations_resolve(_result(tasks, claims=[{"id": "c1", "supported_by": ["f9"]}], input_claims=["c7"]))
    assert len(problems) == 4
    assert any("ghost" in p for p in problems)
    assert any("points to itself" in p for p in problems)
    assert any("supported_by f9" in p for p in problems)
    assert any("input_claims c7" in p for p in problems)


def test_preceded_by_cycle_is_reported():
    tasks = [dict(GOOD_TASKS[0], preceded_by=["t2"]), GOOD_TASKS[1], GOOD_TASKS[2]]
    assert len(check_task_order_acyclic(_result(tasks))) == 1
    assert check_task_order_acyclic(_result(GOOD_TASKS)) == []


def test_claim_quote_must_be_in_the_output_of_the_generating_call():
    wrong = [{"id": "c1", "generated_by": "llm1", "verbatim_source": "Fare is 99."}]
    (problem,) = check_claim_quotes_in_source(_result(GOOD_TASKS, claims=wrong))
    assert problem.startswith("claim c1: the quote 'Fare is 99.' is not in the output")
    # a quote with a quotation mark and a line break, in the JSON message list the KG stores
    quoted = [{"id": "c1", "generated_by": "llm1", "verbatim_source": 'He said "ok". Next line.'}]
    result = _result(GOOD_TASKS, claims=quoted)
    parts = [{"type": "text", "content": 'He said "ok".\nNext   line.'}]
    _node_of(result, "llm1")["node"]["completion"] = json.dumps([{"role": "assistant", "parts": parts}])
    assert check_claim_quotes_in_source(result) == []
    # no quote (the extractor drops the ones it cannot find), or a call that is not in the output
    unchecked = [{"id": "c1", "generated_by": "llm1"}, {"id": "c2", "generated_by": "ghost", "verbatim_source": "x"}]
    assert check_claim_quotes_in_source(_result(GOOD_TASKS, claims=unchecked)) == []


def test_claim_generated_by_must_be_a_node_of_the_output():
    claims = [{"id": "c1", "generated_by": "ghost"}, {"id": "c2"}, {"id": "c3", "generated_by": "llm1"}]
    assert check_claims_generated_by_a_node(_result(GOOD_TASKS, claims=claims)) == [
        "claim c1: generated_by ghost is not a node of the output",
        "claim c2 has no generated_by",
    ]


def test_relation_to_a_later_element_is_reported():
    assert check_relations_point_back_in_time(_result(GOOD_TASKS)) == []
    # t1 (agent call 1) preceded by t2 (agent call 2)
    tasks = [dict(GOOD_TASKS[0], preceded_by=["t2"]), *GOOD_TASKS[1:]]
    (problem,) = check_relations_point_back_in_time(_result(tasks))
    assert problem.startswith("task t1: preceded_by t2 starts 1.00s after it")
    # a fact of a tool call that started after the LLM call that cites it
    result = _result(GOOD_TASKS)
    _node_of(result, "tool1")["node"]["startTime"] = 20.0
    (problem,) = check_relations_point_back_in_time(result)
    assert problem.startswith("claim c1: supported_by f1 starts 10.00s after it")
    # implements and same_as are checked too
    tasks = [GOOD_TASKS[0], dict(GOOD_TASKS[1], same_as=["t3"]), dict(GOOD_TASKS[2], implements=[])]
    assert len(check_relations_point_back_in_time(_result(tasks))) == 1


def test_relation_with_a_node_that_cannot_be_dated_is_reported_once():
    result = _result(GOOD_TASKS)
    del _node_of(result, "a2")["node"]["startTime"]
    assert check_relations_point_back_in_time(result) == [
        "cannot compare times: node a2 is not in the output or has no startTime"
    ]


def test_task_relation_rules():
    t1, t2, t3 = GOOD_TASKS
    assert check_task_relation_rules(_result(GOOD_TASKS)) == []
    # implements something it does not follow
    (problem,) = check_task_relation_rules(_result([t1, t2, dict(t3, implements=["t1"])]))
    assert problem == "task t3 implements t1 but is not preceded_by it"
    # achieves the goal with a predecessor
    (problem,) = check_task_relation_rules(_result([t1, dict(t2, achieves=["g1"]), t3]))
    assert "achieves a goal but is preceded_by 1 task(s)" in problem
    # implements the task it is the same as
    (problem,) = check_task_relation_rules(_result([t1, dict(t2, implements=["t1"]), t3]))
    assert problem == "task t2 implements t1, the task it is also the same as"
    # top-level task never judged against the goal (no achieves key), or no goal to judge it against
    unjudged = {k: v for k, v in t1.items() if k != "achieves"}
    (problem,) = check_task_relation_rules(_result([unjudged, t2, t3]))
    assert problem == "task t1 has no predecessor and was not judged against the goal (no achieves)"
    (problem,) = check_task_relation_rules(_result(GOOD_TASKS, goals=[]))
    assert problem == "task t1 has no predecessor but there is no goal to judge it against"
    # no order at all (no trajectory)
    no_order = {k: v for k, v in t2.items() if k != "preceded_by"}
    (problem,) = check_task_relation_rules(_result([t1, no_order, t3]))
    assert problem.startswith("task t2 has no preceded_by")


def test_agent_call_without_task_needs_a_skip_reason():
    result = _result(GOOD_TASKS)
    assert check_agent_calls_not_lost(result) == []
    skipped = [
        _node("AgentCall", "m1", 20.0, inputContent="input:moderator:", outputContent="output:moderator:ok"),
        _node("AgentCall", "m2", 21.0, inputContent="input:moderator:sre-u1-exec-resume-2", outputContent="x"),
        _node("AgentCall", "m3", 22.0, inputContent="input:moderator:Go on",
              outputContent='output:moderator:{"next": "worker"}'),
    ]
    result["nodes"] += skipped
    assert check_agent_calls_not_lost(result) == []
    result["nodes"].append(
        _node("AgentCall", "w9", 23.0, agentName="worker", inputContent="input:worker:Check the fares",
              outputContent="output:worker:Fare is 75.")
    )
    assert check_agent_calls_not_lost(result) == ["worker (w9) has no task and no reason to skip it"]


def test_agent_call_needs_a_task_or_a_system_task():
    result = _result(GOOD_TASKS)
    assert check_agent_calls_have_a_task(result) == []
    # a routing turn: no task (skipped), but the standing task of its agent covers it
    router = _node("AgentCall", "m1", 20.0, agentName="moderator", inputContent="input:moderator:Go on",
                   outputContent='output:moderator:{"next": "worker"}')
    result["nodes"].append(router)
    assert check_agent_calls_have_a_task(result) == ["moderator (m1) has no task and no system task"]
    router["system_tasks"] = []  # an agent without any (no readable system prompt) is still a failure
    assert len(check_agent_calls_have_a_task(result)) == 1
    router["system_tasks"] = ["st1"]
    assert check_agent_calls_have_a_task(result) == []
    # a call with a task of its own needs no system task
    assert check_agent_calls_have_a_task(_result(GOOD_TASKS)) == []


def test_every_agent_needs_a_system_prompt():
    result = _result(GOOD_TASKS)
    assert check_agents_have_a_system_prompt(result) == []
    # an agent that only has agent calls (nothing readable in its LLM calls, as in webex)
    result["nodes"].append(_node("AgentCall", "w1", 30.0, agentName="webex_agent"))
    result["nodes"].append(_node("AgentCall", "w2", 31.0, agentName="webex_agent"))
    assert check_agents_have_a_system_prompt(result) == ["agent webex_agent has no system prompt"]
    # a cut prompt (SRE) counts: it is read as far as it goes
    _node_of(result, "llm1")["symbolic_state_elements"]["system_prompts"].append(
        {"id": "sp_w", "agent_id": "webex_agent", "truncated": True}
    )
    assert check_agents_have_a_system_prompt(result) == []


def test_every_tool_result_gives_one_fact_grounded_in_it():
    assert check_tool_results_give_facts(_result(GOOD_TASKS)) == []
    # a result without fact, an empty result without fact, a fact grounded elsewhere, two facts, a fact on an LLM call
    result = _result(GOOD_TASKS)
    result["nodes"] += [
        _node("ToolCall", "t_lost", 6.0, toolOutput="Fare: $80"),
        _node("ToolCall", "t_empty", 6.0, toolOutput=""),
        _node("ToolCall", "t_other", 6.0, {"facts": [{"id": "f2", "grounded_in": "tool1"}]}, toolOutput="x"),
        _node("ToolCall", "t_two", 6.0, {"facts": [{"id": "f3", "grounded_in": "t_two"}, {"id": "f4", "grounded_in": "t_two"}]},
              toolOutput="y"),
    ]
    _node_of(result, "llm1")["symbolic_state_elements"]["facts"] = [{"id": "f5", "grounded_in": "tool1"}]
    assert check_tool_results_give_facts(result) == [
        "LLMCall llm1 has 1 fact(s) but is not a tool call",
        "ToolCall t_lost has 0 fact(s), expected 1",
        "ToolCall t_other: fact f2 is grounded in tool1, not in this call",
        "ToolCall t_two has 2 fact(s), expected 1",
    ]


# --------------------------------------------------------------------------------------------
# The functional test: real KG, real LLM
# --------------------------------------------------------------------------------------------


def _session_ids(config: pytest.Config) -> list[str]:
    option = config.getoption("--sessions")
    if option:
        return [s.strip() for s in option.split(",") if s.strip()]
    lines = (line.split("#")[0].strip() for line in SESSIONS_FILE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if {"session_id", "check_name"} <= set(metafunc.fixturenames):
        pairs = [(sid, check) for sid in _session_ids(metafunc.config) for check in CHECKS]
        metafunc.parametrize(("session_id", "check_name"), pairs, ids=[f"{s[:8]}-{c}" for s, c in pairs])


def _extract(session_id: str, workdir: Path) -> Result:
    """Read the session from the KG and run the whole extraction (embeddings off), as the worker does, but
    without writing to the KG. The output is also saved under ``workdir``."""
    from symbolic_extractor_worker.worker import SymbolicExtractorWorker

    worker = SymbolicExtractorWorker("amqp://unused", "unused", same_claims=False)
    result = worker.extract_result(session_id)
    output = workdir / "output" / f"{session_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return json.loads(output.read_text(encoding="utf-8"))


def _mas_name(session_id: str) -> str:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
        from scope.client.neurosymbolic import get_mas_name
        from scope.dependencies import get_neo4j_connector

        return get_mas_name(get_neo4j_connector(), session_id) or "unknown MAS"
    except Exception as exc:  # the name only labels the report
        logger.warning("MAS name of %s not available: %s", session_id, exc)
        return "unknown MAS"


class FunctionalRun:
    """The extractions of the run (one per trace, shared by its checks) and the report."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self._results: dict[str, Result | Exception] = {}
        self.mas: dict[str, str] = {}
        #: session id -> [(check, status, problems)], in the order they ran.
        self.rows: dict[str, list[tuple[str, str, list[str]]]] = {}

    def result(self, session_id: str) -> Result:
        """The output of the trace; the extraction runs once, a failure is kept and raised again."""
        if session_id not in self._results:
            self.mas[session_id] = _mas_name(session_id)
            try:
                self._results[session_id] = _extract(session_id, self.workdir)
            except Exception as exc:
                self._results[session_id] = exc
        result = self._results[session_id]
        if isinstance(result, Exception):
            raise RuntimeError(f"extraction of {session_id} failed: {result!r}") from result
        return result

    def record(self, session_id: str, check: str, status: str, problems: list[str]) -> None:
        self.rows.setdefault(session_id, []).append((check, status, problems))

    def write_report(self, reporter: Any) -> None:
        reporter.write_sep("=", "functional check: output of the extraction, per trace")
        reporter.write_line(f"outputs in {self.workdir}")
        for session_id, rows in self.rows.items():
            reporter.write_line("")
            reporter.write_line(f"{self.mas.get(session_id, 'unknown MAS')}  ({session_id})", bold=True)
            for check, status, problems in rows:
                colour = {"PASS": {"green": True}, "FAIL": {"red": True}}.get(status, {"red": True, "bold": True})
                reporter.write_line(f"  [{status}] {check}", **colour)
                for problem in problems[:3]:
                    reporter.write_line(f"           {problem}")
                if len(problems) > 3:
                    reporter.write_line(f"           ... and {len(problems) - 3} more")
        total = [status for rows in self.rows.values() for _, status, _ in rows]
        reporter.write_line("")
        reporter.write_line(
            f"{total.count('PASS')} passed, {total.count('FAIL')} failed, {total.count('ERROR')} errors "
            f"on {len(self.rows)} traces"
        )


@pytest.fixture(scope="module")
def run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    from symbolic_extractor.llm import OpenAIChatClient

    if not OpenAIChatClient.from_env()._client.api_key:
        pytest.skip("no API key found (OPENAI_API_KEY / LLM_API_KEY) in the environment or .env")
    functional = FunctionalRun(tmp_path_factory.mktemp("functional"))
    yield functional
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        functional.write_report(reporter)


@pytest.mark.live
def test_extraction_output_of_trace(run: FunctionalRun, session_id: str, check_name: str) -> None:
    try:
        result = run.result(session_id)
    except RuntimeError as exc:
        run.record(session_id, check_name, "ERROR", [str(exc)[:200]])
        raise
    problems = CHECKS[check_name](result)
    run.record(session_id, check_name, "FAIL" if problems else "PASS", problems)
    assert not problems, f"{run.mas[session_id]} / {session_id}:\n" + "\n".join(problems)
