"""Write an extraction result to the KG: the symbolic state elements as nodes linked to the original nodes.

Two steps, so the first can be tested without a database:

* ``to_graph(result)`` turns an ``ExtractionResult`` into labelled nodes and typed edges, named as in the
  ontology (``mas:claimText`` -> ``claimText``, ``mas:supportedBy`` -> ``supportedBy``, ...).
* ``KGWriter(db).write(result)`` MERGEs them with the Cypher of ``kg_queries``.

Written to move into scope: ``kg_queries`` becomes a query builder module, ``KGWriter`` a client method that
takes the ``Connector``. Both only need the two methods of ``GraphDB`` (``execute`` and ``execute_command``).

What is written
---------------
Only what ``neurosymbolic-ontology.ttl`` describes, under its names; nothing else is created.

* Labels: ``Claim``, ``Fact``, ``Goal``, ``Task``, ``Constraint``, ``AgentCapability``, ``SystemPrompt``.
  Properties: the ontology's data properties (``claimText``, ``systemPromptText``, ...) plus ``sessionId`` (to
  clean a session).
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

Idempotent: ids are deterministic, so writing the same result twice changes nothing. Extraction is not
deterministic (a new run can word claims differently, so their ids differ): pass ``replace=True`` to delete
the session's symbolic state before writing the new one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from symbolic_extractor import kg_queries as q
from symbolic_extractor.models import ExtractionResult

logger = logging.getLogger(__name__)

BATCH_SIZE = 200

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


class GraphDB(Protocol):
    """The two methods of scope's ``Connector`` that the writer uses."""

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> Any: ...

    def execute_command(self, command: str, params: dict[str, Any] | None = None) -> None: ...


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
    dangling: list[tuple[str, str, str]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # not in the ontology: left out on purpose
    deleted: int = 0

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


def _props(element: dict[str, Any], session_id: str, kind: str) -> dict[str, Any]:
    props: dict[str, Any] = {"sessionId": session_id}
    for key, name in PROPERTIES.items():
        if element.get(key) is not None:
            props[name] = _scalar(element[key])
    source = CONSTRAINT_SOURCES.get(element.get("origin")) if kind == "constraints" else SOURCES.get(kind)
    if source:
        props["source"] = source
    return props


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
                    graph.nodes.append(GraphNode(label, element["id"], _props(element, result.session_id, kind)))
                relations += [(r, element["id"], element[f]) for f, r in _POINTERS.items() if element.get(f)]
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
        graph.edges.append(GraphEdge(name, source, labels[source], target, labels[target]))
    return graph


def _chunks(rows: list[Any], size: int = BATCH_SIZE) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _written(rows: Any) -> int:
    return int(rows[0]["written"]) if rows else 0


class KGWriter:
    """Writes extraction results with a scope ``Connector`` (or anything with ``execute``/``execute_command``)."""

    def __init__(self, db: GraphDB, batch_size: int = BATCH_SIZE) -> None:
        self._db = db
        self._batch_size = batch_size
        self._indexed: set[str] = set()

    def write(self, result: ExtractionResult, *, replace: bool = False) -> WriteReport:
        graph = to_graph(result)
        report = WriteReport(session_id=result.session_id, dangling=graph.dangling, skipped=graph.skipped)
        if replace:
            report.deleted = self._clean(result.session_id)
        by_label: dict[str, list[dict[str, Any]]] = {}
        for node in graph.nodes:
            by_label.setdefault(node.label, []).append({"id": node.id, "props": node.props})
        for label, rows in by_label.items():
            self._ensure_index(label)
            for batch in _chunks(rows, self._batch_size):
                query, params = q.merge_nodes_query(label, batch)
                report.nodes[label] = report.nodes.get(label, 0) + _written(
                    self._db.execute(query, params)
                )

        missing = self._missing_kg_nodes(graph)
        report.missing_nodes = missing
        by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for edge in graph.edges:
            by_key.setdefault((edge.type, edge.from_label, edge.to_label), []).append(
                {"from_id": edge.from_id, "to_id": edge.to_id, "props": edge.props}
            )
        for (edge_type, from_label, to_label), rows in by_key.items():
            written = 0
            for batch in _chunks(rows, self._batch_size):
                query, params = q.merge_edges_query(edge_type, from_label, to_label, batch)
                written += _written(self._db.execute(query, params))
            report.edges[edge_type] = report.edges.get(edge_type, 0) + written
            if written < len(rows):
                report.missing_edges[edge_type] = (
                    report.missing_edges.get(edge_type, 0) + len(rows) - written
                )
        if not report.complete:
            logger.warning("Session %s written with gaps: %s", result.session_id, report.summary())
        return report

    def _ensure_index(self, label: str) -> None:
        if label in self._indexed:
            return
        self._db.execute_command(q.ensure_index_query(label))
        self._indexed.add(label)

    def _clean(self, session_id: str) -> int:
        labels = sorted(ELEMENT_LABELS.values())
        query, params = q.clean_session_query(labels, session_id)
        rows = self._db.execute(query, params)
        return int(rows[0]["deleted"]) if rows else 0

    def _missing_kg_nodes(self, graph: Graph) -> dict[str, list[str]]:
        """Ids the edges point at, among the original KG nodes, that the KG does not have."""
        wanted: dict[str, set[str]] = {}
        for edge in graph.edges:
            for node_id, label in ((edge.from_id, edge.from_label), (edge.to_id, edge.to_label)):
                if label in KG_LABELS:
                    wanted.setdefault(label, set()).add(node_id)
        missing: dict[str, list[str]] = {}
        for label, ids in wanted.items():
            found: set[str] = set()
            for batch in _chunks(sorted(ids), self._batch_size):
                query, params = q.existing_ids_query(label, batch)
                found |= {row["id"] for row in self._db.execute(query, params) or []}
            if ids - found:
                missing[label] = sorted(ids - found)
        return missing


def writer_from_env() -> KGWriter:
    """A writer on the SCOPE API's shared Neo4j connector (same settings as ``KGSessionReader.from_env``)."""
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    from scope.dependencies import get_neo4j_connector

    return KGWriter(get_neo4j_connector())
