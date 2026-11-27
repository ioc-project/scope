"""CC-1 (overlap, gap) and DC-4 (delegation): who was given what.

Left out for now: CC-1 gap `detect` and DC-4 SS (they need links from tasks to the goal's
success criteria). DC-4 CM and DE-1-MR, which need the capabilities of the agents, are in
`capability.py`.
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.models import ACHIEVES, PRECEDED_BY, PROVIDED_TO
from failure_detection.primitives import (
    claims_of,
    contradict,
    delegation,
    overlapping_tasks,
    redundant_claims,
)
from failure_detection.rules import rule

# -- CC-1 overlap ----------------------------------------------------------------------------------


@rule("CC-1 overlap", "CC-1", "risk", on=("task", "edge:sameAs"))
def overlap_risk(g, delta):
    for group in overlapping_tasks(g):
        yield Hit(
            evidence=group,
            message=f"Same work given to {len(group)} agent calls: {g.tasks[group[0]].text!r}",
        )


@rule("CC-1 overlap", "CC-1", "detect", on=("claim", "call", "edge:sameAs", "edge:sameClaims"))
def overlap(g, delta):
    for a, b in redundant_claims(g):
        yield Hit(
            evidence=(a, b),
            message=f"Two agents made the same claim independently: {g.claims[a].text!r}",
        )


# -- CC-1 gap --------------------------------------------------------------------------------------


@rule("CC-1 gap", "CC-1", "risk", phase="final")
def gap(g, delta):
    for goal in g.goals:
        if not g.into(goal, ACHIEVES):
            yield Hit(
                evidence=(goal,), message=f"No task achieves the goal: {g.goals[goal].text!r}"
            )
    for t in g.tasks.values():
        if not t.standing and not g.out(t.id, PRECEDED_BY) and not g.out(t.id, ACHIEVES):
            yield Hit(evidence=(t.id,), message=f"A first task serves no goal: {t.text!r}")


# -- DC-4 delegation -------------------------------------------------------------------------------


@rule("DC-4 AC", "DC-4", "risk", phase="final")
def no_acceptance_criteria(g, delta):
    """One finding per session: nearly every task lacks criteria, so one per task says nothing."""
    bare = [t for t in g.tasks.values() if not t.standing and not t.acceptance_criteria]
    if bare:
        yield Hit(
            evidence=tuple(t.id for t in bare),
            message=f"{len(bare)} of {len(g.tasks)} tasks have no acceptance criteria",
        )


_DELEGATION_CHANGED = ("task", "constraint", "call", "edge:providedTo", "edge:boundedBy")


@rule("DC-4 CO", "DC-4", "risk", on=_DELEGATION_CHANGED)
def constraint_not_passed_on(g, delta):
    for tid in g.tasks:
        d = delegation(g, tid)
        if not d or not d.delegator:
            continue
        for k in d.constraints:
            if g.constraints[k].strength != "hard":
                continue
            received = g.out(k, PROVIDED_TO)
            if d.delegator in received and d.delegatee not in received:
                yield Hit(
                    evidence=(tid, k, d.delegatee),
                    message=f"A hard constraint did not reach the agent: {g.constraints[k].text!r}",
                )


@rule("DC-4 CO", "DC-4", "detect", on=("claim", "constraint", "edge:contradicts", "edge:boundedBy"))
def constraint_violated(g, delta):
    for tid in g.tasks:
        d = delegation(g, tid)
        if not d:
            continue
        for k in d.constraints:
            if g.constraints[k].strength != "hard":
                continue
            for c in claims_of(g, d.delegatee):
                if contradict(g, c, k):
                    yield Hit(
                        evidence=(c, k),
                        message=f"A claim breaks a hard constraint: {g.claims[c].text!r}",
                    )
