"""CR-1 (constraints in conflict) and IR-1 (a constraint dropped along the way).

Left out for now: CR-1 against the constraints of the user request. A constraint's `source`
("user", "system" or "agent") now says where it comes from, so it can be done.
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.models import CONTRADICTS
from failure_detection.primitives import dropped_constraints
from failure_detection.rules import rule


@rule("CR-1", "CR-1", "detect", on=("constraint", "edge:contradicts", "edge:providedTo"))
def conflicting_constraints(g, delta):
    def agents(k):
        return {g.calls[c].agent for c in g.out(k, "providedTo") if c in g.calls} - {None}

    pairs = set()
    for a in g.constraints:
        for b in g.out(a, CONTRADICTS) + g.into(a, CONTRADICTS):
            if b in g.constraints:
                pairs.add(tuple(sorted((a, b))))
    for a, b in sorted(pairs):
        if any(x != y for x in agents(a) for y in agents(b)):
            yield Hit(
                evidence=(a, b),
                message=f"Two constraints given to different agents contradict: "
                f"{g.constraints[a].text!r} and {g.constraints[b].text!r}",
            )


@rule("IR-1", "IR-1", "risk", on=("call", "constraint", "edge:providedTo"))
def constraint_dropped(g, delta):
    for call in g.calls.values():
        if call.kind != "agent":
            continue
        for k, llm in dropped_constraints(g, call.id):
            yield Hit(
                evidence=(k, llm),
                message=f"A constraint dropped from a later LLM call: {g.constraints[k].text!r}",
            )


@rule("IR-1", "IR-1", "detect", on=("claim", "call", "edge:contradicts"))
def constraint_violated_after_drop(g, delta):
    for call in g.calls.values():
        if call.kind != "agent":
            continue
        for k, llm in dropped_constraints(g, call.id):
            for c in g.claims.values():
                if c.generated_by == llm and (
                    k in g.out(c.id, CONTRADICTS) or c.id in g.out(k, CONTRADICTS)
                ):
                    yield Hit(
                        evidence=(c.id, k),
                        message=f"A claim breaks a constraint the call dropped: {c.text!r}",
                    )
