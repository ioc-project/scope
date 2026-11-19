#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Initial/final State + Transition creation for an execution element."""

from __future__ import annotations

from typing import Literal

from scope_ontology.models.base import KGBase
from scope_ontology.models.edges import (
    correspondsToTransition,
    executesProcessing,
    hasFinalState,
    hasInitialState,
    hasProcessingCall,
    hasState,
    inputTo,
    leadsTo,
    representsExecution,
)
from scope_ontology.models.nodes import (
    AgentCall,
    LLMCall,
    Processing,
    ProcessingCall,
    Session,
    State,
    ToolCall,
    Transition,
)

from .fields import _hash_id
from .registry import MetadataGetter, Registry


def add_initial_state(
    registry: Registry, owner_id: str, session_id: str, span_id: str, input_text: str
) -> State:
    """Create owner_id's initial State (hasInitialState + hasState)."""
    state = registry.add(
        State(
            id=_hash_id("state", span_id, "initial", input_text),
            sessionId=session_id,
            content=input_text or "(no content recorded)",
        )
    )
    registry.add_edge(hasInitialState(source_id=owner_id, target_id=state.id))
    registry.add_edge(hasState(source_id=owner_id, target_id=state.id))
    return state


def add_final_state_transition(
    registry: Registry,
    owner_id: str,
    session_id: str,
    span_id: str,
    initial_state_id: str,
    input_text: str,
    output_text: str,
    mark_final: bool = True,
) -> State:
    """Create owner_id's final State (hasFinalState + hasState) and the Transition
    initial -> final, with correspondsToTransition/representsExecution to owner_id.

    With ``mark_final=False`` the State is only attached with hasState: it is a
    final-looking State that a later one has already superseded as owner_id's final."""
    final_state = registry.add(
        State(
            id=_hash_id("state", span_id, "final", output_text),
            sessionId=session_id,
            content=output_text or "(no content recorded)",
        )
    )
    if mark_final:
        registry.add_edge(hasFinalState(source_id=owner_id, target_id=final_state.id))
    registry.add_edge(hasState(source_id=owner_id, target_id=final_state.id))

    transition = registry.add(
        Transition(
            id=_hash_id("transition", span_id, input_text, output_text),
            sessionId=session_id,
        )
    )
    registry.add_edge(inputTo(source_id=initial_state_id, target_id=transition.id))
    registry.add_edge(leadsTo(source_id=transition.id, target_id=final_state.id))
    registry.add_edge(correspondsToTransition(source_id=owner_id, target_id=transition.id))
    registry.add_edge(representsExecution(source_id=transition.id, target_id=owner_id))
    return final_state


def add_state_pair_transition(
    registry: Registry,
    owner_id: str,
    session_id: str,
    span_id: str,
    input_text: str,
    output_text: str,
) -> None:
    """Create initial/final State nodes for owner_id and wire hasInitialState/
    hasFinalState/hasState, plus the Transition connecting them
    (State->Transition->State) and the correspondsToTransition/
    representsExecution pair back to owner_id -- every ExecutionElement needs
    one."""
    initial_state = add_initial_state(registry, owner_id, session_id, span_id, input_text)
    add_final_state_transition(
        registry, owner_id, session_id, span_id, initial_state.id, input_text, output_text
    )


def bridge_states_if_mismatched(
    registry: Registry,
    *,
    from_state_id: str | None,
    to_state_id: str | None,
    attach_to_id: str,
    session_id: str,
    start_time: float,
    end_time: float,
    bridge_kind: str,
    keep: Literal["from", "to"] = "from",
) -> None:
    """If the State at from_state_id's content is the same as the State at
    to_state_id's, merge them into one State (see ``keep``). If it differs,
    bridge them with a synthesized ProcessingCall
    attached to attach_to_id (via hasProcessingCall) whose own initial/final
    states *reuse* the two existing States, rather than asserting a false
    direct link between them.

    Callers resolve from_state_id/to_state_id however is right for their own
    case (e.g. a chain link resolves them via hasFinalState/hasInitialState
    on two different owners; aligning an AgentCall's own boundary against its
    first/last capability call resolves both via the same hasInitialState,
    or both via the same hasFinalState) -- this function only compares and
    bridges, it doesn't know which edge type either side came from.

    A no-op if either id is None or the State can't be found, if the content
    already matches (already continuous, nothing to assert), or if this
    exact pair was already bridged -- so callers can re-run this freely as
    new data arrives without creating duplicates.

    ``bridge_kind`` names the caller's use case (e.g. "handoff",
    "capability-chain") -- it's folded into the synthesized ids/names so
    different callers bridging the same pair for different reasons don't
    collide.
    """
    if from_state_id is None or to_state_id is None:
        return
    from_state = registry.get(State, from_state_id)
    to_state = registry.get(State, to_state_id)
    if from_state is None or to_state is None:
        return
    if from_state.content == to_state.content:
        if from_state_id != to_state_id:
            kept, dropped = (
                (from_state_id, to_state_id)
                if keep == "from"
                else (
                    to_state_id,
                    from_state_id,
                )
            )
            registry.merge_node_into(State, dropped, kept)
        return

    call_id = _hash_id("processingcall", bridge_kind, from_state_id, to_state_id)
    if registry.get(ProcessingCall, call_id) is not None:
        return  # already bridged

    processing = registry.add(
        Processing(id=f"processing:{bridge_kind}", name=bridge_kind.replace("-", "_"))
    )
    processing_call = registry.add(
        ProcessingCall(
            id=call_id,
            name=f"{bridge_kind} bridge ({from_state_id} -> {to_state_id})",
            sessionId=session_id,
            startTime=start_time,
            endTime=end_time,
            duration=max(0.0, (end_time - start_time) * 1000.0),
        )
    )
    registry.add_edge(executesProcessing(source_id=processing_call.id, target_id=processing.id))
    registry.add_edge(hasProcessingCall(source_id=attach_to_id, target_id=processing_call.id))

    registry.add_edge(hasInitialState(source_id=processing_call.id, target_id=from_state.id))
    registry.add_edge(hasState(source_id=processing_call.id, target_id=from_state.id))
    registry.add_edge(hasFinalState(source_id=processing_call.id, target_id=to_state.id))
    registry.add_edge(hasState(source_id=processing_call.id, target_id=to_state.id))

    transition_id = _hash_id("transition", bridge_kind, from_state.id, to_state.id)
    transition = registry.add(Transition(id=transition_id, sessionId=session_id))
    registry.add_edge(inputTo(source_id=from_state.id, target_id=transition.id))
    registry.add_edge(leadsTo(source_id=transition.id, target_id=to_state.id))
    registry.add_edge(
        correspondsToTransition(source_id=processing_call.id, target_id=transition.id)
    )
    registry.add_edge(representsExecution(source_id=transition.id, target_id=processing_call.id))


def adopt_boundary_states(
    registry: Registry,
    owner_id: str,
    session_id: str,
    initial_state_id: str,
    final_state_id: str,
) -> None:
    """Wire owner_id's own hasInitialState/hasFinalState/hasState directly at
    the given existing States -- reused, not copied or bridged.

    For pure container ExecutionElements (MASCall, Session) that have no
    content of their own, the ontology requires the boundary State to be
    *shared* with the child that actually starts/ends it (see
    mas-shapes-custom.ttl's SessionInitialStateSharedShape/
    SessionFinalStateSharedShape), unlike AgentCall's own boundary, which has
    real content of its own and so gets bridged (see
    bridge_states_if_mismatched) rather than replaced.

    Every ExecutionElement still needs its own Transition
    (correspondsToTransition/representsExecution) even when it shares States
    with another element -- only the States are reused, not the Transition.
    """
    registry.add_edge(hasInitialState(source_id=owner_id, target_id=initial_state_id))
    registry.add_edge(hasState(source_id=owner_id, target_id=initial_state_id))
    registry.add_edge(hasFinalState(source_id=owner_id, target_id=final_state_id))
    registry.add_edge(hasState(source_id=owner_id, target_id=final_state_id))

    transition_id = _hash_id(
        "transition", "container-boundary", owner_id, initial_state_id, final_state_id
    )
    transition = registry.add(Transition(id=transition_id, sessionId=session_id))
    registry.add_edge(inputTo(source_id=initial_state_id, target_id=transition.id))
    registry.add_edge(leadsTo(source_id=transition.id, target_id=final_state_id))
    registry.add_edge(correspondsToTransition(source_id=owner_id, target_id=transition.id))
    registry.add_edge(representsExecution(source_id=transition.id, target_id=owner_id))


def _clear_boundaries(registry: Registry, owner_id: str) -> None:
    boundaries = {
        target
        for edge, source, target in registry.edges
        if source == owner_id and edge in (hasInitialState, hasFinalState)
    }
    transitions = set(registry.edges_from(correspondsToTransition, owner_id))
    registry.remove_edges_where(
        lambda edge, source, target: (
            source == owner_id
            and (
                edge in (hasInitialState, hasFinalState, correspondsToTransition)
                or (edge is hasState and target in boundaries)
            )
        )
    )
    for transition_id in transitions:
        if not registry.edge_source(correspondsToTransition, transition_id):
            registry.remove_node(Transition, transition_id)
            registry.remove_edges_where(lambda _, source, target: transition_id in (source, target))


def _remember_trajectory(registry: Registry, call: KGBase, meta: MetadataGetter) -> None:
    meta(call)["trajectory"] = {
        "initial": registry.edge_target(hasInitialState, call.id),
        "final": registry.edge_target(hasFinalState, call.id),
        "transition": registry.edge_target(correspondsToTransition, call.id),
    }


def restore_trajectories(registry: Registry, meta: MetadataGetter) -> None:
    # Shared heuristics merge equal-content States. Recreate each call's canonical
    # boundaries first so a transient partial graph cannot determine their final ids.
    for cls in _CALLS:
        for call in registry.all_of(cls):
            trajectory = meta(call).get("trajectory")
            if not trajectory:
                continue
            initial = registry.get(State, registry.edge_target(hasInitialState, call.id) or "")
            final = registry.get(State, registry.edge_target(hasFinalState, call.id) or "")
            if initial is None:
                continue
            _clear_boundaries(registry, call.id)
            initial = registry.add(initial.model_copy(update={"id": trajectory["initial"]}))
            registry.add_edge(hasInitialState(source_id=call.id, target_id=initial.id))
            registry.add_edge(hasState(source_id=call.id, target_id=initial.id))
            if final is not None and trajectory["final"] and trajectory["transition"]:
                final = registry.add(final.model_copy(update={"id": trajectory["final"]}))
                registry.add_edge(hasFinalState(source_id=call.id, target_id=final.id))
                registry.add_edge(hasState(source_id=call.id, target_id=final.id))
                transition = registry.add(
                    Transition(
                        id=trajectory["transition"],
                        sessionId=call.sessionId,
                    )
                )
                for edge, source, target in (
                    (inputTo, initial.id, transition.id),
                    (leadsTo, transition.id, final.id),
                    (correspondsToTransition, call.id, transition.id),
                    (representsExecution, transition.id, call.id),
                ):
                    registry.add_edge(edge(source_id=source, target_id=target))


def prune_trajectories(registry: Registry, meta: MetadataGetter) -> None:
    # A replaced event trajectory must not leave disconnected States or Transitions behind.
    transitions = {target for edge, _, target in registry.edges if edge is correspondsToTransition}
    for node in registry.all_of(Transition):
        if node.id not in transitions:
            registry.remove_node(Transition, node.id)
            registry.remove_edges_where(lambda _, source, target: node.id in (source, target))
    referenced = {i for _, source, target in registry.edges for i in (source, target)}
    for cls in (State, Processing):
        for node in registry.all_of(cls):
            pending = isinstance(node, State) and (
                meta(node).get("receiverName") or meta(node).get("ownerSpanId")
            )
            if (
                node.id not in referenced
                and pending
                and registry.get(Session, node.sessionId) is not None
            ):
                registry.add_edge(hasState(source_id=node.sessionId, target_id=node.id))
            if node.id not in referenced and not pending:
                registry.remove_node(cls, node.id)


_CALLS = (AgentCall, LLMCall, ToolCall)
