"""Rules about what travels between agent calls: DC-1 (context diluted) and DR-2 (dropped)."""

from __future__ import annotations

import re

from failure_detection.finding import Hit
from failure_detection.flow import (
    dropped_at,
    first_llm,
    handoffs,
    new_facts,
    restatement_ratio,
)
from failure_detection.primitives import (
    acknowledged,
    conflict_pairs,
    needs_support,
    reaches_final,
    restates,
)
from failure_detection.rules import rule

RESTATEMENT_THRESHOLD = 0.5  # share of restating claims above which a handoff adds nothing
MIN_RUN = 2  # how many successive handoffs like that make DC-1


@rule("DC-1 restating", "DC-1", "detect", phase="final")
def only_restating(g, delta):
    flagged = [
        (
            t.to_call,
            (restatement_ratio(g, t) or 0) > RESTATEMENT_THRESHOLD and not new_facts(g, t.to_call),
        )
        for t in handoffs(g)
    ]
    run = []
    for call, bad in flagged + [(None, False)]:  # the closing entry ends the last run
        if bad:
            run.append(call)
            continue
        unresolved = [p for p in conflict_pairs(g) if not acknowledged(g, p)]
        if len(run) >= MIN_RUN and unresolved:
            yield Hit(
                evidence=tuple(run),
                message=f"{len(run)} successive handoffs only restated claims, no new fact, and "
                f"{len(unresolved)} conflict(s) were never reconciled",
            )
        run = []


@rule("DC-1 multicast", "DC-1", "detect", on=("call", "claim", "transition"))
def multicast_dropped(g, delta):
    by_sender = {}
    for t in handoffs(g):
        by_sender.setdefault(t.from_call, []).append(t.to_call)
    for targets in by_sender.values():
        if len(targets) < 2:
            continue
        for c in g.claims:
            holders = [t for t in targets if c in g.calls[t].state_claims]
            for t in holders:
                first = first_llm(g, t)
                if len(holders) >= 2 and first and c not in first.input_claims:
                    yield Hit(
                        evidence=(c, t),
                        message=f"A claim sent to several agents did not reach one of them: "
                        f"{g.claims[c].text!r}",
                    )


@rule("DR-2 dropped", "DR-2", "detect", phase="final")
def dropped_at_handoff(g, delta):
    for t in handoffs(g):
        for c in dropped_at(g, t):
            if c in g.claims and needs_support(g.claims[c]) and not reaches_final(g, c):
                yield Hit(
                    evidence=(c, t.to_call),
                    message=f"Sender claim not passed on: {g.claims[c].text!r}",
                )


def numbers(text: str) -> list[tuple[float, int]]:
    """The numbers in a text, as (value, number of decimals)."""
    tokens = re.findall(r"\d+(?:[.,]\d+)?", text)
    return [
        (float(t.replace(",", ".")), len(re.split(r"[.,]", t)[1]) if re.search(r"[.,]", t) else 0)
        for t in tokens
    ]


def rounded(before: str, after: str) -> list[float]:
    """Numbers of `before` that come back in `after` with fewer decimals, and not the same value."""
    kept = numbers(after)
    return [
        x
        for x, decimals in numbers(before)
        if any(y != x and abs(y - x) < 1 and fewer < decimals for y, fewer in kept)
    ]


@rule("DR-2 precision", "DR-2", "detect", on=("claim", "edge:sameAs", "edge:sameClaims"))
def precision_lost(g, delta):
    for a in g.claims.values():
        for b in restates(g, a.id):
            if g.time(b) > g.time(a.id) and (lost := rounded(a.text, g.claims[b].text)):
                yield Hit(
                    evidence=(a.id, b),
                    message=f"Restating rounded {lost}: {a.text!r} -> {g.claims[b].text!r}",
                )
