"""The data the page shows, built from the nodes and edges of an extraction output (see ``SessionGraph``).

Elements (claims, tasks, goals, ...) come from the ``symbolic_state_elements`` of the nodes, and every relation
between them (``input_claim``, ``supported_by``, ``same_as``, ``produced_by``, ``supports``, ...) from the
``edges``, not from the attributes the nodes also carry for each element.

Some flags say that a relation *was judged* even when it has no edge (a claim that fulfils no task, a task no
capability supports): an edge only says that a relation holds, not that it was asked. For these the page
reads the element itself, where the extractor leaves the key (``fulfils``, ``fulfils_task``,
``supported_by_capabilities``, ``preceded_by``) once it has judged; ``reply`` (a claim that stays in its agent
call, as opposed to one that travels back to the caller) is read the same way.
"""

from __future__ import annotations

import re
from typing import Any

from claims_page.graph import Element, SessionGraph

ROUTING_RE = re.compile(r'^\s*\{\s*"next"')


def build_payload(output: dict[str, Any], trajectory: dict[str, Any] | None) -> dict[str, Any]:
    graph = SessionGraph(output)
    steps = {s["call_id"]: s for s in (trajectory or {}).get("steps", []) if s.get("call_id")}
    llm_nodes = graph.nodes_of_type("LLMCall")
    llm_nodes.sort(key=lambda n: (n["node"].get("startTime") is None, n["node"].get("startTime") or 0.0))
    t0 = min((n["node"]["startTime"] for n in llm_nodes if n["node"].get("startTime") is not None), default=0.0)

    calls = []
    for node in llm_nodes:
        raw = node["node"]
        step = steps.get(raw.get("id"), {})
        agent = step.get("agent_id") or raw.get("agentName") or "unknown"
        calls.append(
            {
                "id": raw.get("id"),
                "agent": agent,
                "agent_call": step.get("parent_id") or f"{agent}?",
                "t": None if raw.get("startTime") is None else round(raw["startTime"] - t0, 2),
                "duration_ms": step.get("duration_ms"),
                "model": raw.get("modelName") or raw.get("llmName"),
                "inputs": graph.out("input_claim", raw.get("id")),
                "input_facts": graph.out("input_fact", raw.get("id")),
                "claims": [
                    _claim(graph, c["id"], c) for c in node.get("symbolic_state_elements", {}).get("claims", [])
                ],
            }
        )
    facts = _facts(graph, steps, t0)

    agents: list[str] = []
    for call in calls:
        if call["agent"] not in agents:
            agents.append(call["agent"])
    goals, goal_agent, conversation = _goals(graph, steps, agents)
    return {
        "session_id": output.get("session_id"),
        "agents": agents,
        "calls": calls,
        "facts": facts,
        "goals": goals,
        "goal_agent": goal_agent,
        "conversation": conversation,
        "tasks": _tasks(graph, calls),
        "briefs": _briefs(graph),
        "system": _system_tasks(graph, agents),
        "constraints": _constraints(graph),
    }


def _claim(graph: SessionGraph, claim_id: str, c: dict[str, Any]) -> dict[str, Any]:
    """A claim with the ids it is related to, one list per relation."""
    return {
        "id": claim_id,
        "text": c.get("claim_text", ""),
        "force": c.get("claim_force"),
        "kind": c.get("claim_kind"),
        "snippet": c.get("verbatim_source"),
        "routing": bool(ROUTING_RE.match(c.get("verbatim_source") or "")),
        "same": graph.out("same_claim", claim_id),
        "supported_by": graph.out("supported_by", claim_id),
        **_claim_relations(graph, claim_id),
        "answers": graph.out("answers", claim_id),
        "fulfils": graph.out("fulfils", claim_id),
        # the final answer was judged against the goal (``fulfils`` is there, maybe empty)
        "judged": c.get("fulfils") is not None,
        # the task(s) of the agent call of the LLM call that generated the claim
        "produced_by": graph.out("produced_by", claim_id),
        # True: the claim is in the reply its agent call gives back to its caller; False: it stays
        # in the agent call; None: the agent call was not delegated to
        "reply": c.get("reply"),
        "fulfils_task": graph.out("fulfils_task", claim_id),
        # the task was judged (``fulfils_task`` is there, maybe empty)
        "task_judged": c.get("fulfils_task") is not None,
    }


def _claim_relations(graph: SessionGraph, claim_id: str) -> dict[str, list[str]]:
    """The relations of ``ClaimRelationExtractor``. ``conflicts_with`` and ``same_as`` are recorded in both
    directions, the page draws a pair once."""
    return {
        "conflicts_with": graph.out("conflicts_with", claim_id),
        "same_as": graph.out("same_as", claim_id),
        "derived_from": graph.out("derived_from", claim_id),
    }


def _facts(graph: SessionGraph, steps: dict[str, Any], t0: float) -> dict[str, Any]:
    """The facts of the ToolCall nodes, with the claims they are in the context of (``in_context``), the
    claims they support (``supported_by`` read backwards), the arguments of the call and the claims that
    announce the call (``announces`` read backwards: why the agent called the tool)."""
    facts: dict[str, Any] = {}
    for node in graph.nodes_of_type("ToolCall"):
        raw = node["node"]
        step = steps.get(raw.get("id"), {})
        for fact in node.get("symbolic_state_elements", {}).get("facts", []):
            facts[fact["id"]] = {
                "id": fact["id"],
                "text": fact.get("fact_text", ""),
                "snippet": fact.get("verbatim_source"),
                "source_type": fact.get("fact_source_type"),
                "tool": fact.get("tool_name") or raw.get("toolName") or raw.get("name"),
                "arguments": fact.get("tool_arguments") or {},
                "announced_by": graph.into("announces", raw.get("id")),
                "tool_call": raw.get("id"),
                "agent": step.get("agent_id") or raw.get("agentName") or "unknown",
                "t": None if raw.get("startTime") is None else round(raw["startTime"] - t0, 2),
                "in_context": graph.out("in_context", fact["id"]),
                "supports": graph.into("supported_by", fact["id"]),
            }
    return facts


def _constraints(graph: SessionGraph) -> dict[str, dict[str, Any]]:
    """Every constraint of the output by id: its text, strength, origin (``user`` / ``system``) and, for a
    system constraint, the agent whose system prompt it was read from."""
    return {
        e.id: {
            "id": e.id,
            "text": e.data.get("constraint_text", ""),
            "strength": e.data.get("constraint_strength", "hard"),
            "origin": e.data.get("origin"),
            "agent": e.data.get("agent_id"),
            "read_from": e.data.get("read_from"),
            "truncated": bool(e.data.get("truncated")),
        }
        for e in graph.elements("constraints")
    }


def _tasks(graph: SessionGraph, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tasks extracted from the AgentCall nodes, with the agent call they belong to (the LLM
    calls of that agent call are found through ``agent_call``)."""
    tasks: list[dict[str, Any]] = []
    for node in graph.nodes_of_type("AgentCall"):
        raw = node["node"]
        agent_call = raw.get("id")
        agent = next((c["agent"] for c in calls if c["agent_call"] == agent_call), None)
        agent = agent or raw.get("agentName") or raw.get("name") or "unknown"
        for task in node.get("symbolic_state_elements", {}).get("tasks", []):
            tasks.append(
                {
                    "id": task.get("id"),
                    "text": task.get("task_text", ""),
                    "criteria": list(task.get("acceptance_criteria") or []),
                    "inferred": bool(task.get("inferred")),
                    "status": task.get("status"),
                    "same_as": graph.out("same_as", task["id"]),
                    "implements": graph.out("implements", task["id"]),
                    "achieves": graph.out("achieves", task["id"]),
                    "bounded_by": graph.out("bounded_by", task["id"]),
                    # the capabilities of the agent were compared with the task (``supports`` was judged)
                    "has_support": "supported_by_capabilities" in task,
                    "supported_by": graph.into("supports", task["id"]),
                    # the agent calls could be ordered (``preceded_by`` was computed)
                    "has_order": "preceded_by" in task,
                    "preceded_by": graph.out("preceded_by", task["id"]),
                    "agent_call": agent_call,
                    "agent": agent,
                    "input": raw.get("inputContent"),
                    "output": raw.get("outputContent"),
                }
            )
    # ``same_as`` is recorded in both directions: a task lists the earlier identical ones only
    position = {t["id"]: i for i, t in enumerate(tasks)}
    for i, task in enumerate(tasks):
        task["same_as"] = [o for o in task["same_as"] if position.get(o, i) < i]
    return tasks


def _briefs(graph: SessionGraph) -> list[dict[str, Any]]:
    """The delegation claims of each AgentCall (the claims of the brief it received), with the brief and
    the ids of the claims it could restate (the ``input_claim`` of the agent call). The page shows them on the
    first LLM call of that agent call."""
    briefs: list[dict[str, Any]] = []
    for node in graph.nodes_of_type("AgentCall"):
        claims = node.get("symbolic_state_elements", {}).get("delegation_claims") or []
        if not claims:
            continue
        raw = node["node"]
        briefs.append(
            {
                "agent_call": raw.get("id"),
                "input": raw.get("inputContent"),
                "source_claims": graph.out("input_claim", raw.get("id")),
                "claims": [
                    {
                        "id": c["id"],
                        "text": c.get("claim_text", ""),
                        "force": c.get("claim_force"),
                        "kind": c.get("claim_kind"),
                        "snippet": c.get("verbatim_source"),
                        "routing": False,
                        "delegation": True,
                        "caller_agent": c.get("caller_agent"),
                        "generated_by": c.get("generated_by"),
                        "delegated_to": c.get("delegated_to"),
                        **_claim_relations(graph, c["id"]),
                    }
                    for c in claims
                ],
            }
        )
    return briefs


def _system_tasks(graph: SessionGraph, agents: list[str]) -> list[dict[str, Any]]:
    """The standing tasks of each agent (read from its system prompt), with the prompt and the number of
    agent calls that carry them (``has_system_task`` / ``has_capability``), in the order of ``agents`` (the
    others after). The constraints and the capabilities read from the same prompt come with them."""
    prompts = {e.data["agent_id"]: e.data for e in graph.elements("system_prompts")}
    tasks = _by_agent(graph.elements("system_tasks"))
    constraints = _by_agent(e for e in graph.elements("constraints") if e.data.get("origin") == "system")
    capabilities = _by_agent(graph.elements("capabilities"))
    names = [a for a in agents if a in prompts or a in tasks or a in constraints or a in capabilities]
    names += [a for a in [*prompts, *tasks, *constraints, *capabilities] if a not in names]
    out = []
    for agent in dict.fromkeys(names):
        prompt = prompts.get(agent) or {}
        calls = {
            call
            for edge_type, items in (("has_system_task", tasks), ("has_capability", capabilities))
            for e in items.get(agent, [])
            for call in graph.into(edge_type, e.id)
        }
        out.append(
            {
                "agent": agent,
                "calls": len(calls),
                "prompt": prompt.get("system_prompt"),
                "truncated": bool(
                    prompt.get("truncated")
                    or any(
                        e.data.get("truncated")
                        for e in [*tasks.get(agent, []), *constraints.get(agent, []), *capabilities.get(agent, [])]
                    )
                ),
                "constraints": [e.id for e in constraints.get(agent, [])],
                "capabilities": [{"id": e.id, "text": e.data.get("capability_text", "")} for e in capabilities.get(agent, [])],
                "tasks": [
                    {
                        "id": e.id,
                        "text": e.data.get("task_text", ""),
                        "criteria": list(e.data.get("acceptance_criteria") or []),
                        "has_support": "supported_by_capabilities" in e.data,
                        "supported_by": graph.into("supports", e.id),
                    }
                    for e in tasks.get(agent, [])
                ],
            }
        )
    return out


def _by_agent(elements: Any) -> dict[str, list[Element]]:
    grouped: dict[str, list[Element]] = {}
    for element in elements:
        grouped.setdefault(element.data["agent_id"], []).append(element)
    return grouped


def _goals(
    graph: SessionGraph, steps: dict[str, Any], agents: list[str]
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """The goals of the session (extracted from its ``Conversation`` node), the agent they are
    attached to (the one of the first AgentCall, added to ``agents`` when it has no LLM call), and
    the user request / final response they were read from."""
    goals: list[dict[str, Any]] = []
    conversation: dict[str, Any] = {}
    for node in graph.nodes_of_type("Conversation"):
        conversation = {"query": node["node"].get("query"), "response": node["node"].get("response")}
        for goal in node.get("symbolic_state_elements", {}).get("goals", []):
            goals.append(
                {
                    "id": goal.get("id"),
                    "text": goal.get("goal_text", ""),
                    "criteria": list(goal.get("success_criteria") or []),
                    "rationale": goal.get("rationale"),
                    "inferred": bool(goal.get("inferred")),
                    "status": goal.get("status"),
                    "bounded_by": graph.out("bounded_by", goal["id"]),
                }
            )
    if not goals:
        return [], None, conversation

    agent_calls = graph.nodes_of_type("AgentCall")
    agent_calls.sort(key=lambda n: (n["node"].get("startTime") is None, n["node"].get("startTime") or 0.0))
    first = None
    for node in agent_calls:
        raw = node["node"]
        first = steps.get(raw.get("id"), {}).get("agent_id") or raw.get("agentName") or raw.get("name")
        if first:
            break
    first = first or (agents[0] if agents else "session")
    if first not in agents:
        agents.insert(0, first)
    return goals, first, conversation
