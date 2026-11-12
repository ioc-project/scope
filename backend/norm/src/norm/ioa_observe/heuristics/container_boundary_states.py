#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Give MASCall and Session their own initial/final State by sharing (not
bridging) with the first/last child that actually starts/ends them.

Runs last, after chain_capability_calls, since it needs every AgentCall's
own boundary State edges in their final, possibly-bridged form. MASCall is
resolved before Session so that Session's own adoption can build on
MASCall's already-adopted boundary.
"""

from __future__ import annotations

from scope_ontology.models.edges import (
    hasAgentCall,
    hasFinalState,
    hasInitialState,
    hasMASCall,
)
from scope_ontology.models.nodes import AgentCall, MASCall, Session

from ..registry import Registry
from ..trajectory import _clear_boundaries, adopt_boundary_states


def assign_container_boundary_states(registry: Registry) -> None:
    for cls, child_cls, edge in (
        (MASCall, AgentCall, hasAgentCall),
        (Session, MASCall, hasMASCall),
    ):
        for container in registry.all_of(cls):
            children = [
                registry.get(child_cls, node_id)
                for node_id in registry.edges_from(edge, container.id)
            ]
            children = [child for child in children if child is not None]
            if not children:
                continue
            first = min(children, key=lambda node: (node.startTime, node.id))
            last = max(children, key=lambda node: (node.endTime, node.id))
            initial = registry.edge_target(hasInitialState, first.id)
            final = registry.edge_target(hasFinalState, last.id)
            if initial and final:
                _clear_boundaries(registry, container.id)
                adopt_boundary_states(registry, container.id, container.sessionId, initial, final)


def update_container_times(registry: Registry) -> None:
    meta = registry.metadata_for
    for cls, child_cls, edge in (
        (MASCall, AgentCall, hasAgentCall),
        (Session, MASCall, hasMASCall),
    ):
        for container in registry.all_of(cls):
            children = [registry.get(child_cls, i) for i in registry.edges_from(edge, container.id)]
            children = [c for c in children if c is not None]
            if children:
                start = min(container.startTime, *(c.startTime for c in children))
                end = max(container.endTime, *(c.endTime for c in children))
                registry.upsert(
                    container.model_copy(
                        update={
                            "startTime": start,
                            "endTime": end,
                            "duration": (end - start) * 1000,
                            **(
                                {"spanId": meta(container).get("sessionSpanId", "")}
                                if cls is Session
                                else {}
                            ),
                        }
                    )
                )
