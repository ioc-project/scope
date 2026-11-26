"""The extraction output read as a graph: KG nodes, the symbolic state elements anchored to them, and edges.

The output JSON (``ExtractionResult``) has ``nodes`` (each KG node with its ``symbolic_state_elements``) and
``edges`` (``edge_type``, ``source_id``, ``target_id``: the relations between element ids, or ids of KG nodes
such as an AgentCall). ``SessionGraph`` indexes both so the page is built from them: a relation is read with
``out`` / ``into`` instead of from an attribute of each element.

Standard library only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Element:
    """A symbolic state element (a claim, task, goal, ...) and the KG node it is anchored to."""

    id: str
    kind: str  # key of ``symbolic_state_elements``: "claims", "tasks", "goals", ...
    node: dict[str, Any]  # the annotated KG node holding it
    data: dict[str, Any]  # the element as in the output

    @property
    def node_id(self) -> str | None:
        return self.node["node"].get("id")


class SessionGraph:
    """Nodes, elements and edges of one extraction output.

    ``out(type, id)`` is the ids ``id`` points to through edges of that type, ``into(type, id)`` the ids that
    point to it; both in the order the edges were recorded, without repeats. A relation recorded in both
    directions (``same_as``, ``conflicts_with``) is in both."""

    def __init__(self, output: dict[str, Any]) -> None:
        if "edges" not in output:
            raise ValueError("the output has no edges: extract the session again to get them")
        self.nodes: list[dict[str, Any]] = output.get("nodes", [])
        self.edges: list[dict[str, Any]] = output["edges"]
        #: True when the extraction had a trajectory (the agent calls could be ordered).
        self.has_trajectory = output.get("symbolic_state") is not None
        self._elements: dict[str, Element] = {}
        self._kinds: dict[str, list[Element]] = defaultdict(list)
        for node in self.nodes:
            for kind, items in node.get("symbolic_state_elements", {}).items():
                for data in items:
                    element = Element(data["id"], kind, node, data)
                    self._elements[element.id] = element
                    self._kinds[kind].append(element)
        self._out: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._in: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._attributes: dict[tuple[str, str, str], dict[str, Any]] = {}
        for edge in self.edges:
            kind, source, target = edge["edge_type"], edge["source_id"], edge["target_id"]
            self._attributes[(kind, source, target)] = edge.get("attributes") or {}
            if target not in self._out[(kind, source)]:
                self._out[(kind, source)].append(target)
            if source not in self._in[(kind, target)]:
                self._in[(kind, target)].append(source)

    def nodes_of_type(self, node_type: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n.get("node_type") == node_type]

    def elements(self, kind: str) -> list[Element]:
        """The elements of that kind, in the order of the nodes."""
        return self._kinds.get(kind, [])

    def element(self, element_id: str) -> Element | None:
        return self._elements.get(element_id)

    def out(self, edge_type: str, source_id: str) -> list[str]:
        return list(self._out.get((edge_type, source_id), []))

    def into(self, edge_type: str, target_id: str) -> list[str]:
        return list(self._in.get((edge_type, target_id), []))

    def attributes(self, edge_type: str, source_id: str, target_id: str) -> dict[str, Any]:
        """The attributes of one edge (empty when it has none or does not exist)."""
        return dict(self._attributes.get((edge_type, source_id, target_id), {}))

    def kind_of(self, element_id: str) -> str | None:
        """The kind ("claims", "facts", ...) of an element id, None when it is not an element."""
        element = self._elements.get(element_id)
        return element.kind if element else None

    def has_edges(self, edge_type: str) -> bool:
        return any(e["edge_type"] == edge_type for e in self.edges)
