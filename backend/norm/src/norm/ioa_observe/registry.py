#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Generic (type, id)-keyed store for KG nodes and edges.

No notion of structural vs. execution, declared vs. undeclared, etc. --
that policy lives with whoever calls this. ``add`` (keep-first) and
``upsert`` (overwrite) are enough for callers to implement it themselves,
e.g. handlers/graph.py's declared-vs-undeclared merge for MAS/Agent/Tool.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from scope_ontology.models.base import KGBase

T = TypeVar("T", bound=KGBase)


class Registry:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], KGBase] = {}
        self.edges: dict[tuple[type, str, str], KGBase] = {}

    def get(self, cls: type[T], node_id: str) -> T | None:
        return cast("T | None", self.nodes.get((cls.__name__, node_id)))

    def add(self, node: T) -> T:
        """Add a node, deduped by (type, id) -- keeps the first one seen."""
        key = (type(node).__name__, node.id)
        return cast(T, self.nodes.setdefault(key, node))

    def add_if_not_exists(self, node: T) -> tuple[T, bool]:
        """Return the existing (type, id) node, or add ``node`` if absent.

        The second value says whether ``node`` was the one just added, so
        callers can gate one-time side effects (e.g. edges) on it instead of
        re-deriving "is this new?" themselves.
        """
        existing = self.get(type(node), node.id)
        if existing is not None:
            return existing, False
        return self.add(node), True

    def upsert(self, node: KGBase) -> KGBase:
        """Add or replace a node by (type, id)."""
        key = (type(node).__name__, node.id)
        self.nodes[key] = node
        return node

    def add_edge(self, edge: KGBase) -> None:
        key = (type(edge), edge.source_id, edge.target_id)
        self.edges.setdefault(key, edge)

    def find(self, cls: type[T], predicate: Callable[[T], bool]) -> T | None:
        """Return the first node of this class satisfying predicate."""
        for (type_name, _node_id), node in self.nodes.items():
            if type_name == cls.__name__ and predicate(cast(T, node)):
                return cast(T, node)
        return None

    def edge_target(self, edge_cls: type[KGBase], source_id: str) -> str | None:
        """Return the target_id of the first edge_cls edge out of source_id."""
        for etype, sid, tid in self.edges:
            if etype is edge_cls and sid == source_id:
                return tid
        return None

    def edge_source(self, edge_cls: type[KGBase], target_id: str) -> str | None:
        """Return the source_id of the first edge_cls edge into target_id."""
        for etype, sid, tid in self.edges:
            if etype is edge_cls and tid == target_id:
                return sid
        return None

    def edges_from(self, edge_cls: type[KGBase], source_id: str) -> list[str]:
        """Return every target_id of edge_cls edges out of source_id."""
        return [tid for etype, sid, tid in self.edges if etype is edge_cls and sid == source_id]

    def merge_node_into(self, cls: type[KGBase], drop_id: str, keep_id: str) -> None:
        """Replace the ``cls`` node ``drop_id`` by ``keep_id``.

        Every edge that touched ``drop_id`` now touches ``keep_id`` (an edge that
        becomes a duplicate of an existing one is dropped), and ``drop_id`` is removed.
        """
        if drop_id == keep_id:
            return
        for key, edge in list(self.edges.items()):
            etype, source_id, target_id = key
            if source_id != drop_id and target_id != drop_id:
                continue
            del self.edges[key]
            new_source = keep_id if source_id == drop_id else source_id
            new_target = keep_id if target_id == drop_id else target_id
            moved = edge.model_copy(update={"source_id": new_source, "target_id": new_target})
            self.edges.setdefault((etype, new_source, new_target), moved)
        self.nodes.pop((cls.__name__, drop_id), None)

    def all_of(self, cls: type[T]) -> list[T]:
        """Return every node of this class."""
        return [
            cast(T, node)
            for (type_name, _node_id), node in self.nodes.items()
            if type_name == cls.__name__
        ]

    def all_items(self) -> list[KGBase]:
        return [*self.nodes.values(), *self.edges.values()]
