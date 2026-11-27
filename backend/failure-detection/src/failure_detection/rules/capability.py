"""DC-4 CM and DE-1-MR: work given to an agent that cannot do it.

They use the capabilities of the agents and the `supports` edges the extractor judged (one
request per task, against the capabilities of the agent that owns it). A `supports` edge says a
capability lets the agent do the task, or part of it. So "no capability supports the task" is a
sure mismatch; a task only partly covered is not seen here.
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.primitives import (
    capability_gap,
    overlapping_tasks,
    routers,
    task_agent,
    task_claims,
    unsupported,
)
from failure_detection.rules import rule

CHANGED = ("task", "capability", "edge:supports")


def names(g, tasks) -> str:
    return ", ".join(sorted({task_agent(g, t) or "?" for t in tasks}))


@rule("DC-4 CM", "DC-4", "risk", on=CHANGED)
def no_capability_risk(g, delta):
    for tid in capability_gap(g):
        yield Hit(
            evidence=(tid,),
            message=f"{task_agent(g, tid)} has no capability for: {g.tasks[tid].text!r}",
        )


@rule("DC-4 CM", "DC-4", "detect", phase="final")
def no_capability_answered(g, delta):
    """The agent could not do the task and still made claims nothing backs: it answered from
    generation."""
    for tid in capability_gap(g):
        bare = [c for c in task_claims(g, tid) if unsupported(g, c)]
        if bare:
            yield Hit(
                evidence=(tid, *bare),
                message=f"{task_agent(g, tid)} has no capability for {g.tasks[tid].text!r} "
                f"and made {len(bare)} claim(s) with no evidence",
            )


@rule("DE-1-MR", "DE-1", "detect", phase="final")
def misrouted(g, delta):
    """Identical work given to several agents: one that cannot do it, another that can."""
    gap = set(capability_gap(g))
    for group in overlapping_tasks(g):
        stuck = [t for t in group if t in gap]
        able = [  # a router that "can" coordinate is not another agent who could do the work
            t
            for t in group
            if g.tasks[t].capability_checked and t not in gap and task_agent(g, t) not in routers(g)
        ]
        if stuck and able:
            yield Hit(
                evidence=(*stuck, *able),
                message=f"{g.tasks[group[0]].text!r} went to {names(g, stuck)}, which cannot do "
                f"it, while {names(g, able)} can",
            )
