#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Retain graph metadata and declare only matching observed agents.

This is the one authoritative source of "declared" structural data, so it
always overwrites (``registry.upsert``) rather than fills-in-if-absent --
every other handler only ever creates the same MAS/Agent ids as undeclared
placeholders (``registry.add``, keep-first), so whichever one of those ran
first never blocks this handler's richer data from landing.
"""

from __future__ import annotations

from typing import Any

from scope_ontology.models.base import KGBase
from scope_ontology.models.edges import belongsToMAS
from scope_ontology.models.nodes import MAS, Agent, StructuralElement

from ..fields import get_span_application_id, parse_json_object
from ..registry import Registry


def _declare(
    registry: Registry,
    cls: type[KGBase],
    node_id: str,
    *,
    name: str = "",
    description: str = "",
) -> KGBase:
    """Set the declared (graph-sourced) version of a structural node.

    Keeps an existing declared record as-is (e.g. two "*.graph" spans for the
    same app) rather than reconstructing it, but overwrites any undeclared
    placeholder another handler already created for this id.
    """
    existing: StructuralElement = registry.get(cls, node_id)
    if existing is not None and existing.declared:  # type: ignore[attr-defined]
        return existing
    return registry.upsert(
        cls.model_validate(
            {
                "id": node_id,
                "name": name or node_id,
                "description": description,
                "declared": True,
            }
        )
    )


def handle_graph(
    span: dict[str, Any],
    span_attrs: dict[str, Any],
    registry: Registry,
) -> None:
    application_id = get_span_application_id(span, span_attrs)
    if not application_id:
        return
    name = str(span_attrs.get("ioa_observe.entity.name") or application_id)
    description = str(span_attrs.get("ioa_observe.entity.description") or "")
    _declare(registry, MAS, application_id, name=name, description=description)

    graph_raw = span_attrs.get("gen_ai.ioa.graph")
    if not graph_raw:
        return
    graph_data = parse_json_object(graph_raw, "gen_ai.ioa.graph")
    graph_nodes = graph_data.get("nodes", {})
    if not isinstance(graph_nodes, dict):
        raise ValueError("gen_ai.ioa.graph.nodes must be an object")
    mas = registry.get(MAS, application_id)
    if mas is not None and graph_data:
        registry.metadata_for(mas)["graphNodes"] = graph_nodes


def reconcile_graph_agents(registry: Registry) -> None:
    """Graph nodes describe execution, not roles; require runtime agent evidence."""
    for mas in registry.all_of(MAS):
        graph_nodes = registry.metadata_for(mas).get("graphNodes", {})
        for agent in registry.all_of(Agent):
            if mas.id not in registry.edges_from(belongsToMAS, agent.id):
                continue
            aliases = [agent.id, agent.name, *registry.metadata_for(agent).get("aliases", [])]
            if any(
                key in aliases or str(value.get("name") or key) in aliases
                for key, value in graph_nodes.items()
                if isinstance(value, dict)
            ):
                registry.upsert(agent.model_copy(update={"declared": True}))
