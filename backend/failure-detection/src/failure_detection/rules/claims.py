"""Rules about how a claim changes along its chain: CS-1 (sycophancy), DC-2 and IE-1.

Certainty and assertiveness levels are placeholders until the claim force values are fixed:
claims with a force not in these tables are skipped.
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.flow import after_tool_failure, no_new_facts
from failure_detection.models import DERIVED_FROM
from failure_detection.primitives import (
    agent_of,
    ancestry,
    contradict,
    evidence_facts,
    facts_in,
    is_coordinative,
    restates,
    unsupported,
)
from failure_detection.rules import rule

CERTAINTY = {"hedged": 0, "neutral": 1, "certain": 2}  # epistemic claims
ASSERTIVENESS = {"suggestion": 0, "directive": 1, "committing": 1, "order": 2}  # coordinative


@rule("CS-1 herding", "CS-1", "detect", on=("claim", "call", "edge:sameAs", "edge:sameClaims"))
def herding(g, delta):
    for x in g.claims:
        copies = [
            c
            for c in restates(g, x)
            if g.time(c) > g.time(x)
            and agent_of(g, c) not in (None, agent_of(g, x))
            and not evidence_facts(g, c)
            and no_new_facts(g, g.claims[x].generated_by, g.claims[c].generated_by)
        ]
        if len({agent_of(g, c) for c in copies}) >= 2:
            yield Hit(
                evidence=(x, *sorted(copies)),
                message=f"Several agents repeated a claim without evidence of their own: "
                f"{g.claims[x].text!r}",
            )


@rule("CS-1 revision", "CS-1", "detect", on=("claim", "call", "edge:contradicts"))
def sycophantic_revision(g, delta):
    for a in g.claims:
        who, first = agent_of(g, a), g.claims[a].generated_by
        if who is None:
            continue
        for call in g.calls.values():
            if call.kind != "llm" or call.agent != who or call.start < g.calls[first].end:
                continue
            for b in call.input_claims:
                if (
                    b not in g.claims
                    or not contradict(g, a, b)
                    or not no_new_facts(g, first, call.id)
                ):
                    continue
                for c in g.claims.values():
                    if c.generated_by == call.id and b in restates(g, c.id):
                        yield Hit(
                            evidence=(a, b, c.id),
                            message=f"The agent dropped {g.claims[a].text!r} for "
                            f"{g.claims[b].text!r} with nothing new",
                        )


@rule("CS-1 certainty", "CS-1", "detect", on=("claim", "edge:derivedFrom"))
def certainty_escalation(g, delta):
    for c in g.claims.values():
        for p in g.out(c.id, DERIVED_FROM):
            if p not in g.claims or is_coordinative(c) or is_coordinative(g.claims[p]):
                continue
            up = CERTAINTY.get(c.force, -1) > CERTAINTY.get(g.claims[p].force, 99)
            if up and facts_in(g, ancestry(g, c.id)) <= facts_in(g, ancestry(g, p)):
                yield Hit(
                    evidence=(p, c.id),
                    message=f"Certainty rose with no new fact: {g.claims[p].text!r} -> {c.text!r}",
                )


@rule("DC-2", "DC-2", "detect", on=("claim", "edge:derivedFrom"))
def force_escalation(g, delta):
    for c in g.claims.values():
        for p in g.out(c.id, DERIVED_FROM):
            if p not in g.claims or not is_coordinative(g.claims[p]):
                continue
            turned_fact = not is_coordinative(c) and not evidence_facts(g, c.id)  # no fact backs it
            harder = ASSERTIVENESS.get(c.force, -1) > ASSERTIVENESS.get(g.claims[p].force, 99)
            if turned_fact or harder:
                yield Hit(
                    evidence=(p, c.id),
                    message=f"A proposal hardened: {g.claims[p].text!r} -> {c.text!r}",
                )


@rule("IE-1 missing evidence", "IE-1", "detect", phase="final")
def claim_without_evidence(g, delta):
    """The agent asserts a claim nothing backs. The CE-1 risk is the same claim seen by the
    collective. A claim stated as certain says so in the message.
    Final, not live: a support link can arrive after the claim, and a detect is not retracted."""
    for c in g.claims.values():
        if leaves := unsupported(g, c.id):
            how = "as certain " if c.force == "certain" else ""
            yield Hit(
                evidence=(c.id, *leaves),
                message=f"Stated {how}with no evidence: {c.text!r}",
            )


@rule("IE-1 after failure", "IE-1", "detect", on=("claim", "call"))
def claim_after_tool_failure(g, delta):
    for call in g.calls.values():
        if call.kind == "agent":
            for c in after_tool_failure(g, call.id):
                yield Hit(
                    evidence=(c,),
                    message=f"Claimed without evidence after a tool failed: {g.claims[c].text!r}",
                )
