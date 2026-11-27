from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Hit(BaseModel):
    """What a rule yields: the elements involved and a message. The engine adds the rest."""

    evidence: tuple[str, ...]
    message: str


class Finding(Hit):
    rule: str
    failure: str
    level: Literal["risk", "detect"]  # risk: precondition open (warning); detect: consequence seen
    agents: tuple[str, ...] = ()  # agents of the claims in `evidence`, in order, no repeats
    session_id: str = ""

    @property
    def key(self) -> tuple:
        """Identity, so the same finding is never reported twice."""
        return (self.rule, self.level, self.evidence)
