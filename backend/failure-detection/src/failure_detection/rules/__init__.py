"""Rules: one function each, registered with @rule. Add a rule by writing one in a module here."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from failure_detection.finding import Hit

RULES: list[Rule] = []


@dataclass(frozen=True)
class Rule:
    id: str
    failure: str  # taxonomy code, e.g. "CE-1"
    level: str  # "risk" (precondition open) or "detect" (consequence seen)
    on: frozenset[str]  # delta kinds that trigger it (see Delta.kinds); empty = every delta
    phase: str  # "live": on each delta. "final": once, when the session ends
    fn: Callable[..., Iterable[Hit]]


def rule(id: str, failure: str, level: str, on: tuple[str, ...] = (), phase: str = "live"):
    """Register `fn(graph, delta) -> iterable of Hit` as a rule."""

    def register(fn):
        RULES.append(Rule(id, failure, level, frozenset(on), phase, fn))
        return fn

    return register


from failure_detection.rules import (  # noqa: E402,F401
    capability,
    claims,
    conflicts,
    constraints,
    delegation,
    evidence,
    handoffs,
)
