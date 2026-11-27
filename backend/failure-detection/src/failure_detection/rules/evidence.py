"""CE-1: conclusions without evidence.

Each rule has a `risk` half (live: the precondition is open) and a `detect` half (final: it
reached the final answer).
"""

from __future__ import annotations

from failure_detection.finding import Hit
from failure_detection.primitives import (
    adopted_side,
    conflict_pairs,
    conflict_status,
    contradicting_facts,
    final_claims,
    unsupported,
)
from failure_detection.rules import rule

CLAIM_OR_LINK = ("claim", "edge:derivedFrom", "edge:supportedBy", "edge:contradicts")


# -- CE-1 missing evidence ---------------------------------------------------------------------


@rule("CE-1 missing evidence", "CE-1", "risk", on=("claim",))
def missing_evidence_risk(g, delta):
    """Live: an unsupported claim is a risk for the collective until it reaches the final answer.
    The agent-level finding of the same claim is IE-1."""
    for claim in delta.claims:
        if leaves := unsupported(g, claim.id):
            yield Hit(evidence=(claim.id, *leaves), message=f"No fact backs: {claim.text!r}")


@rule("CE-1 missing evidence", "CE-1", "detect", phase="final")
def missing_evidence(g, delta):
    for cid in final_claims(g):
        if leaves := unsupported(g, cid):
            yield Hit(
                evidence=(cid, *leaves),
                message=f"Final answer states without evidence: {g.claims[cid].text!r}",
            )


# -- CE-1 contradicted by evidence ---------------------------------------------------------------


@rule("CE-1 contradicted by evidence", "CE-1", "risk", on=("claim", "edge:contradicts"))
def contradicted_risk(g, delta):
    for cid in g.claims:
        if facts := contradicting_facts(g, cid):
            yield Hit(evidence=(cid, *facts), message=f"A fact contradicts: {g.claims[cid].text!r}")


@rule("CE-1 contradicted by evidence", "CE-1", "detect", phase="final")
def contradicted(g, delta):
    for cid in final_claims(g):
        if facts := contradicting_facts(g, cid):
            yield Hit(
                evidence=(cid, *facts),
                message=f"Final answer contradicted by a fact: {g.claims[cid].text!r}",
            )


# -- CE-1 unreconciled contradiction -------------------------------------------------------------


@rule("CE-1 unreconciled contradiction", "CE-1", "risk", on=CLAIM_OR_LINK)
def unreconciled_risk(g, delta):
    for pair in conflict_pairs(g):
        if conflict_status(g, pair) == "open":
            yield Hit(
                evidence=pair, message="Two claims contradict and no later claim reconciles them"
            )


@rule("CE-1 unreconciled contradiction", "CE-1", "detect", phase="final")
def unreconciled(g, delta):
    for pair in conflict_pairs(g):
        side = adopted_side(g, pair)
        if side and conflict_status(g, pair) == "open":
            other = pair[1] if side == pair[0] else pair[0]
            yield Hit(
                evidence=pair,
                message=f"Final answer rests on {g.claims[side].text!r}, "
                f"which contradicts {g.claims[other].text!r}, never reconciled",
            )
