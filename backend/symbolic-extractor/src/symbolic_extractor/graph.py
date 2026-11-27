"""An extraction result as the nodes and edges of the neurosymbolic layer: symbolic state elements as nodes, linked to the original nodes of the execution KG.

Two steps:

* ``to_graph(result)`` turns an ``ExtractionResult`` into labelled nodes and typed edges, named as in the
  ontology (``mas:claimText`` -> ``claimText``, ``mas:supportedBy`` -> ``supportedBy``, ...).
* ``Graph.node_rows()`` / ``Graph.edge_rows()`` give them as plain dicts, ready to be written.

Nothing here talks to the KG: the caller hands ``Graph.node_rows()`` and ``Graph.edge_rows()`` to whatever writes
them (in SCOPE, ``scope.client.neurosymbolic.write_symbolic_state``).

What is written
---------------
Only what ``neurosymbolic-ontology.ttl`` describes, under its names; nothing else is created.

* Labels: ``Claim``, ``Fact``, ``Goal``, ``Task``, ``Constraint``, ``AgentCapability``, ``SystemPrompt``.
  Properties: the ontology's data properties (``claimText``, ``systemPromptText``, ...), validated by the
  generated models of ``scope_ontology`` (``sessionId``, to clean a session, is added by the writer).
* System prompts: a ``SystemPrompt`` is linked to its ``Agent`` (``systemPromptOf``, the Agent node is matched
  by name). The standing tasks (written as ``Task``), capabilities and system constraints read from it are linked
  to it (``definedIn``) and carry ``source = 'system'``; user constraints carry ``source = 'user'``. So
  ``MATCH (n {source: 'system'})`` finds everything that comes from system prompts.
* Delegation claims are written as ordinary ``Claim`` nodes (the extractor uses them internally to link claims).
* Relations: those of ``ONTOLOGY_EDGES``, checked against the label of each end. Anything else the extractor
  records (``has_capability``, ``call_preceded_by``, ``reply_claim``, ``same_claim``, ...) is **not written**
  and is counted in ``Graph.skipped``. So are the synthetic ``Conversation`` node and ``goalOf``:
  their target is a ``MASCall`` of the KG, which the extraction does not carry.
* The original nodes (``Agent``, ``AgentCall``, ``LLMCall``, ``ToolCall``) are never created or changed: they are
  only matched by id. An edge towards one that is not in the KG is not written and is reported.

Ids are deterministic, so writing the same result twice changes nothing. Extraction is not deterministic (a new
run can word claims differently, so their ids differ): the writer should replace the session's earlier symbolic
state, not add to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from scope_ontology.models import edges as ontology_edges
from scope_ontology.models import nodes as ontology_nodes

from symbolic_extractor.models import ExtractionResult

#: Element key (``symbolic_state_elements``) -> KG label, for the elements the ontology has a class for.
ELEMENT_LABELS = {
    "claims": "Claim",
    "facts": "Fact",
    "goals": "Goal",
    "tasks": "Task",
    "constraints": "Constraint",
    "capabilities": "AgentCapability",
    "system_tasks": "Task",  # a standing task: a Task with source 'system'
    "system_prompts": "SystemPrompt",
    "delegation_claims": "Claim",
}
#: Elements read from a system prompt: ``source`` of the nodes of that kind.
SOURCES = {"system_tasks": "system", "capabilities": "system"}
#: ``origin`` of a Constraint -> ``source``.
CONSTRAINT_SOURCES = {"system": "system", "user": "user", "agent": "agent"}
#: Labels of the original KG nodes (``node_type`` of the extractor's nodes): only ever matched.
KG_LABELS = {"Agent", "AgentCall", "LLMCall", "ToolCall"}

#: Element field -> property, for the data properties of the ontology.
PROPERTIES = {
    "claim_text": "claimText",
    "claim_force": "claimForce",
    "claim_kind": "claimKind",
    "verbatim_source": "verbatimSource",
    "fact_text": "factText",
    "fact_source_type": "factSourceType",
    "goal_text": "goalText",
    "success_criteria": "successCriteria",
    "rationale": "rationale",
    "status": "status",
    "task_text": "taskText",
    "acceptance_criteria": "acceptanceCriteria",
    "constraint_text": "constraintText",
    "constraint_strength": "constraintStrength",
    "capability_text": "agentCapabilityText",
    "system_prompt": "systemPromptText",
}

_CLAIM, _FACT, _GOAL, _TASK = {"Claim"}, {"Fact"}, {"Goal"}, {"Task"}
#: Store relation -> (ontology name, labels allowed at the source, labels allowed at the target).
ONTOLOGY_EDGES: dict[str, tuple[str, set[str], set[str]]] = {
    "generated_by": ("generatedBy", _CLAIM, {"LLMCall"}),
    "grounded_in": ("groundedIn", _FACT, {"ToolCall"}),
    "owned_by": ("ownedBy", _TASK, {"AgentCall"}),
    "announces": ("announces", _CLAIM, {"ToolCall"}),
    # Claim -> Fact, Fact -> Fact, Fact -> Claim. The label check is per end, so a Claim -> Claim would pass
    # here too; the extraction never makes one (the ontology shapes forbid it).
    "supported_by": ("supportedBy", _CLAIM | _FACT, _FACT | _CLAIM),
    "derived_from": ("derivedFrom", _CLAIM, _CLAIM),
    "conflicts_with": ("conflictsWith", _CLAIM, _CLAIM),
    "same_as": ("sameAs", _CLAIM | _TASK, _CLAIM | _TASK),
    "produced_by": ("producedBy", _CLAIM, _TASK),
    "answers": ("answers", _CLAIM, _GOAL),
    "fulfils": ("fulfils", _CLAIM, _GOAL | _TASK),
    "fulfils_task": ("fulfils", _CLAIM, _TASK),
    "achieves": ("achieves", _TASK, _GOAL),
    "implements": ("implements", _TASK, _TASK | _GOAL),
    "preceded_by": ("precededBy", _TASK, _TASK),
    "bounded_by": ("boundedBy", _GOAL | _TASK, {"Constraint"}),
    "supports": ("supports", {"AgentCapability"}, _TASK),
    "defined_in": ("definedIn", {"Task", "AgentCapability", "Constraint"}, {"SystemPrompt"}),
    "system_prompt_of": ("systemPromptOf", {"SystemPrompt"}, {"Agent"}),
    "input_claim": ("inputClaim", {"LLMCall"}, _CLAIM),
    "input_fact": ("inputFact", {"LLMCall"}, _FACT),
    "in_context": ("inContext", _FACT, _CLAIM),
}
#: Element fields that are pointers to original nodes: written as the edge of the same name.
#: Element field -> relation, for the pointer fields.
_POINTERS = {
    "generated_by": "generated_by",
    "grounded_in": "grounded_in",
    "owned_by": "owned_by",
    "system_prompt_id": "defined_in",
}


@dataclass(frozen=True)
class GraphNode:
    label: str
    id: str
    props: dict[str, Any]


@dataclass(frozen=True)
class GraphEdge:
    type: str
    from_id: str
    from_label: str
    to_id: str
    to_label: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Graph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    #: Edges dropped because an end is not a node of the result: ``(relation, source id, target id)``.
    dangling: list[tuple[str, str, str]] = field(default_factory=list)
    #: Relations the ontology does not describe (or between labels it does not allow), counted: not written.
    skipped: dict[str, int] = field(default_factory=dict)

    def node_rows(self) -> list[dict[str, Any]]:
        """The nodes as plain dicts: ``{"label", "id", "props"}``."""
        return [{"label": n.label, "id": n.id, "props": n.props} for n in self.nodes]

    def edge_rows(self) -> list[dict[str, Any]]:
        """The edges as plain dicts: ``{"type", "from_id", "from_label", "to_id", "to_label", "props"}``."""
        return [
            {
                "type": e.type,
                "from_id": e.from_id,
                "from_label": e.from_label,
                "to_id": e.to_id,
                "to_label": e.to_label,
                "props": e.props,
            }
            for e in self.edges
        ]


def camel(name: str) -> str:
    """``claim_text`` -> ``claimText`` (the property and relationship names of the ontology)."""
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _scalar(value: Any) -> Any:
    """A Neo4j-safe property value; maps and nested lists are stored as JSON text."""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _props(element: dict[str, Any], label: str, kind: str) -> dict[str, Any]:
    """The properties of an element, as the ontology's node model of ``label`` has them (validated by it).

    Only what the extraction set is kept (no empty defaults): ``claimKind`` must be absent on a committing
    claim, for instance. ``sessionId`` is not a property of the ontology; the writer adds it.
    """
    props: dict[str, Any] = {}
    for key, name in PROPERTIES.items():
        if element.get(key) is not None:
            props[name] = _scalar(element[key])
    source = (
        CONSTRAINT_SOURCES.get(element.get("origin"))
        if kind == "constraints"
        else SOURCES.get(kind)
    )
    if source:
        props["source"] = source
    model = getattr(ontology_nodes, label)(id=element["id"], **props)
    return model.model_dump(exclude_unset=True, exclude={"id"})


def to_graph(result: ExtractionResult) -> Graph:
    """The nodes and edges to write for ``result`` (nothing touches the KG here)."""
    graph = Graph()
    labels: dict[str, str] = {}  # id -> label, for every node an edge can end at
    left_out: set[str] = set()  # ids of elements the ontology has no class for
    relations: list[tuple[str, str, str]] = [
        (e.edge_type, e.source_id, e.target_id) for e in result.edges
    ]

    # ``nodes`` + ``updated_nodes``: the latter are the earlier objects after a late change.
    for annotated in [*result.nodes, *result.updated_nodes]:
        node_id = annotated.node["id"]
        if annotated.node_type not in KG_LABELS:
            left_out.add(node_id)  # e.g. the synthetic Conversation
        if annotated.node_type in KG_LABELS:
            labels[node_id] = annotated.node_type  # already in the KG: matched, not written
        for kind, elements in annotated.symbolic_state_elements.items():
            label = ELEMENT_LABELS.get(kind)
            if label is None:
                left_out.update(e["id"] for e in elements)  # no class in the ontology: not written
                continue
            for element in elements:
                if element["id"] not in labels:
                    labels[element["id"]] = label
                    graph.nodes.append(
                        GraphNode(label, element["id"], _props(element, label, kind))
                    )
                relations += [
                    (r, element["id"], element[f]) for f, r in _POINTERS.items() if element.get(f)
                ]
                if kind == "system_prompts" and element.get("agent_id"):
                    labels[element["agent_id"]] = "Agent"  # matched by name, not written
                    relations.append(("system_prompt_of", element["id"], element["agent_id"]))

    seen: set[tuple[str, str, str]] = set()
    for relation, source, target in relations:
        if (relation, source, target) in seen:
            continue
        seen.add((relation, source, target))
        spec = ONTOLOGY_EDGES.get(relation)
        if spec is None:
            graph.skipped[relation] = graph.skipped.get(relation, 0) + 1
            continue
        name, sources, targets = spec
        if {source, target} & left_out:
            graph.skipped[relation] = graph.skipped.get(relation, 0) + 1
            continue
        if source not in labels or target not in labels:
            graph.dangling.append((relation, source, target))  # an end the extraction does not have
            continue
        if labels[source] not in sources or labels[target] not in targets:
            graph.skipped[relation] = graph.skipped.get(relation, 0) + 1
            continue
        getattr(ontology_edges, name)(
            source_id=source, target_id=target
        )  # an edge the ontology has
        graph.edges.append(GraphEdge(name, source, labels[source], target, labels[target]))
    return graph
