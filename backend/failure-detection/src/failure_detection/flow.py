"""Primitives about the flow of the agents: who passed control to whom, and what travelled."""

from __future__ import annotations

from dataclasses import dataclass

from failure_detection.graph import SymbolicGraph
from failure_detection.models import CONTRADICTS, Call, Transition
from failure_detection.primitives import (
    agent_call_of,
    agent_of,
    claims_of,
    primitive,
    restates,
    unsupported,
)

HANDOFFS = ("route", "handoff")  # transitions that hand work to the next agent call


@dataclass(frozen=True)
class Architecture:
    kind: str  # "hub": one agent routes to the others; "peer": handoffs; "iterative": neither
    hub: str | None  # the name of the hub agent


@primitive
def architecture(g: SymbolicGraph) -> Architecture:
    """How the agents are organised, from the transitions. Needed by: the conflict rules."""
    routers = {
        g.calls[t.from_call].agent
        for t in g.transitions
        if t.via == "route" and t.from_call in g.calls
    } - {None}
    if any(t.via == "handoff" for t in g.transitions):
        return Architecture("peer", None)
    if len(routers) == 1:
        return Architecture("hub", next(iter(routers)))
    return Architecture("iterative", None)


@primitive
def handoffs(g: SymbolicGraph) -> tuple[Transition, ...]:
    """The transitions that pass work on, in the order the receiving calls started."""
    found = [t for t in g.transitions if t.via in HANDOFFS and t.to_call in g.calls]
    return tuple(sorted(found, key=lambda t: g.calls[t.to_call].start))


def first_llm(g: SymbolicGraph, agent_call: str) -> Call | None:
    """The first LLM call of an agent call."""
    llms = [c for c in g.calls.values() if c.kind == "llm" and c.parent == agent_call]
    return min(llms, key=lambda c: c.start, default=None)


@primitive
def dropped_at(g: SymbolicGraph, handoff: Transition) -> tuple[str, ...]:
    """Claims in the predecessor's state that the successor's first LLM call did not receive.

    Needed by: DR-2.
    """
    before, after = g.calls.get(handoff.from_call), first_llm(g, handoff.to_call)
    if not before or not after:
        return ()
    return tuple(c for c in before.state_claims if c not in after.input_claims)


@primitive
def new_facts(g: SymbolicGraph, agent_call: str) -> tuple[str, ...]:
    """Facts from the tool calls made inside an agent call."""
    return tuple(f.id for f in g.facts.values() if agent_call_of(g, f.grounded_in) == agent_call)


@primitive
def restatement_ratio(g: SymbolicGraph, handoff: Transition) -> float | None:
    """The share of the successor's claims that restate a claim it received. Needed by: DC-1."""
    claims = claims_of(g, handoff.to_call)
    first = first_llm(g, handoff.to_call)
    if not claims or not first:
        return None
    restating = [c for c in claims if any(r in first.input_claims for r in restates(g, c))]
    return len(restating) / len(claims)


@primitive
def stale(g: SymbolicGraph, call_id: str) -> tuple[tuple[str, str], ...]:
    """(b, a) where claim b, made earlier by another agent, contradicts a claim a in the call's
    input, and b is not in the input. Needed by: CC-4.
    """
    call = g.calls[call_id]
    found = []
    for a in call.input_claims:
        if a not in g.claims:
            continue
        for b in g.out(a, CONTRADICTS) + g.into(a, CONTRADICTS):
            if b not in g.claims or b in call.input_claims:
                continue
            other = g.calls.get(g.claims[b].generated_by)
            if other and other.agent not in (None, call.agent) and other.end <= call.start:
                found.append((b, a))
    return tuple(dict.fromkeys(found))


@primitive
def after_tool_failure(g: SymbolicGraph, agent_call: str) -> tuple[str, ...]:
    """Unsupported claims an agent call made after one of its tools failed. Needed by: IE-1."""
    failed = [
        c.end
        for c in g.calls.values()
        if c.kind == "tool" and c.failed and agent_call_of(g, c.id) == agent_call
    ]
    if not failed:
        return ()
    first_failure = min(failed)
    return tuple(
        c
        for c in claims_of(g, agent_call)
        if g.calls[g.claims[c].generated_by].start >= first_failure and unsupported(g, c)
    )


def no_new_facts(g: SymbolicGraph, earlier_call: str, later_call: str) -> bool:
    """Did the later call receive no fact that the earlier one had not?"""
    return set(g.calls[later_call].input_facts) <= set(g.calls[earlier_call].input_facts)


def same_query(g: SymbolicGraph, a: str, b: str) -> bool:
    """Do two claims answer the same task (or tasks that say the same)? Via `producedBy`."""
    ta, tb = g.out(a, "producedBy"), g.out(b, "producedBy")
    return any(x == y or y in g.out(x, "sameAs") + g.into(x, "sameAs") for x in ta for y in tb)


def agents_holding(g: SymbolicGraph, claim_id: str) -> set[str]:
    """The agents that made the claim, or a claim that restates it."""
    return {agent_of(g, c) for c in (claim_id, *restates(g, claim_id))} - {None}
