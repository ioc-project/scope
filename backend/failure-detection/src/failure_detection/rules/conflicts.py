"""Rules about claims that contradict each other, or contradict what an agent relies on.

The `risk` of the conflict family (a pair that no later claim takes into account) is the one of
CE-1 unreconciled contradiction, so it is not repeated here. Left out: the accumulated form of
CC-4 (a count over the session, not a finding).
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.flow import (
    agents_holding,
    architecture,
    no_new_facts,
    same_query,
    stale,
)
from failure_detection.models import CONTRADICTS, DERIVED_FROM
from failure_detection.primitives import (
    adopted_side,
    agent_of,
    ancestry,
    conflict_pairs,
    conflict_status,
    correction_kind,
    facts_in,
    final_claims,
    is_commitment,
    is_coordinative,
    unsupported,
)
from failure_detection.rules import rule

CHANGED = ("claim", "edge:contradicts", "edge:derivedFrom", "transition")


def open_pairs(g):
    """The conflicts the later agent never had in front of it, and nothing reconciles."""
    return [p for p in conflict_pairs(g) if conflict_status(g, p) == "open"]


def seen_pairs(g, *kinds):
    """The conflicts the later call had in front of it, with nothing linking them, by kind."""
    return [
        p
        for p in conflict_pairs(g)
        if conflict_status(g, p) == "seen" and correction_kind(g, p) in kinds
    ]


@rule("CE-1 corrected by evidence", "CE-1", "risk", on=CHANGED)
def corrected_by_evidence(g, delta):
    for a, b in seen_pairs(g, "correction"):
        yield Hit(
            evidence=(a, b),
            message=f"{g.claims[b].text!r} replaces {g.claims[a].text!r}, which has no evidence",
        )


@rule("CE-1 sources disagree", "CE-1", "risk", on=CHANGED)
def sources_disagree(g, delta):
    for a, b in seen_pairs(g, "both"):
        yield Hit(
            evidence=(a, b),
            message=f"{g.claims[b].text!r} follows {g.claims[a].text!r}, and both rest on "
            "evidence: sources disagree, or the agent revised",
        )


@rule("CE-1 revised without evidence", "CE-1", "risk", on=CHANGED)
def revised_without_evidence(g, delta):
    for a, b in seen_pairs(g, "neither"):
        yield Hit(
            evidence=(a, b),
            message=f"{g.claims[b].text!r} follows {g.claims[a].text!r}, and neither rests on "
            "evidence: a revision, or ignored (the graph cannot tell)",
        )


@rule("CE-1 overrode evidence", "CE-1", "detect", on=CHANGED)
def overrode_evidence(g, delta):
    for a, b in seen_pairs(g, "override"):
        yield Hit(
            evidence=(a, b),
            message=f"{g.claims[b].text!r} replaces {g.claims[a].text!r}, which rests on "
            "evidence, with a claim that has none",
        )


def adopted_pairs(g):
    """(pair, adopted, dropped) for the open conflicts where the final answer took a side."""
    for pair in open_pairs(g):
        if side := adopted_side(g, pair):
            yield pair, side, pair[1] if side == pair[0] else pair[0]


def adopters(g, side):
    """The agents whose final claims rest on a claim."""
    return {agent_of(g, f) for f in final_claims(g) if side in ancestry(g, f)} - {None}


@rule("CC-3", "CC-3", "detect", phase="final")
def hub_picked_a_side(g, delta):
    arch = architecture(g)
    if arch.kind != "hub":
        return
    for (a, b), side, dropped in adopted_pairs(g):
        workers = {agent_of(g, a), agent_of(g, b)} - {arch.hub, None}
        hub_claims = [c for c in g.claims if agent_of(g, c) == arch.hub]
        if len(workers) == 2 and any(side in ancestry(g, c) for c in hub_claims):
            yield Hit(
                evidence=(a, b),
                message=f"The hub took {g.claims[side].text!r} over {g.claims[dropped].text!r} "
                "without reconciling them",
            )


@rule("DC-3", "DC-3", "detect", phase="final")
def peers_never_agreed(g, delta):
    if architecture(g).kind == "hub":
        return
    for a, b in open_pairs(g):
        sides = {agent_of(g, a): b, agent_of(g, b): a}  # an agent -> the claim of the other side
        settled = any(
            is_commitment(c)
            and agent_of(g, c.id) in sides
            and sides[agent_of(g, c.id)] in ancestry(g, c.id)
            for c in g.claims.values()
            if c.id not in (a, b)
        )
        if not settled:
            yield Hit(
                evidence=(a, b),
                message=f"Peers disagree and neither committed to a reconciliation: "
                f"{g.claims[a].text!r} / {g.claims[b].text!r}",
            )


@rule("DE-1-CM", "DE-1-CM", "detect", phase="final")
def weaker_answer_adopted(g, delta):
    arch = architecture(g)
    for (a, b), side, dropped in adopted_pairs(g):
        if not same_query(g, a, b) or arch.hub in adopters(g, side):
            continue
        weaker = (unsupported(g, side) and not unsupported(g, dropped)) or facts_in(
            g, ancestry(g, side)
        ) < facts_in(g, ancestry(g, dropped))
        if weaker:
            yield Hit(
                evidence=(a, b),
                message=f"The final answer took the less supported answer {g.claims[side].text!r} "
                f"over {g.claims[dropped].text!r}",
            )


@rule("CS-1 dissent", "CS-1", "detect", phase="final")
def dissent_dropped(g, delta):
    for _, side, dropped in adopted_pairs(g):
        if len(agents_holding(g, dropped)) < len(agents_holding(g, side)) and not g.into(
            dropped, DERIVED_FROM
        ):
            yield Hit(
                evidence=(side, dropped),
                message=f"The minority view was dropped without being evaluated: "
                f"{g.claims[dropped].text!r}",
            )


@rule("CC-4", "CC-4", "detect", on=("claim", "call", "edge:contradicts"))
def missed_update(g, delta):
    for call in g.calls.values():
        if call.kind != "llm":
            continue
        for b, a in stale(g, call.id):
            yield Hit(
                evidence=(b, a),
                message=f"{g.claims[b].text!r}, from an earlier call of another agent, "
                f"contradicts {g.claims[a].text!r}, which a later call of {call.agent} used "
                "without receiving it",
            )


@rule("IC-2", "IC-2", "detect", on=("claim", "call", "fact", "edge:contradicts"))
def plan_kept_despite_contradiction(g, delta):
    for call in g.calls.values():
        if call.kind != "llm" or not call.agent:
            continue
        for b in (*call.input_claims, *call.input_facts):
            for a in g.out(b, CONTRADICTS) + g.into(b, CONTRADICTS):
                if a not in g.claims or agent_of(g, a) != call.agent:
                    continue
                plan = [  # commitments of the agent, made before this call, that rest on `a`
                    c
                    for c in g.claims.values()
                    if c.id != a
                    and is_coordinative(c)
                    and agent_of(g, c.id) == call.agent
                    and g.calls[c.generated_by].end <= call.start
                    and a in ancestry(g, c.id)
                ]
                later = [
                    c.id
                    for c in g.claims.values()
                    if c.id != a
                    and agent_of(g, c.id) == call.agent
                    and g.calls[c.generated_by].start >= call.start
                    and a in ancestry(g, c.id)
                ]
                if plan and later:
                    yield Hit(
                        evidence=(b, a, later[0]),
                        message=f"The agent kept relying on {g.claims[a].text!r} after it received "
                        "what contradicts it",
                    )


@rule("IC-1", "IC-1", "detect", on=("claim", "call", "edge:contradicts"))
def contradicts_its_commitment(g, delta):
    for k, c in conflict_pairs(g):
        who = agent_of(g, k)
        if not is_commitment(g.claims[k]) or who is None or who != agent_of(g, c):
            continue
        before, after = g.claims[k].generated_by, g.claims[c].generated_by
        if before in g.calls and after in g.calls and no_new_facts(g, before, after):
            yield Hit(
                evidence=(k, c),
                message=f"The agent contradicted its own commitment with nothing new: "
                f"{g.claims[k].text!r}",
            )
