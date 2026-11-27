"""The graph the detectors read: elements by id and typed edges, followed both ways."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from failure_detection.models import (
    Call,
    Capability,
    Claim,
    Constraint,
    Edge,
    Fact,
    Goal,
    SystemPrompt,
    Task,
    Transition,
)

KINDS = (
    "call",
    "claim",
    "fact",
    "goal",
    "task",
    "constraint",
    "capability",
    "system_prompt",
    "transition",
)  # what a rule can trigger on


class Delta(BaseModel):
    """An increment of the graph. A full graph is just a big delta."""

    calls: list[Call] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    system_prompts: list[SystemPrompt] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def kinds(self) -> set[str]:
        """What the delta holds: one of KINDS, or "edge:<relation>". Rules trigger on it."""
        plural = {"capability": "capabilities"}
        kinds = {k for k in KINDS if getattr(self, plural.get(k, k + "s"))}
        return kinds | {f"edge:{e.relation}" for e in self.edges}


class SymbolicGraph:
    def __init__(self) -> None:
        self.calls: dict[str, Call] = {}
        self.claims: dict[str, Claim] = {}
        self.facts: dict[str, Fact] = {}
        self.goals: dict[str, Goal] = {}
        self.tasks: dict[str, Task] = {}
        self.constraints: dict[str, Constraint] = {}
        self.capabilities: dict[str, Capability] = {}
        self.system_prompts: dict[str, SystemPrompt] = {}
        self.transitions: list[Transition] = []
        self.order: dict[str, int] = {}  # arrival order of claims, to break ties in time
        self._out: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._in: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.cache: dict = {}

    def add(self, delta: Delta) -> None:
        """Merge a delta. Idempotent: the same id overwrites, the same edge is kept once."""
        for call in delta.calls:
            self.calls[call.id] = call
        for claim in delta.claims:
            self.order.setdefault(claim.id, len(self.order))
            self.claims[claim.id] = claim
        for fact in delta.facts:
            self.facts[fact.id] = fact
        for goal in delta.goals:
            self.goals[goal.id] = goal
        for task in delta.tasks:
            self.tasks[task.id] = task
        for constraint in delta.constraints:
            self.constraints[constraint.id] = constraint
        for capability in delta.capabilities:
            self.capabilities[capability.id] = capability
        for prompt in delta.system_prompts:
            self.system_prompts[prompt.id] = prompt
        for t in delta.transitions:
            if t not in self.transitions:
                self.transitions.append(t)
        for e in delta.edges:
            if e.target not in self._out[(e.source, e.relation)]:
                self._out[(e.source, e.relation)].append(e.target)
                self._in[(e.target, e.relation)].append(e.source)
        self.cache.clear()  # cached primitives may no longer hold

    def out(self, id: str, relation: str) -> list[str]:
        return self._out.get((id, relation), [])

    def into(self, id: str, relation: str) -> list[str]:
        return self._in.get((id, relation), [])

    def time(self, claim_id: str) -> tuple[float, int]:
        """When a claim was made: the end of the call that generated it, then arrival order."""
        call = self.calls.get(self.claims[claim_id].generated_by)
        return (call.end if call else 0.0, self.order[claim_id])
