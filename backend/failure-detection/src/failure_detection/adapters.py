"""Turn the output of the symbolic extractor (one session, or a part of one) into a Delta."""

from __future__ import annotations

from typing import Any

from failure_detection import models as m
from failure_detection.graph import Delta

_KINDS = {"AgentCall": "agent", "LLMCall": "llm", "ToolCall": "tool"}

# Fields of an extracted element that list the ids it is linked to -> relation (from the element).
_CLAIM_LINKS = {
    "supported_by": m.SUPPORTED_BY,
    "derived_from": m.DERIVED_FROM,
    "conflicts_with": m.CONTRADICTS,
    "same_as": m.SAME_AS,
    "same_claims": m.SAME_CLAIMS,
    "produced_by": m.PRODUCED_BY,
    "fulfils_task": m.FULFILS,
}
_TASK_LINKS = {
    "achieves": m.ACHIEVES,
    "implements": m.IMPLEMENTS,
    "preceded_by": m.PRECEDED_BY,
    "same_as": m.SAME_AS,
}
# `bounds` (goals and tasks) is handled apart; these field names follow the ontology.
_CONSTRAINT_LINKS = {"provided_to": m.PROVIDED_TO}  # `bounds` (goals and tasks) is handled apart
_DELEGATING = {"route", "handoff"}  # transitions that hand work to another agent call


def delta_from_extraction(result: dict[str, Any]) -> Delta:
    """`result` has the shape of the extractor's output: {"nodes": [{"node_type", "node", ...}]}."""
    nodes = result.get("nodes", [])
    agent_calls = [n["node"] for n in nodes if n["node_type"] == "AgentCall"]

    def enclosing(raw: dict[str, Any]) -> dict[str, Any] | None:
        """The agent call whose time window contains this call (the span tree is not kept)."""
        inside = [
            a
            for a in agent_calls
            if a["id"] != raw["id"]
            and a["startTime"] <= raw["startTime"]
            and raw["endTime"] <= a["endTime"]
        ]
        return max(inside, key=lambda a: a["startTime"], default=None)

    symbolic_state = result.get("symbolic_state") or {}
    transitions = symbolic_state.get("transitions", [])
    agent_states = symbolic_state.get("agent_states", {})
    routed_by = {t["to_call"]: t["from_call"] for t in transitions if t.get("via") in _DELEGATING}
    delta = Delta()
    # The extractor compares an agent's capabilities with a task once, and lists the tasks it did:
    # a task with no `supports` edge only lacks a capability if it was compared.
    evaluations = result.get("evaluations") or {}
    checked = {e["task_id"] for e in evaluations.get("capabilities", []) if "task_id" in e}

    def link(source: str, element: dict[str, Any], fields: dict[str, str]) -> None:
        for field, relation in fields.items():
            targets = element.get(field) or []
            delta.edges += [m.Edge(source=source, relation=relation, target=t) for t in targets]

    for n in nodes:
        raw = n["node"]
        if n["node_type"] in _KINDS:
            owner = raw if n["node_type"] == "AgentCall" else enclosing(raw)
            delta.calls.append(
                m.Call(
                    id=raw["id"],
                    kind=_KINDS[n["node_type"]],
                    agent=owner.get("agentName") if owner else None,
                    parent=owner["id"] if owner and owner is not raw else None,
                    start=raw.get("startTime") or 0.0,
                    end=raw.get("endTime") or 0.0,
                    routed_by=routed_by.get(raw["id"]),
                    input_claims=n.get("input_claims") or (),
                    input_constraints=n.get("input_constraints") or (),
                    input_facts=n.get("input_facts") or (),
                    state_claims=agent_states.get(raw["id"]) or (),
                    failed=n["node_type"] == "ToolCall"
                    and (raw.get("success") is False or bool(raw.get("contains_error"))),
                )
            )
        # The elements sit on different nodes: the Goal on the first one, tasks on agent calls...
        elements = n.get("symbolic_state_elements") or {}
        for c in elements.get("claims", []):
            delta.claims.append(
                m.Claim(
                    id=c["id"],
                    text=c["claim_text"],
                    force=c["claim_force"],
                    kind=c.get("claim_kind"),
                    generated_by=c.get("generated_by") or raw["id"],
                )
            )
            link(c["id"], c, _CLAIM_LINKS)
        for f in elements.get("facts", []):
            delta.facts.append(
                m.Fact(
                    id=f["id"], text=f["fact_text"], grounded_in=f.get("grounded_in") or raw["id"]
                )
            )
        for g in elements.get("goals", []):
            delta.goals.append(
                m.Goal(
                    id=g["id"],
                    text=g["goal_text"],
                    success_criteria=g.get("success_criteria") or (),
                    status=g.get("status"),
                )
            )
        for t in elements.get("tasks", []):
            delta.tasks.append(
                m.Task(
                    id=t["id"],
                    text=t["task_text"],
                    acceptance_criteria=t.get("acceptance_criteria") or (),
                    status=t.get("status"),
                    owned_by=t.get("owned_by"),
                    capability_checked=t["id"] in checked,
                )
            )
            link(t["id"], t, _TASK_LINKS)
        for p in elements.get("system_prompts", []):
            delta.system_prompts.append(m.SystemPrompt(id=p["id"], agent=p["agent_id"]))
        for t in elements.get("system_tasks", []):  # standing tasks, from the agent's system prompt
            delta.tasks.append(  # as in the KG: a Task with source "system", definedIn its prompt
                m.Task(
                    id=t["id"],
                    text=t["task_text"],
                    acceptance_criteria=t.get("acceptance_criteria") or (),
                    source="system",
                    system_prompt=t.get("system_prompt_id"),
                    capability_checked=t["id"] in checked,
                )
            )
        for c in elements.get("capabilities", []):
            delta.capabilities.append(
                m.Capability(
                    id=c["id"],
                    text=c["capability_text"],
                    source="system",
                    system_prompt=c.get("system_prompt_id"),
                )
            )
        for k in elements.get("constraints", []):
            delta.constraints.append(
                m.Constraint(
                    id=k["id"],
                    text=k["constraint_text"],
                    strength=k.get("constraint_strength"),
                    source=k.get("origin"),
                    system_prompt=k.get("system_prompt_id"),
                )
            )
            link(k["id"], k, _CONSTRAINT_LINKS)
            delta.edges += [
                m.Edge(source=t, relation=m.BOUNDED_BY, target=k["id"])
                for t in k.get("bounds") or []
            ]
    delta.edges += [  # the extractor's list of edges is the truth for what it judged late
        m.Edge(source=e["source_id"], relation=m.SUPPORTS, target=e["target_id"])
        for e in result.get("edges", [])
        if e.get("edge_type") == "supports"
    ]
    delta.transitions = [
        m.Transition(from_call=t["from_call"], to_call=t["to_call"], via=t["via"])
        for t in transitions
    ]
    return delta
