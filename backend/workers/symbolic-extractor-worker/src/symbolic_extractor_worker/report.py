from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WriteReport:
    """What a write did. ``missing_*`` is what was *not* written, so nothing is lost silently."""

    session_id: str
    nodes: dict[str, int] = field(default_factory=dict)  # label -> written
    edges: dict[str, int] = field(default_factory=dict)  # relationship type -> written
    missing_edges: dict[str, int] = field(default_factory=dict)  # relationship type -> not written
    missing_nodes: dict[str, list[str]] = field(
        default_factory=dict
    )  # KG label -> ids not in the KG
    dangling: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # edges whose end the extraction lacks
    skipped: dict[str, int] = field(
        default_factory=dict
    )  # not in the ontology: left out on purpose
    deleted: int = 0

    @classmethod
    def from_write(cls, session_id: str, written: dict[str, Any], graph: Any) -> WriteReport:
        """The report of ``scope.client.neurosymbolic.write_symbolic_state`` (``written``) for ``graph``."""
        return cls(
            session_id=session_id,
            nodes=written["nodes"],
            edges=written["edges"],
            missing_edges=written["missing_edges"],
            missing_nodes=written["missing_nodes"],
            dangling=graph.dangling,
            skipped=graph.skipped,
            deleted=written["deleted"],
        )

    @property
    def complete(self) -> bool:
        return not self.missing_edges and not self.missing_nodes and not self.dangling

    def summary(self) -> str:
        parts = [
            f"{sum(self.nodes.values())} nodes ({', '.join(f'{k} {v}' for k, v in sorted(self.nodes.items()))})",
            f"{sum(self.edges.values())} edges",
        ]
        if self.deleted:
            parts.append(f"{self.deleted} replaced")
        if self.skipped:
            parts.append(f"{sum(self.skipped.values())} links outside the ontology left out")
        if not self.complete:
            missing = sum(self.missing_edges.values()) + len(self.dangling)
            parts.append(
                f"{missing} edges not written, {sum(map(len, self.missing_nodes.values()))} KG nodes missing"
            )
        return "; ".join(parts)
