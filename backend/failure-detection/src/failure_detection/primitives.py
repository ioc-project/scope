"""Shared computations over the graph. Plain functions; the rules call them.

Each takes the graph and ids (hashable, so results are cached until the graph changes).
Each one's docstring says which rules need it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps

from failure_detection.graph import SymbolicGraph
from failure_detection.models import (
    ACHIEVES,
    BOUNDED_BY,
    CONTRADICTS,
    DERIVED_FROM,
    PRECEDED_BY,
    PRODUCED_BY,
    PROVIDED_TO,
    SAME_AS,
    SAME_CLAIMS,
    SUPPORTED_BY,
    SUPPORTS,
    Claim,
)

NO_SUPPORT_NEEDED = {"committing", "directive"}  # claims that do not state something checkable


def primitive(fn):
    """Cache the result per graph version: `add` clears the cache."""

    @wraps(fn)
    def wrapper(g: SymbolicGraph, *args):
        key = (fn.__name__, args)
        if key not in g.cache:
            g.cache[key] = fn(g, *args)
        return g.cache[key]

    return wrapper


def is_coordinative(claim: Claim) -> bool:
    """Does the claim coordinate the work (commit, direct) instead of stating something?"""
    return claim.force in NO_SUPPORT_NEEDED or claim.kind == "coordinative"


def needs_support(claim: Claim) -> bool:
    """Does the claim state something a fact should back? Not an opinion or a directive."""
    return not is_coordinative(claim) and claim.kind != "evaluative"


def is_commitment(claim: Claim) -> bool:
    """Does the claim commit its agent to an action or a choice?"""
    return claim.force == "committing"


def agent_of(g: SymbolicGraph, claim_id: str) -> str | None:
    """The name of the agent a claim comes from."""
    call = g.calls.get(g.claims[claim_id].generated_by)
    return call.agent if call else None


def facts_in(g: SymbolicGraph, ids) -> int:
    """How many of these elements are facts."""
    return sum(1 for x in ids if x in g.facts)


@primitive
def restates(g: SymbolicGraph, cid: str) -> tuple[str, ...]:
    """Claims that say the same as `cid` (sameAs, sameClaims; symmetric)."""
    found = []
    for rel in (SAME_AS, SAME_CLAIMS):
        found += g.out(cid, rel) + g.into(cid, rel)
    return tuple(dict.fromkeys(c for c in found if c in g.claims))


def evidence_facts(g: SymbolicGraph, cid: str) -> list[str]:
    """Facts a claim points at directly (the extractor's derivedFrom may target a fact)."""
    return [t for t in g.out(cid, SUPPORTED_BY) + g.out(cid, DERIVED_FROM) if t in g.facts]


def parents(g: SymbolicGraph, cid: str) -> list[str]:
    """Claims `cid` derives from or restates."""
    derived = [t for t in g.out(cid, DERIVED_FROM) if t in g.claims]
    return list(dict.fromkeys(derived + list(restates(g, cid))))


def restating_group(g: SymbolicGraph, cid: str) -> frozenset[str]:
    """The claim and every claim it restates, directly or not: they share one support."""
    group, todo = {cid}, [cid]
    while todo:
        for r in restates(g, todo.pop()):
            if r not in group:
                group.add(r)
                todo.append(r)
    return frozenset(group)


@primitive
def unsupported(g: SymbolicGraph, cid: str) -> tuple[str, ...]:
    """The unsupported leaves of a claim's support chain (empty = supported).

    Claims that restate each other share their support: the group is backed if any of them has
    a fact. Otherwise follow the claims the group derives from. A group with no fact and nothing
    left to follow is a leaf, reported by its earliest claim. Claims that need no support are
    never leaves. Needed by: CE-1 missing evidence.
    """
    leaves: list[str] = []
    seen: set[str] = set()

    def walk(c: str) -> None:
        if c in seen:
            return
        group = restating_group(g, c)
        seen.update(group)
        if not needs_support(g.claims[c]) or any(evidence_facts(g, m) for m in group):
            return
        ps = {p for m in group for p in g.out(m, DERIVED_FROM) if p in g.claims} - group
        if not ps:
            leaves.append(min(group, key=g.time))
        for p in sorted(ps):
            walk(p)

    walk(cid)
    return tuple(leaves)


@primitive
def ancestry(g: SymbolicGraph, cid: str) -> frozenset[str]:
    """`cid` itself, plus the claims and facts it comes from. Needed by: CE-1 unreconciled."""
    seen = {cid}
    todo = [cid]
    while todo:
        c = todo.pop()
        for nxt in parents(g, c) + evidence_facts(g, c):
            if nxt not in seen:
                seen.add(nxt)
                if nxt in g.claims:
                    todo.append(nxt)
    return frozenset(seen)


def _under_final(g: SymbolicGraph, call_id: str | None) -> bool:
    while call_id in g.calls:
        if g.calls[call_id].final:
            return True
        call_id = g.calls[call_id].parent
    return False


@primitive
def final_claims(g: SymbolicGraph) -> tuple[str, ...]:
    """Claims of the final answer. Needed by: every CE-1 rule.

    Uses the call the extractor marked final. Until it does, falls back to the claims of the
    last call that made any.
    """
    marked = [c.id for c in g.claims.values() if _under_final(g, c.generated_by)]
    if marked or not g.claims:
        return tuple(marked)
    last = max(g.claims, key=g.time)
    return tuple(c.id for c in g.claims.values() if c.generated_by == g.claims[last].generated_by)


@primitive
def reaches_final(g: SymbolicGraph, cid: str) -> bool:
    """Is the claim, or one it restates, part of the final answer or what it derives from?
    Needed by: DR-2 dropped."""
    used = set(final_claims(g)).union(*(ancestry(g, f) for f in final_claims(g)))
    return bool(restating_group(g, cid) & used)


@primitive
def conflict_pairs(g: SymbolicGraph) -> tuple[tuple[str, str], ...]:
    """Claims in a `contradicts` relation, earlier first. Needed by: CE-1 unreconciled."""
    pairs = set()
    for a in g.claims:
        for b in g.out(a, CONTRADICTS) + g.into(a, CONTRADICTS):
            if b in g.claims and a != b:
                pairs.add(tuple(sorted((a, b), key=g.time)))
    return tuple(sorted(pairs, key=lambda p: (g.time(p[0]), g.time(p[1]))))


@primitive
def acknowledged(g: SymbolicGraph, pair: tuple[str, str]) -> bool:
    """Was the conflict taken into account in the claims themselves?

    Yes if the later claim derives from the earlier one, or a still later claim has both
    sides in its ancestry.
    """
    a, b = pair  # earlier first
    if a in ancestry(g, b):
        return True
    later = max(g.time(a), g.time(b))
    return any(
        c not in pair and g.time(c) > later and a in ancestry(g, c) and b in ancestry(g, c)
        for c in g.claims
    )


@primitive
def conflict_status(g: SymbolicGraph, pair: tuple[str, str]) -> str:
    """ "acknowledged", "seen" or "open".

    "seen": the later claim was written by a call that had the earlier one in its input, but
    nothing links them. Only the input is known, not whether the agent revised the claim or
    ignored it. "open": the later call never had the earlier claim.
    """
    if acknowledged(g, pair):
        return "acknowledged"
    call = g.calls.get(g.claims[pair[1]].generated_by)
    return "seen" if call and pair[0] in call.input_claims else "open"


@primitive
def correction_kind(g: SymbolicGraph, pair: tuple[str, str]) -> str:
    """Who has evidence in a pair (earlier, later): "correction" (only the later one),
    "override" (only the earlier one), "both" or "neither"."""
    early, late = (not unsupported(g, c) for c in pair)
    if early and late:
        return "both"
    return "correction" if late else "override" if early else "neither"


@primitive
def adopted_side(g: SymbolicGraph, pair: tuple[str, str]) -> str | None:
    """The side of a pair that the final claims rest on, if exactly one."""
    used = set().union(*(ancestry(g, f) for f in final_claims(g)))
    sides = [c for c in pair if c in used]
    return sides[0] if len(sides) == 1 else None


@primitive
def contradicting_facts(g: SymbolicGraph, cid: str) -> tuple[str, ...]:
    """Facts that contradict the claim. Needed by: CE-1 contradicted by evidence."""
    found = g.out(cid, CONTRADICTS) + g.into(cid, CONTRADICTS)
    return tuple(dict.fromkeys(f for f in found if f in g.facts))


# -- goals, tasks, constraints ---------------------------------------------------------------------


def contradict(g: SymbolicGraph, a: str, b: str) -> bool:
    """Is there a `contradicts` link between two elements, in either direction?"""
    return b in g.out(a, CONTRADICTS) or a in g.out(b, CONTRADICTS)


def agent_call_of(g: SymbolicGraph, call_id: str) -> str | None:
    """The agent call a call belongs to (itself, for an agent call)."""
    call = g.calls.get(call_id)
    return None if call is None else (call_id if call.kind == "agent" else call.parent)


@primitive
def claims_of(g: SymbolicGraph, agent_call: str) -> tuple[str, ...]:
    """The claims generated inside an agent call."""
    return tuple(c.id for c in g.claims.values() if agent_call_of(g, c.generated_by) == agent_call)


@primitive
def goal_of(g: SymbolicGraph, task_id: str) -> str | None:
    """The goal a task serves: the one it achieves, or else the one of the task it follows."""
    seen, todo = set(), [task_id]
    while todo:
        t = todo.pop(0)
        if t in seen:
            continue
        seen.add(t)
        if goals := [x for x in g.out(t, ACHIEVES) if x in g.goals]:
            return goals[0]
        todo += g.out(t, PRECEDED_BY)
    return None


@dataclass(frozen=True)
class Delegation:
    task: str
    delegator: (
        str | None
    )  # the agent call that routed the work (None if the extractor does not say)
    delegatee: str  # the agent call that received it
    goal: str | None
    constraints: tuple[str, ...]  # in force: bound to the task or to its goal


@primitive
def delegation(g: SymbolicGraph, task_id: str) -> Delegation | None:
    """Who handed which task to whom, and which constraints apply. Needed by: DC-4."""
    task = g.tasks[task_id]
    if task.standing or task.owned_by not in g.calls:
        return None
    goal = goal_of(g, task_id)
    bound = g.out(task_id, BOUNDED_BY) + (g.out(goal, BOUNDED_BY) if goal else [])
    return Delegation(
        task=task_id,
        delegator=g.calls[task.owned_by].routed_by,
        delegatee=task.owned_by,
        goal=goal,
        constraints=tuple(dict.fromkeys(k for k in bound if k in g.constraints)),
    )


@primitive
def overlapping_tasks(g: SymbolicGraph) -> tuple[tuple[str, ...], ...]:
    """Groups of identical tasks given to different agent calls. Needed by: CC-1 overlap."""
    assigned = [t.id for t in g.tasks.values() if not t.standing and t.owned_by]
    groups, seen = [], set()
    for start in assigned:
        if start in seen:
            continue
        group, todo = [], [start]
        while todo:  # the tasks connected to `start` by sameAs
            t = todo.pop()
            if t in seen or t not in assigned:
                continue
            seen.add(t)
            group.append(t)
            todo += g.out(t, SAME_AS) + g.into(t, SAME_AS)
        if len({g.tasks[t].owned_by for t in group}) > 1:
            groups.append(tuple(sorted(group)))
    return tuple(groups)


@primitive
def redundant_claims(g: SymbolicGraph) -> tuple[tuple[str, str], ...]:
    """Claims of two agents that say the same without either having seen the other.

    Neither is in the input of the other's call and neither derives from the other.
    Needed by: CC-1 overlap.
    """
    pairs = set()
    for a in g.claims:
        for b in restates(g, a):
            ca, cb = g.calls.get(g.claims[a].generated_by), g.calls.get(g.claims[b].generated_by)
            if not ca or not cb or not ca.agent or not cb.agent or ca.agent == cb.agent:
                continue
            if b in ca.input_claims or a in cb.input_claims:
                continue
            if b in g.out(a, DERIVED_FROM) or a in g.out(b, DERIVED_FROM):
                continue
            pairs.add(tuple(sorted((a, b))))
    return tuple(sorted(pairs))


@primitive
def dropped_constraints(g: SymbolicGraph, agent_call: str) -> tuple[tuple[str, str], ...]:
    """(constraint, llm call) where an agent call's constraint, in the input of its first LLM
    call, is missing from the input of a later one. Hard constraints first. Needed by: IR-1.
    """
    llms = sorted(
        (c for c in g.calls.values() if c.kind == "llm" and c.parent == agent_call),
        key=lambda c: c.start,
    )
    if len(llms) < 2:
        return ()
    held = [k for k in llms[0].input_constraints if agent_call in g.out(k, PROVIDED_TO)]
    held.sort(key=lambda k: g.constraints[k].strength != "hard" if k in g.constraints else True)
    return tuple((k, c.id) for c in llms[1:] for k in held if k not in c.input_constraints)


# -- capabilities ----------------------------------------------------------------------------------


def prompt_agent(g: SymbolicGraph, element) -> str | None:
    """The agent an element comes from: the one its system prompt (`definedIn`) belongs to."""
    prompt = g.system_prompts.get(element.system_prompt) if element.system_prompt else None
    return prompt.agent if prompt else None


@primitive
def task_agent(g: SymbolicGraph, task_id: str) -> str | None:
    """The agent a task was given to (a standing task: the agent of its system prompt)."""
    task = g.tasks[task_id]
    owner = g.calls.get(task.owned_by) if task.owned_by else None
    return (owner.agent if owner else None) or prompt_agent(g, task)


@primitive
def routers(g: SymbolicGraph) -> frozenset[str]:
    """Agents that hand work to at least two other agents: they route it, they are not asked to
    do it. (The extractor labels a worker giving control back as a handoff too, so one handoff
    does not make a router; a hub hands work to several.)"""
    sent_to: dict[str, set[str]] = {}
    for c in g.calls.values():
        sender = g.calls.get(c.routed_by) if c.routed_by else None
        if sender and sender.agent and c.agent and c.agent != sender.agent:
            sent_to.setdefault(sender.agent, set()).add(c.agent)
    return frozenset(agent for agent, targets in sent_to.items() if len(targets) >= 2)


@primitive
def capability_gap(g: SymbolicGraph) -> tuple[str, ...]:
    """Assigned tasks given to an agent none of whose capabilities lets it do them.

    Only tasks the extractor compared with the agent's capabilities count, and only when the agent
    has some. A router is left out: it delegates the work. Needed by: DC-4 CM, DE-1-MR.
    """
    has = {prompt_agent(g, c) for c in g.capabilities.values()} - {None} - routers(g)
    return tuple(
        t.id
        for t in g.tasks.values()
        if t.owned_by
        and not t.standing
        and t.capability_checked
        and task_agent(g, t.id) in has
        and not g.into(t.id, SUPPORTS)
    )


@primitive
def task_claims(g: SymbolicGraph, task_id: str) -> tuple[str, ...]:
    """The claims produced inside the agent call that owns the task."""
    return tuple(c for c in g.into(task_id, PRODUCED_BY) if c in g.claims)
