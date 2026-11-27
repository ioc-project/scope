"""The objects the detectors work on.

A view of the ontology's nodes shaped for the rules: one Call for agent, LLM and tool calls, and
ids instead of edges for where an element comes from. Relation names are those of the ontology
(tests/test_ontology.py checks them against the generated models).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Relations (edge types).
SUPPORTED_BY = "supportedBy"  # claim -> fact
DERIVED_FROM = (
    "derivedFrom"  # claim -> claim, or claim -> fact (the extractor does not tell them apart)
)
CONTRADICTS = "contradicts"  # any element -> any element, symmetric
SAME_AS = "sameAs"  # claim <-> claim, symmetric
SAME_CLAIMS = "sameClaims"  # claim <-> claim, symmetric
ACHIEVES = "achieves"  # task -> goal
IMPLEMENTS = "implements"  # task -> task (an assigned task implements a standing one)
PRECEDED_BY = "precededBy"  # task -> task
PRODUCED_BY = "producedBy"  # claim -> task whose work produced it
FULFILS = "fulfils"  # claim -> task it fulfils
PROVIDED_TO = "providedTo"  # constraint -> agent call that received it
BOUNDED_BY = "boundedBy"  # task or goal -> constraint that applies to it
SUPPORTS = "supports"  # capability -> task it lets the agent do


class Element(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str


class Call(Element):
    """An agent, LLM or tool call of the trajectory."""

    kind: Literal["agent", "llm", "tool"]
    agent: str | None = None
    parent: str | None = None  # id of the enclosing Call
    start: float = 0.0
    end: float = 0.0
    final: bool = False  # the call that produces the final answer, when the extractor says so
    routed_by: str | None = None  # id of the agent Call that routed the work to this one
    input_claims: tuple[str, ...] = ()  # ids of the claims in the call's input
    input_constraints: tuple[str, ...] = ()  # ids of the constraints in the call's input
    input_facts: tuple[str, ...] = ()  # ids of the facts in the call's input
    state_claims: tuple[str, ...] = ()  # an agent call: the claims in its state when it starts
    failed: bool = False  # a tool call that failed


class Claim(Element):
    text: str
    force: str = "assertive"  # assertive | committing | directive (to become kind/force)
    kind: str | None = None
    generated_by: str  # id of the Call


class Fact(Element):
    text: str
    grounded_in: str  # id of the tool Call


class Goal(Element):
    text: str
    success_criteria: tuple[str, ...] = ()
    status: str | None = None


class SystemPrompt(Element):
    """The standing instructions of an agent (mas:SystemPrompt, `systemPromptOf` its agent)."""

    agent: str  # name of the agent it belongs to


class Task(Element):
    text: str
    acceptance_criteria: tuple[str, ...] = ()
    status: str | None = None
    owned_by: str | None = None  # id of the agent Call the task was given to
    source: str | None = None  # mas:source: "system" for a standing task; none when assigned
    system_prompt: str | None = None  # mas:definedIn: id of the SystemPrompt of a standing task
    capability_checked: bool = False  # the agent's capabilities were compared with this task

    @property
    def standing(self) -> bool:
        """A task the agent's system prompt gives it, not one it was assigned."""
        return self.source == "system"


class Capability(Element):
    text: str
    source: str | None = "system"  # mas:source
    system_prompt: str | None = None  # mas:definedIn: its agent is the one the prompt belongs to


class Constraint(Element):
    text: str
    strength: str | None = None  # "hard" or "soft"
    source: str | None = None  # mas:source: "system" (operator), "user" or "agent"
    system_prompt: str | None = None  # mas:definedIn: set for "system" and "agent" constraints


class Transition(BaseModel):
    """How control passed between two agent calls: route, return, sequence, end or handoff."""

    model_config = ConfigDict(frozen=True)

    from_call: str
    to_call: str
    via: str


class Edge(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    relation: str
    target: str
