"""The symbolic state of a session, as rows, turned into a failure_detection ``Delta``.

No knowledge-graph access: the rows are what ``scope.client.neurosymbolic.get_symbolic_state`` returns
(nodes as ``{"id", "label", "props"}``, edges as ``{"source", "type", "target"}``), and the trajectory is
a ``SessionTrajectory``. Labels and relations are the ones of ``neurosymbolic-ontology.ttl``
(``Claim``, ``Fact``, ``Goal``, ``Task``, ``Constraint``, ``AgentCapability``, ``SystemPrompt``). What is
not in the rows as an element or a relation is rebuilt from the trajectory, the way the extractor does:

* the calls (kind, agent, owner, start, end, failure) come from the session trajectory;
* the transitions between agent calls come from the handoffs and the same heuristics as the extractor
  (``symbolic_state.build_transitions``), and give ``routed_by``;
* the claims in the state of an agent call (``state_claims``) are rebuilt from those transitions, the
  ``inputClaim`` links of its LLM calls and the claims they generated;
* the final call is the one the trajectory says wrote the final answer;
* a task counts as compared with the capabilities of its agent (``capability_checked``) when the agent has
  capabilities: the extractor compares nothing, and writes nothing, for an agent that has none.

Anything the rows do not hold (``providedTo``, ``sameClaims``, ...) is not read: the rules that need it do
not fire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from symbolic_extractor.models import SessionTrajectory
from symbolic_extractor.symbolic_state import build_transitions

from failure_detection import models as m
from failure_detection.graph import Delta

ELEMENT_LABELS = (
    "Claim",
    "Fact",
    "Goal",
    "Task",
    "Constraint",
    "AgentCapability",
    "SystemPrompt",
)

#: KG relation -> relation of the detectors (same names, except the ontology's ``conflictsWith``).
RELATIONS = {
    "supportedBy": m.SUPPORTED_BY,
    "derivedFrom": m.DERIVED_FROM,
    "conflictsWith": m.CONTRADICTS,
    "sameAs": m.SAME_AS,
    "producedBy": m.PRODUCED_BY,
    "fulfils": m.FULFILS,
    "achieves": m.ACHIEVES,
    "implements": m.IMPLEMENTS,
    "precededBy": m.PRECEDED_BY,
    "boundedBy": m.BOUNDED_BY,
    "supports": m.SUPPORTS,
    "providedTo": m.PROVIDED_TO,
}
#: KG relations that are a field of the element, not an edge of the graph.
POINTERS = ("generatedBy", "groundedIn", "ownedBy", "definedIn", "systemPromptOf")
#: LLM call -> what was in its input.
INPUTS = ("inputClaim", "inputFact")


@dataclass
class SymbolicState:
    """What the KG holds for one session, before it is turned into a Delta."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> properties
    labels: dict[str, str] = field(default_factory=dict)  # id -> label
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (source, type, target)

    @classmethod
    def from_rows(cls, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> SymbolicState:
        """From the rows of ``get_symbolic_state``: ``{id, label, props}`` and ``{source, type, target}``."""
        state = cls()
        for row in nodes:
            state.nodes[row["id"]] = dict(row["props"])
            state.labels[row["id"]] = row["label"]
        state.edges = [(r["source"], r["type"], r["target"]) for r in edges]
        return state

    def has_elements(self) -> bool:
        """Whether there is anything to detect on (a claim, a fact, a goal or a task)."""
        return any(self.ids(label) for label in ("Claim", "Fact", "Goal", "Task"))

    def ids(self, label: str) -> list[str]:
        return sorted(i for i, name in self.labels.items() if name == label)

    def targets(self, source: str, relation: str) -> list[str]:
        return [t for s, r, t in self.edges if s == source and r == relation]


#: Every relation the rows may hold, for the query that fetches them.
EDGE_TYPES = (*RELATIONS, *POINTERS, *INPUTS)


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(value) if isinstance(value, (list, tuple)) else (str(value),)


def _first(state: SymbolicState, source: str, relation: str) -> str | None:
    found = state.targets(source, relation)
    return found[0] if found else None


def _elements(state: SymbolicState, delta: Delta) -> set[str]:
    """Fill ``delta`` with the elements; return the agents that have capabilities."""
    for i in state.ids("SystemPrompt"):
        agent = _first(state, i, "systemPromptOf")
        if agent:
            delta.system_prompts.append(m.SystemPrompt(id=i, agent=agent))
    prompt_agent = {p.id: p.agent for p in delta.system_prompts}
    with_capabilities = {
        prompt_agent.get(_first(state, i, "definedIn") or "") for i in state.ids("AgentCapability")
    } - {None}

    for i in state.ids("Claim"):
        p = state.nodes[i]
        delta.claims.append(
            m.Claim(
                id=i,
                text=p.get("claimText", ""),
                force=p.get("claimForce") or "assertive",
                kind=p.get("claimKind"),
                generated_by=_first(state, i, "generatedBy") or "",
            )
        )
    for i in state.ids("Fact"):
        delta.facts.append(
            m.Fact(
                id=i,
                text=state.nodes[i].get("factText", ""),
                grounded_in=_first(state, i, "groundedIn") or "",
            )
        )
    for i in state.ids("Goal"):
        p = state.nodes[i]
        delta.goals.append(
            m.Goal(
                id=i,
                text=p.get("goalText", ""),
                success_criteria=_tuple(p.get("successCriteria")),
                status=p.get("status"),
            )
        )
    for i in state.ids("Task"):
        p = state.nodes[i]
        owner = _first(state, i, "ownedBy")
        prompt = _first(state, i, "definedIn")
        delta.tasks.append(
            m.Task(
                id=i,
                text=p.get("taskText", ""),
                acceptance_criteria=_tuple(p.get("acceptanceCriteria")),
                status=p.get("status"),
                owned_by=owner,
                source=p.get("source"),
                system_prompt=prompt,
                capability_checked=False,  # set in `_calls`: it needs the agent of the owner
            )
        )
    for i in state.ids("AgentCapability"):
        p = state.nodes[i]
        delta.capabilities.append(
            m.Capability(
                id=i,
                text=p.get("agentCapabilityText", ""),
                source=p.get("source"),
                system_prompt=_first(state, i, "definedIn"),
            )
        )
    for i in state.ids("Constraint"):
        p = state.nodes[i]
        delta.constraints.append(
            m.Constraint(
                id=i,
                text=p.get("constraintText", ""),
                strength=p.get("constraintStrength"),
                source=p.get("source"),
                system_prompt=_first(state, i, "definedIn"),
            )
        )
    delta.edges += [
        m.Edge(source=s, relation=RELATIONS[r], target=t)
        for s, r, t in state.edges
        if r in RELATIONS
    ]
    return {a for a in with_capabilities if a}


def _calls(
    state: SymbolicState, trajectory: SessionTrajectory, delta: Delta, with_capabilities: set[str]
) -> None:
    steps = {s["call_id"]: s for s in trajectory.steps}
    agent_steps = [s for s in trajectory.steps if s.get("kind") == "agent"]
    transitions = build_transitions(agent_steps, trajectory.handoffs)
    routed_by = {t.to_call: t.from_call for t in transitions if t.via in ("route", "handoff")}
    final_llm = trajectory.final_llm_call()

    inputs: dict[str, dict[str, list[str]]] = {"inputClaim": {}, "inputFact": {}}
    for source, relation, target in state.edges:
        if relation in inputs:
            inputs[relation].setdefault(source, []).append(target)
    generated: dict[str, list[str]] = {}
    for i in state.ids("Claim"):
        for call in state.targets(i, "generatedBy"):
            generated.setdefault(call, []).append(i)

    # The claims in the state of an agent call: what its LLM calls read or wrote, or, with no LLM
    # call, what it inherited from the agent calls that handed work to it.
    predecessors: dict[str, list[str]] = {}
    for t in transitions:
        predecessors.setdefault(t.to_call, []).append(t.from_call)
    llms: dict[str, list[str]] = {}
    for s in sorted(trajectory.steps, key=lambda s: s.get("start_time") or 0.0):
        if s.get("kind") == "llm":
            llms.setdefault(s.get("parent_id") or "", []).append(s["call_id"])
    agent_states: dict[str, list[str]] = {}
    for a in sorted(agent_steps, key=lambda s: s.get("start_time") or 0.0):
        mine = llms.get(a["call_id"], [])
        if mine:
            held = [
                c
                for llm in mine
                for c in (*inputs["inputClaim"].get(llm, []), *generated.get(llm, []))
            ]
        else:
            held = [c for p in predecessors.get(a["call_id"], []) for c in agent_states.get(p, [])]
        agent_states[a["call_id"]] = list(dict.fromkeys(held))

    kinds = {"agent": "agent", "llm": "llm", "tool": "tool"}
    for s in trajectory.steps:
        kind = kinds.get(s.get("kind"))
        if kind is None:
            continue
        start = s.get("start_time") or 0.0
        delta.calls.append(
            m.Call(
                id=s["call_id"],
                kind=kind,
                agent=s.get("agent_id") or None,
                parent=(s.get("parent_id") or None) if kind != "agent" else None,
                start=start,
                end=start + (s.get("duration_ms") or 0.0) / 1000.0,
                final=s["call_id"] == final_llm,
                routed_by=routed_by.get(s["call_id"]),
                input_claims=tuple(inputs["inputClaim"].get(s["call_id"], [])),
                input_facts=tuple(inputs["inputFact"].get(s["call_id"], [])),
                state_claims=tuple(agent_states.get(s["call_id"], [])),
                failed=kind == "tool" and s.get("success") is False,
            )
        )
    delta.transitions = [
        m.Transition(from_call=t.from_call, to_call=t.to_call, via=t.via) for t in transitions
    ]
    # A task given to an agent that has capabilities was compared with them.
    delta.tasks = [
        t.model_copy(
            update={
                "capability_checked": bool(
                    t.owned_by and steps.get(t.owned_by, {}).get("agent_id") in with_capabilities
                )
            }
        )
        for t in delta.tasks
    ]


def build_delta(state: SymbolicState, trajectory: SessionTrajectory) -> Delta:
    """The graph the detectors read: the elements of ``state`` and the calls of ``trajectory``."""
    delta = Delta()
    with_capabilities = _elements(state, delta)
    _calls(state, trajectory, delta, with_capabilities)
    return delta
