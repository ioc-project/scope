"""Top-level entry point of the symbolic extraction pipeline."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from symbolic_extractor.comparison import (
    DEFAULT_SAME_CLAIM_THRESHOLD,
    DEFAULT_SIMILARITY_THRESHOLD,
    EmbeddingComparator,
    annotate_same_claims,
)
from symbolic_extractor.embeddings import (
    EmbeddingSet,
    TextEmbedder,
    embed_nodes,
    embed_result,
)
from symbolic_extractor.extractors import Extractor
from symbolic_extractor.extractors.delegation_claims import (
    Caller,
    delegated_reply,
    reply_claims,
    reply_receiver,
    source_claims,
)
from symbolic_extractor.extractors.tasks import same_task_key
from symbolic_extractor.models import (
    AnnotatedNode,
    ExtractionRequest,
    ExtractionResult,
    KGNode,
    SessionData,
    SessionTrajectory,
    SymbolicStateElement,
)
from symbolic_extractor.relations.argument_fact_links import ArgumentFactLinker
from symbolic_extractor.relations.argument_origin_relations import (
    ArgumentOriginEvaluation,
    ArgumentOriginExtractor,
)
from symbolic_extractor.relations.claim_claim_relations import (
    SAME_AS,
    ClaimRelation,
    ClaimRelationExtractor,
)
from symbolic_extractor.relations.fact_claim_links import SUPPORTED_BY, FactClaimLinker
from symbolic_extractor.relations.goal_claim_relations import (
    FULFILS,
    GoalClaimRelationExtractor,
    GoalEvaluation,
)
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.relations.task_capability_relations import (
    SUPPORTS,
    CapabilityEvaluation,
    CapabilityTaskRelationExtractor,
)
from symbolic_extractor.relations.task_claim_relations import (
    FULFILS_TASK,
    TaskClaimRelationExtractor,
    TaskEvaluation,
)
from symbolic_extractor.relations.task_relations import (
    ACHIEVES,
    IMPLEMENTS,
    TaskRelationExtractor,
)
from symbolic_extractor.store import (
    ANNOUNCES,
    ANSWERS,
    BOUNDED_BY,
    CALL_PRECEDED_BY,
    CALL_RETURNED_FROM,
    HAS_CAPABILITY,
    HAS_SYSTEM_TASK,
    IN_CONTEXT,
    PRECEDED_BY,
    PRODUCED_BY,
    REPLY_CLAIM,
    Edge,
    StoredElement,
    SymbolicSessionStore,
)
from symbolic_extractor.symbolic_state import SymbolicStateTrace, track_symbolic_state
from symbolic_extractor.tool_calls import match_tool_calls
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)


def _annotations(node: AnnotatedNode) -> tuple:
    """A copy of what ``SymbolicExtractor.annotate`` may change on ``node``, to tell whether it did."""
    return deepcopy(
        (
            node.symbolic_state_elements,
            node.input_claims,
            node.input_facts,
            node.system_tasks,
            node.capabilities,
            node.source_claims,
        )
    )


class SymbolicExtractor:
    """Pipeline: fetch session from KG -> run each element extractor per node.

    Two ways to use it:

    * per session, ``process`` / ``run``: the whole session is available. Every call starts from
      a new store, so the extractor can be reused for several sessions.
    * live, ``live_process``: the session is still running and each call gets one KG node, as it
      appears. The store is kept between the calls (``self.store``) so the claims and facts of
      the nodes already seen are available to track the input of the next ones.

    The result is a plain ``ExtractionResult``; persisting it (local JSON now, KG later)
    is the caller's concern, see ``symbolic_extractor.output``.
    """

    def __init__(
        self,
        extractors: Sequence[Extractor] = (),
        embedder: TextEmbedder | None = None,
        same_claim_threshold: float = DEFAULT_SAME_CLAIM_THRESHOLD,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        relation_extractors: Sequence[SemanticRelationExtractor] = (),
        match_input_facts: bool = True,
    ) -> None:
        """With an ``embedder`` (and a trajectory), each claim is compared to the input claims of
        its LLM call and gets ``same_claims``: those whose probability of being the same
        claim is above ``same_claim_threshold``. ``similarity_threshold`` is the embedding
        similarity at which that probability is 0.5 (see ``EmbeddingComparator``): lower it to
        accept looser paraphrases. ``live_process`` computes ``same_claims`` too, one node at a time.

        ``relation_extractors`` (for example a ``FactClaimLinker``) are run by ``process`` once
        the whole session is extracted, and by ``live_process`` at the end of each node, on the
        claims of the node and the facts in their context. They record their relations in the
        store (the LLM requests they make are added to the usage).

        A ``GoalClaimRelationExtractor`` is not run per node: once the claims of the final answer
        are extracted (see ``_link_answers``) it judges whether they satisfy the goal, see
        ``_evaluate_goals``. A ``TaskClaimRelationExtractor`` runs per node, like the others, on the
        claims of an LLM call and the tasks of its agent call (``produced_by``), see
        ``_judge_node_tasks``; it needs the AgentCall node to be processed before its LLM calls.

        ``match_input_facts`` (default True): an LLM call's input facts are those, among the facts of
        the tool calls of its agent call finished before it started, whose text occurs in its input.
        With False that textual check is skipped and all of those facts are input facts.
        """
        self._match_input_facts = match_input_facts
        self._extractors = list(extractors)
        self._relation_extractors = list(relation_extractors)
        self._embedder = embedder
        self._same_claim_threshold = same_claim_threshold
        self._similarity_threshold = similarity_threshold
        #: Embeddings computed by the last ``process`` call, or accumulated by ``live_process``
        #: over the session so far (None when not computed).
        self.embeddings: EmbeddingSet | None = None
        #: Elements and relations of the session being processed (None before the first call).
        #: ``process`` / ``run`` replace it at each call; ``live_process`` keeps it, and replaces
        #: it only when a node of another session arrives.
        self.store: SymbolicSessionStore | None = None
        #: Trajectory used by the last ``process`` / ``live_process`` call (call -> owning AgentCall), if any.
        self.trajectory: SessionTrajectory | None = None
        #: Verdicts of the ``GoalClaimRelationExtractor`` on the session being processed: whether the
        #: claims of the final answer satisfy each goal (empty until the final answer is extracted).
        self.goal_evaluations: list[GoalEvaluation] = []
        # goal id -> ids of the answer claims already judged, so a goal is not judged twice
        self._evaluated: dict[str, tuple[str, ...]] = {}
        #: Verdicts of the ``TaskClaimRelationExtractor``: which claims of an LLM call fulfil each task
        #: they were produced by (one per LLM call and task).
        self.task_evaluations: list[TaskEvaluation] = []
        #: Verdicts of the ``CapabilityTaskRelationExtractor``: which capabilities of its agent support each
        #: task and system task (one per task, judged once).
        self.capability_evaluations: list[CapabilityEvaluation] = []
        # ids of the tasks and system tasks already submitted to the CapabilityTaskRelationExtractor
        self._capability_judged: set[str] = set()
        #: Verdicts of the ``ArgumentOriginExtractor``: which claims of its agent call the arguments of each
        #: tool call come from (one per tool call, judged once).
        self.argument_evaluations: list[ArgumentOriginEvaluation] = []
        # live mode: node id -> the node returned for it, so a late relation can be put on it (see ``_refresh``)
        self._returned: dict[str, AnnotatedNode] = {}

    def live_process(
        self,
        node: KGNode,
        request: ExtractionRequest,
        trajectory: SessionTrajectory | None = None,
    ) -> ExtractionResult:
        """Process one node of a session in progress, as it appears in the KG.

        The extractors run on ``node`` only, its elements are added to ``self.store`` (which
        keeps the ones of the nodes processed before), and, as in ``process``, the claims and facts
        present in the input of the node are tracked (``input_claims`` / ``input_facts``, and the
        ``input_claim`` / ``input_fact`` relations of the store).

        With an embedder, the claims of the node are embedded and added to ``self.embeddings``
        (which accumulates the embeddings of the session, one call after the other), then compared
        to the input claims of the node: ``same_claims`` and the ``same_claim`` relations are
        computed for this node only, like ``process`` does for all of them. Without a trajectory
        the claims are still embedded but not compared.

        The tracking needs the ``trajectory`` of the session so far (it must hold the step of
        ``node``, the AgentCall owning it, and the earlier calls); without a trajectory the node gets no
        ``input_claims``.

        The result has the annotated node only (``nodes``), the usage of this call, and the
        ``symbolic_state`` computed over the trajectory with the store as it is now. A node of
        another session than the last one starts a new store and new embeddings.

        ``edges`` of the result are the relations recorded while processing this node. They can join
        elements of nodes returned earlier (the capabilities of an agent are read from its first LLM
        call, so its first task is linked to them one node after it was returned). Those nodes are kept
        (``self._returned``) and the ones touched by the new edges are annotated again from the store,
        in place, right away: no need to wait for the end of the session. ``updated_nodes`` lists the
        ones whose annotations changed, so a consumer that already read them knows to read them again.
        Annotating is a plain read of the store (no LLM request), done only for the nodes the new edges
        touch, so its cost follows the edges of the step and not the size of the session.
        """
        session_id = request.session_id
        if self.store is None or self.store.session_id != session_id:
            self.store = SymbolicSessionStore(session_id)
            self.goal_evaluations, self._evaluated = [], {}
            self.task_evaluations = []
            self.capability_evaluations, self._capability_judged = [], set()
            self.argument_evaluations = []
            self._returned = {}
        if self.embeddings is not None and self.embeddings.session_id != session_id:
            self.embeddings = None
        store = self.store
        edges_before = store.edge_count()
        goals_before = len(self.goal_evaluations)
        tasks_before = len(self.task_evaluations)
        capabilities_before = len(self.capability_evaluations)
        arguments_before = len(self.argument_evaluations)
        self.trajectory = trajectory

        annotated = self._extract_node(node, store, trajectory)
        inputs = self._llm_inputs([node.node] if node.node_type == "LLMCall" else [])
        trace = self._track_input_claims(inputs, [annotated], store, trajectory)
        if (
            trace is not None
            and node.node_type == "LLMCall"
            and node.node_id not in trace.input_claims
        ):
            logger.warning(
                "LLM call %s is not in the trajectory yet: input_claims not computed",
                node.node_id,
            )

        total, by_element = self._sum_usage([annotated])
        logger.info(
            "Live node %s (%s) of session %s: %s; LLM usage: %s calls, %s tokens, cost %s USD",
            node.node_id,
            node.node_type,
            session_id,
            {
                key: len(items)
                for key, items in annotated.symbolic_state_elements.items()
            },
            total.calls,
            total.total_tokens,
            total.cost_usd,
        )
        result = ExtractionResult(
            session_id=session_id,
            nodes=[annotated],
            llm_usage=total,
            llm_usage_by_element=by_element,
            symbolic_state=trace,
        )

        # This step should be done outside the extractor, the extractor should only take care of extraction of the elements and basic links creation i.e. in-context relatation
        # Further semantic links, as the ones created by _link_same_claims_live, should be done by a separate component
        # At the moment we do everything in one place to have a simpler access to the store

        # Embedding base relation between claims
        self._link_same_claims_live(result, store)

        # Identical tasks (string matching), linked to the earlier ones.
        self._link_same_task(result, store)

        # Raw order of the tasks: each task is preceded by the tasks of the previous agent call.
        self._link_preceding_tasks(result, store)
        # Each claim is produced by the task of the agent call of its LLM call.
        self._link_produced_by(store, trajectory)
        self.annotate_produced_by(result.nodes)
        # The committing claims are linked to the tool calls they announce (late if the tool call is not there yet).
        self._link_announces(store, trajectory)
        self.annotate_announces(result.nodes)
        # The standing tasks of the agent (read from its system prompt) are linked to its agent calls.
        # In live mode the node returned is the one being processed: an agent call that arrives
        # before the first LLM call of its agent (where the prompt is read) is returned without them.
        self._link_system_tasks(store, trajectory)
        self.annotate_system_tasks(result.nodes)
        # The capabilities of the agent (read from the same prompt) are linked to its agent calls the same way.
        self._link_capabilities(store, trajectory)
        self.annotate_capabilities(result.nodes)
        # The user constraints bound the goal of their Conversation node, the system constraints of an agent
        # the tasks of its agent calls (same ordering caveat as the system tasks in live mode).
        self._link_constraints(store, trajectory)
        self.annotate_constraints(result.nodes)
        # The claims a delegation brief could restate, recorded as its input claims.
        self._link_source_claims(store, trajectory)
        # The claims of a delegated agent call that travel back to its caller, as input of the caller's next call.
        self._link_replies(store, trajectory)
        self.annotate_source_claims(result.nodes)
        self.annotate_replies(result.nodes)
        # Semantic relations (supported_by, ...) on the claims and facts of the node.
        self._extract_relations(result, store)
        # Which capabilities of its agent support each task, once the task and the capabilities are there.
        self._evaluate_capabilities(result, store)
        # The claims of the final answer and the goal, when the final call is known and processed.
        # The node returned is the one being processed: if it is the final LLM call, its claims get
        # their ``answers`` now, the caller does not need to know it is the last node.
        self._link_answers(store, trajectory)
        self.annotate_answers(result.nodes)
        # Whether those claims satisfy the goal: only once they are all extracted.
        self._evaluate_goals(result, store)
        result.edges = store.edges_since(edges_before)
        result.evaluations = self._evaluations(
            goals_before, tasks_before, capabilities_before, arguments_before
        )
        # What was judged in this step: a verdict leaves a marker on the element (``fulfils``, ``supported_by_capabilities``,
        # the status of the goal) even when it has no edge (a task no capability supports), so it does not
        # depend on the new edges.
        judged = [e.task_id for e in self.capability_evaluations[capabilities_before:]]
        judged += [e.goal_id for e in self.goal_evaluations[goals_before:]]
        result.updated_nodes = self._refresh(
            annotated,
            result.edges,
            judged,
            goal_judged=len(self.goal_evaluations) > goals_before,
        )
        return result

    def process(
        self,
        data: SessionData,
        store: SymbolicSessionStore | None = None,
        trajectory: SessionTrajectory | None = None,
    ) -> ExtractionResult:
        """Extract from an already fetched session (lets callers dump the input first).

        Every extracted element is also recorded in ``store`` (a new one when not given),
        available afterwards as ``self.store``.
        """
        store = store if store is not None else SymbolicSessionStore(data.session_id)
        self.store = store
        self.trajectory = trajectory
        self.embeddings = None
        self._returned = {}
        self.goal_evaluations, self._evaluated = [], {}
        self.task_evaluations = []
        self.capability_evaluations, self._capability_judged = [], set()
        self.argument_evaluations = []
        # Note sorting by time simply mimics what we would have in a live processing
        annotated = [
            self._extract_node(node, store, trajectory) for node in data.nodes()
        ]  # sorted by startTime
        total, by_element = self._sum_usage(annotated)
        # Defines which claims were present in the input of each LLM call, and records them in the store.
        trace = self._track_input_claims(
            self._llm_inputs(data.llm_spans), annotated, store, trajectory
        )

        logger.info(
            "Extracted %s from session %s; LLM usage: %s calls, %s tokens, cost %s USD",
            {
                key: sum(len(n.symbolic_state_elements.get(key, [])) for n in annotated)
                for key in {e.element_key for e in self._extractors}
            },
            data.session_id,
            total.calls,
            total.total_tokens,
            total.cost_usd,
        )
        result = ExtractionResult(
            session_id=data.session_id,
            session=data.session,
            nodes=annotated,
            llm_usage=total,
            llm_usage_by_element=by_element,
            symbolic_state=trace,
        )
        # Link claims that are likely to be the same as input claims, if we have an embedder and a trajectory.
        self._link_same_claims(result, store)
        # Identical tasks of different agent calls (string matching).
        self._link_same_task(result, store)
        # Raw order of the tasks: each task is preceded by the tasks of the previous agent call.
        self._link_preceding_tasks(result, store)
        # Each claim is produced by the task of the agent call of its LLM call.
        self._link_produced_by(store, trajectory)
        # The committing claims are linked to the tool calls they announce.
        self._link_announces(store, trajectory)
        # Each agent call carries the standing tasks of its agent (read from its system prompt).
        self._link_system_tasks(store, trajectory)
        # Each agent call also carries the capabilities of its agent (read from the same prompt).
        self._link_capabilities(store, trajectory)
        # The user constraints bound the goal of their Conversation node, the system constraints of an agent
        # the tasks of its agent calls.
        self._link_constraints(store, trajectory)
        # The claims a delegation brief could restate, recorded as its input claims.
        self._link_source_claims(store, trajectory)
        # The claims of a delegated agent call that travel back to its caller, as input of the caller's next call.
        self._link_replies(store, trajectory)
        # Semantic relations (supported_by, ...) on the claims and facts of every node.
        self._extract_relations(result, store)
        # Which capabilities of its agent support each task and each system task.
        self._evaluate_capabilities(result, store)
        # The goal and the claims of the final answer.
        self._link_answers(store, trajectory)
        # Whether those claims satisfy the goal.
        self._evaluate_goals(result, store)
        # Every relation is in the store now: the nodes get their annotations from it, once.
        self.annotate(result.nodes)
        result.edges = store.edges()
        result.evaluations = self._evaluations()
        return result

    def _evaluations(
        self, goals: int = 0, tasks: int = 0, capabilities: int = 0, arguments: int = 0
    ) -> dict[str, list[dict[str, Any]]]:
        """The judgements (with their reasoning) made after the given number of goal, task, capability and
        tool-argument evaluations, as plain dicts for the output file."""
        return {
            "goals": [e.model_dump(mode="json") for e in self.goal_evaluations[goals:]],
            "tasks": [e.model_dump(mode="json") for e in self.task_evaluations[tasks:]],
            "capabilities": [
                e.model_dump(mode="json")
                for e in self.capability_evaluations[capabilities:]
            ],
            "argument_origins": [
                e.model_dump(mode="json") for e in self.argument_evaluations[arguments:]
            ],
        }

    def _refresh(
        self,
        current: AnnotatedNode,
        new_edges: Sequence[Edge],
        judged: Sequence[str] = (),
        goal_judged: bool = False,
    ) -> list[AnnotatedNode]:
        """Live step: register ``current`` as returned, and annotate again, from the store, the nodes returned
        before that the step touched:

        * the nodes of the elements at the ends of ``new_edges`` (an AgentCall id is a node id itself, any other
          id is an element, found through the node it is anchored to);
        * the nodes of the elements in ``judged`` (a task whose capabilities were compared, a goal), even without
          an edge: a verdict leaves a marker on the element (``supported_by_capabilities`` is ``[]`` when no
          capability supports the task) and the judgement changes the status of the goal;
        * when a goal was judged (``goal_judged``), every returned node with claims or goals: once a goal is judged
          all the claims have a ``fulfils`` (``[]`` when they do not fulfil it), those that fulfil nothing have no edge.

        ``current`` is annotated too, so a relation recorded after one of the ``annotate_*`` calls of the step is
        on it.

        Returns the earlier nodes whose annotations changed. Only the annotations that ``annotate`` sets are
        compared: a node that was touched but reads the same (a ``same_as`` edge from a later claim, say) is
        not reported."""
        store = self.store
        if store is None:
            return []
        returned = self._returned
        ids = {i for edge in new_edges for i in (edge.source_id, edge.target_id)} | set(
            judged
        )
        touched: dict[str, AnnotatedNode] = {}
        for element_id in ids:
            stored = store.get(element_id)
            for node_id in (element_id, stored.node_id if stored is not None else None):
                if node_id in returned and node_id != current.node_id:
                    touched[node_id] = returned[node_id]
        if goal_judged:
            for node_id, node in returned.items():
                elements = node.symbolic_state_elements
                if node_id != current.node_id and (
                    elements.get("claims") or elements.get("goals")
                ):
                    touched[node_id] = node
        before = {node_id: _annotations(node) for node_id, node in touched.items()}
        if current.node_id:
            returned[current.node_id] = current
        self.annotate([current, *touched.values()])
        updated = [
            node
            for node_id, node in touched.items()
            if _annotations(node) != before[node_id]
        ]
        if updated:
            logger.info(
                "Late relations: %d earlier nodes updated (%s)",
                len(updated),
                ", ".join(n.node_id or "?" for n in updated),
            )
        return updated

    def annotate(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Set on ``nodes`` every annotation that is read from the relations of ``self.store``: ``produced_by``,
        ``system_tasks``, ``capabilities``, ``bounded_by``, ``source_claims`` (and the claims added to
        ``input_claims``), ``reply``, ``answers``, ``supported_by_capabilities``, ``fulfils`` and the status of
        the goals. Each one is a plain read of the store, so calling it again on the same nodes gives the same
        result, and it makes no LLM request.

        ``process`` calls it once, when every relation is recorded. In live mode a node is annotated when it is
        returned, with the relations known then; a relation recorded later (the capabilities of an agent are
        read from its first LLM call, after the AgentCall node that holds its first task) is put on it by
        ``live_process`` at the step that records it (``_refresh``). Calling this on any returned nodes gives
        the same result at any time.
        The relations that are judged on a node (``supported_by``, ``conflicts_with``, ``implements``, ...) are
        not late: they are set when the node is processed, and are not touched here.
        """
        self.annotate_produced_by(nodes)
        self.annotate_announces(nodes)
        self.annotate_system_tasks(nodes)
        self.annotate_capabilities(nodes)
        self.annotate_constraints(nodes)
        self.annotate_source_claims(nodes)
        self.annotate_replies(nodes)
        self.annotate_answers(nodes)
        self.annotate_task_capabilities(nodes)
        self.annotate_fulfils(nodes)
        self.annotate_goal_status(nodes)

    def _extract_node(
        self,
        node: KGNode,
        store: SymbolicSessionStore,
        trajectory: SessionTrajectory | None = None,
    ) -> AnnotatedNode:
        """Run the extractors that apply to ``node``, record their elements in ``store`` and
        return the node with its ``symbolic_state_elements`` and ``llm_usage``. An extractor that
        sets ``uses_trajectory`` (it needs to know the agent of a call) also gets the ``trajectory``."""
        elements: dict[str, list[dict]] = {}
        node_usage: dict[str, LLMUsage] = {}
        for extractor in self._extractors:
            if not extractor.applies_to(node):
                continue
            key = extractor.element_key
            if getattr(extractor, "uses_trajectory", False):
                extracted = extractor.extract(node, store, trajectory=trajectory)
            else:
                extracted = extractor.extract(node, store)
            for element in extracted:
                store.add(key, element, node.node_id)
            elements[key] = [element.model_dump(mode="json") for element in extracted]
            if extractor.last_usage.calls:
                node_usage[key] = extractor.last_usage
        return AnnotatedNode(
            **node.model_dump(), symbolic_state_elements=elements, llm_usage=node_usage
        )

    @staticmethod
    def _sum_usage(
        annotated: list[AnnotatedNode],
    ) -> tuple[LLMUsage, dict[str, LLMUsage]]:
        """Total LLM usage of the nodes, and the part of each element kind."""
        total = LLMUsage()
        by_element: dict[str, LLMUsage] = {}
        for node in annotated:
            for key, usage in node.llm_usage.items():
                by_element[key] = by_element.get(key, LLMUsage()) + usage
                total = total + usage
        return total, by_element

    @staticmethod
    def _llm_inputs(llm_spans: Sequence[dict]) -> dict[str, str]:
        """LLMCall id -> its input text (``prompt``, else ``inputContent``)."""
        return {
            span["id"]: str(span.get("prompt") or span.get("inputContent") or "")
            for span in llm_spans
            if span.get("id")
        }

    def _link_same_claims(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Add ``same_claims`` to every claim (needs an embedder and the input claims)."""
        if self._embedder is None:
            return
        if result.symbolic_state is None:
            logger.info("No input claims: same_claims not computed")
            return
        self.embeddings = embed_result(result, self._embedder)
        comparator = EmbeddingComparator(
            self.embeddings, threshold=self._similarity_threshold
        )
        links = annotate_same_claims(
            result, store, comparator, self._same_claim_threshold
        )
        logger.info(
            "same_claims: %d claim -> input claim links above %s",
            links,
            self._same_claim_threshold,
        )

    def _link_same_claims_live(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """``_link_same_claims`` for one new node: embed its claims, add them to the session's
        ``self.embeddings`` and annotate that node's claims against its input claims (whose
        embeddings come from the earlier calls)."""
        if self._embedder is None:
            return
        new = embed_nodes(result.nodes, self._embedder)
        if self.embeddings is None:
            self.embeddings = EmbeddingSet(
                session_id=result.session_id, model=self._embedder.model_id
            )
        replaced = {e.element_id for e in new}
        self.embeddings.elements = [
            e for e in self.embeddings.elements if e.element_id not in replaced
        ] + new
        if self.embeddings.dimensions is None:
            self.embeddings.dimensions = next(
                (len(v) for e in new for v in e.embeddings.values()), None
            )
        if result.symbolic_state is None:
            logger.info("No input claims: same_claims not computed")
            return
        comparator = EmbeddingComparator(
            self.embeddings, threshold=self._similarity_threshold
        )
        links = annotate_same_claims(
            result, store, comparator, self._same_claim_threshold
        )
        logger.info(
            "same_claims: %d claim -> input claim links above %s",
            links,
            self._same_claim_threshold,
        )

    def _link_same_task(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Link the tasks of the nodes of ``result`` that are identical to a task extracted before
        them in the session: same text once case, extra spaces and trailing punctuation are
        ignored (``same_task_key``, no embeddings). The criteria are not compared.

        Each task gets ``same_as``: the ids of the earlier identical tasks of other agent calls
        (``[]`` when none), like ``same_claims``; the earlier ones are not modified. The pair is
        recorded in ``store`` as a ``same_as`` relation in both directions. The store lists the
        tasks in the order they were extracted, so the same code serves ``process`` (all the
        tasks are there) and ``live_process`` (only the ones up to the current node).
        """
        earlier: dict[
            str, list[tuple[str, str]]
        ] = {}  # key -> [(task id, node id)] seen so far
        same: dict[str, list[str]] = {}  # task id -> ids of the earlier identical tasks
        for stored in store.by_kind("tasks"):
            key = same_task_key(stored.element)
            matches = [
                task_id
                for task_id, node_id in earlier.get(key, [])
                if node_id != stored.node_id
            ]
            same[stored.id] = matches
            earlier.setdefault(key, []).append((stored.id, stored.node_id))
            for other_id in matches:
                store.link(SAME_AS, stored.id, other_id)
                store.link(SAME_AS, other_id, stored.id)
        links = 0
        for node in result.nodes:
            for task in node.symbolic_state_elements.get("tasks", []):
                task["same_as"] = same.get(task["id"], [])
                links += len(task["same_as"])
        if links:
            logger.info("same_as: %d task -> earlier identical task links", links)

    def _link_answers(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link the claims of the final answer to the goals: ``answers`` (Claim -> Goal) for every
        claim of the LLM call that wrote it (``SessionTrajectory.final_llm_call``, found through the
        final State of the session in the KG, not through timestamps). Every goal of the session is
        linked: there is one per session. Without a trajectory, or when the KG gave no final agent
        call, nothing is recorded.

        The whole store is looked at each time, so it serves ``process`` and ``live_process``: in a
        session in progress the link appears once both the claims of the final call and the final
        agent call (known when the KG has the session's final State) are there, whichever comes first.
        """
        final_call = trajectory.final_llm_call() if trajectory is not None else None
        if final_call is None:
            return
        claims = store.claims_for_node(final_call)
        links = 0
        for claim in claims:
            for goal in store.by_kind("goals"):
                store.link(ANSWERS, claim.id, goal.id)
                links += 1
        if links:
            logger.info("answers: %d claim of the final answer -> goal links", links)

    def annotate_answers(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each claim of ``nodes`` its ``answers``: the ids of the goals it answers (``[]`` for a
        claim that is not part of the final answer), from the ``answers`` relations of ``self.store``.
        When the final agent call is unknown (``self.trajectory`` has none) the claims get no key.

        ``process`` and ``live_process`` both call it, on the nodes they return: all of them in
        ``process``, only the node being processed in ``live_process`` (the extractor recognises the
        final LLM call itself through the trajectory, so no caller has to know which node is the last).
        In live mode a node returned earlier is not modified: if the final agent call only becomes
        known after the final LLM call was processed, its claims have no key, but the relation is
        in ``self.store``.
        """
        store, trajectory = self.store, self.trajectory
        if store is None or trajectory is None or not trajectory.final_agent_call:
            return
        for node in nodes:
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["answers"] = store.related(ANSWERS, claim["id"])

    def _evaluate_goals(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Run the ``GoalClaimRelationExtractor`` (if any): do the claims of the final answer satisfy
        the goal? Only for a goal that has such claims (``answers``, recorded by ``_link_answers``,
        which needs the final agent call), so nothing is asked before the final answer is extracted.
        A goal is judged again only if its set of answer claims changed. The verdicts are added to
        ``self.goal_evaluations``, the ``fulfils`` relations are recorded in the store, and the usage
        of the request is added to ``result.llm_usage`` (and ``llm_usage_by_element['relations']``).

        Once a goal was judged, every claim of the nodes of ``result`` gets ``fulfils``: the ids of
        the goals it fulfils (``[]`` when none: a claim of another call, or a final answer that does
        not satisfy the goal). Before that the claims have no such key. In live mode that is the node
        being processed, so the final LLM node carries it; a node returned earlier is not modified.

        The judgement also gives the goal its ``status`` (``achieved`` / ``abandoned`` / ``set``): the
        goal is replaced in the store and, in ``process``, the goal of the result is updated. In live
        mode the node that carried the goal was returned long before, so it keeps the status the goal
        extractor gave it; the new one is in the store and in ``goal_evaluations``.

        An extractor that fails (an LLM error, an unusable reply) is logged and skipped: it must
        not lose the extraction of the node. The goal is not asked again for the same claims.
        """
        extractors = [
            e
            for e in self._relation_extractors
            if isinstance(e, GoalClaimRelationExtractor)
        ]
        if not extractors:
            return
        usage = LLMUsage()
        for extractor in extractors:
            pending = []
            for stored in store.by_kind("goals"):
                claim_ids = tuple(
                    c.id for c in extractor.answer_claims(store, stored.element)
                )
                if claim_ids and self._evaluated.get(stored.id) != claim_ids:
                    self._evaluated[stored.id] = claim_ids
                    pending.append(stored.element)
            if not pending:
                continue
            try:
                evaluations = extractor.extract(store, pending)
            except Exception:
                logger.warning("%s failed", type(extractor).__name__, exc_info=True)
                usage = usage + extractor.last_usage
                continue
            usage = usage + extractor.last_usage
            self.goal_evaluations.extend(evaluations)
            for evaluation in evaluations:
                logger.info(
                    "fulfils: goal %s %s by %d answer claims (%d fulfil it), status %s -> %s: %s",
                    evaluation.goal_id,
                    "satisfied" if evaluation.satisfied else "not satisfied",
                    len(evaluation.claim_ids),
                    len(evaluation.fulfils),
                    evaluation.previous_status,
                    evaluation.status,
                    evaluation.reasoning,
                )
                for node in result.nodes:
                    for goal in node.symbolic_state_elements.get("goals", []):
                        if goal["id"] == evaluation.goal_id:
                            goal["status"] = evaluation.status
        if usage.calls:
            result.llm_usage = result.llm_usage + usage
            result.llm_usage_by_element["relations"] = (
                result.llm_usage_by_element.get("relations", LLMUsage()) + usage
            )
        self.annotate_fulfils(result.nodes)

    def annotate_fulfils(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each claim of ``nodes`` its ``fulfils``: the ids of the goals it fulfils (``[]`` when none),
        once a goal was judged (``self.goal_evaluations``); before that the claims have no such key."""
        store = self.store
        if store is None or not self.goal_evaluations:
            return
        for node in nodes:
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["fulfils"] = store.related(FULFILS, claim["id"])

    def annotate_goal_status(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each goal of ``nodes`` the status it has in the store: the one the goal extractor gave it, or the
        one ``GoalClaimRelationExtractor`` replaced it with once it judged the final answer."""
        store = self.store
        if store is None:
            return
        for node in nodes:
            for goal in node.symbolic_state_elements.get("goals", []):
                stored = store.get(goal["id"])
                if stored is not None:
                    goal["status"] = stored.element.status

    def _evaluate_capabilities(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Run the ``CapabilityTaskRelationExtractor`` (if any): link each task to the capabilities of its
        agent that support it (``supports``, AgentCapability -> task). Every ``Task`` and ``SystemTask`` of the
        store is submitted once, as soon as the capabilities of its agent are known: the task of an agent
        call needs the ``has_capability`` links of the call (``_link_capabilities``, so a trajectory), a
        system task the capabilities with the same ``agent_id``. A task whose agent has no capability (yet)
        is left for a later call and, if there is none, ends with no link. A system task is shared by all
        the calls of its agent and is submitted only once, not once per call.

        Ordering: the capabilities of an agent are read from its first LLM call, after the AgentCall node
        that holds its first task. In ``process`` the whole session is there; in live mode that first task
        is linked when the capabilities arrive, later than its node. The ``supports`` relations are recorded
        in the store, the answers (with the reasoning) added to ``self.capability_evaluations``, and the
        usage of the requests added to ``result.llm_usage`` (and ``llm_usage_by_element['relations']``).

        Every task and system task of the nodes of ``result`` gets ``supported_by_capabilities``: the ids of
        the capabilities linked to it, ``[]`` when there is none (no capability supports it, or its agent has
        none). In live mode only the node being processed is annotated, a task linked after its node was
        returned has the relation in ``self.store`` only.

        A task whose request fails (an LLM error, an unusable reply) is logged and skipped, and not asked
        again: it must not lose the extraction of the node. It ends with no link, like the others.
        """
        extractors = [
            e
            for e in self._relation_extractors
            if isinstance(e, CapabilityTaskRelationExtractor)
        ]
        if not extractors:
            return
        usage = LLMUsage()
        for extractor in extractors:
            for kind in ("system_tasks", "tasks"):
                for stored in store.by_kind(kind):
                    if stored.id in self._capability_judged or not extractor.candidates(
                        store, stored.element
                    ):
                        continue
                    self._capability_judged.add(stored.id)
                    try:
                        evaluations = extractor.extract(store, [stored.element])
                    except Exception:
                        logger.warning(
                            "%s failed on %s",
                            type(extractor).__name__,
                            stored.id,
                            exc_info=True,
                        )
                        usage = usage + extractor.last_usage
                        continue
                    usage = usage + extractor.last_usage
                    self.capability_evaluations.extend(evaluations)
                    for evaluation in evaluations:
                        logger.info(
                            "supports: %d of the %d capabilities of the agent linked to %s %s: %s",
                            len(evaluation.supports),
                            len(evaluation.capability_ids),
                            "system task" if kind == "system_tasks" else "task",
                            evaluation.task_id,
                            evaluation.reasoning,
                        )
        if usage.calls:
            result.llm_usage = result.llm_usage + usage
            result.llm_usage_by_element["relations"] = (
                result.llm_usage_by_element.get("relations", LLMUsage()) + usage
            )
        self.annotate_task_capabilities(result.nodes)

    def annotate_task_capabilities(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each task and system task of ``nodes`` its ``supported_by_capabilities``: the ids of the
        capabilities linked to it (``supports``), ``[]`` when there is none. Only when a
        ``CapabilityTaskRelationExtractor`` is in the pipeline; otherwise the key stays unset."""
        store = self.store
        if store is None or not any(
            isinstance(e, CapabilityTaskRelationExtractor)
            for e in self._relation_extractors
        ):
            return
        for node in nodes:
            for key in ("tasks", "system_tasks"):
                for task in node.symbolic_state_elements.get(key, []):
                    task["supported_by_capabilities"] = store.related_from(
                        SUPPORTS, task["id"]
                    )

    def _link_produced_by(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link every claim to the task(s) of the agent call that owns the LLM call that generated
        it: ``produced_by`` (Claim -> Task). Deterministic, nothing is judged. A claim of an agent
        call without task (a router turn, a skipped call) has no such link. An agent call has
        usually one task; when the task extractor found several, the claim is linked to each
        of them (the extractor cannot tell which one it comes from). Without a trajectory the
        agent call of an LLM call is unknown, so nothing is recorded.

        The whole store is looked at each time, so it serves ``process`` and ``live_process``: the
        link appears once the claim and the task of its agent call are both there.
        """
        if trajectory is None:
            return
        links = 0
        for stored in store.by_kind("claims"):
            agent_call = trajectory.agent_call_id(stored.node_id)
            if agent_call is None:
                continue
            for task in store.for_node(agent_call, "tasks"):
                store.link(PRODUCED_BY, stored.id, task.id)
                links += 1
        if links:
            logger.info("produced_by: %d claim -> task links", links)

    def annotate_produced_by(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each claim of ``nodes`` its ``produced_by``: the ids of the tasks it was produced by
        (``[]`` for a claim of an agent call without task), from the ``produced_by`` relations of
        ``self.store``. Without a trajectory the claims get no key.

        In live mode only the node being processed is annotated; a node returned earlier is not
        modified, so a claim whose task was extracted later has no ids there, but the relation is in
        ``self.store``.
        """
        store, trajectory = self.store, self.trajectory
        if store is None or trajectory is None:
            return
        for node in nodes:
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["produced_by"] = store.related(PRODUCED_BY, claim["id"])

    def _link_announces(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link every committing claim that announces tool calls to the ToolCall nodes that ran them:
        ``announces`` (Claim -> ToolCall). The claim extractor gives the ids of the requests in the output
        of the LLM call (``tool_call_ids``, checked against that output); the trajectory gives the ToolCall
        node of each (``match_tool_calls``). Deterministic, nothing is judged. A request whose tool step is
        not in the trajectory yet (a session in progress) is linked at a later step, because the whole
        store is looked at each time. Without a trajectory nothing is recorded.
        """
        if trajectory is None:
            return
        links = 0
        for stored in store.by_kind("claims"):
            wanted = getattr(stored.element, "tool_call_ids", [])
            if not wanted:
                continue
            ran = match_tool_calls(trajectory, stored.node_id)
            for provider_id in wanted:
                if provider_id in ran:
                    store.link(ANNOUNCES, stored.id, ran[provider_id])
                    links += 1
        if links:
            logger.info("announces: %d claim -> tool call links", links)

    def annotate_announces(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each claim of ``nodes`` its ``announces``: the ids of the ToolCall nodes it announces
        (``[]`` for a claim that announces none), and each fact its ``announced_by``: the ids of the claims
        that announce the tool call it is grounded in (``[]`` when none). Both come from the ``announces``
        relations of ``self.store``. Without a trajectory there is no key. In live mode a tool call that
        runs after the node was returned is added by ``_refresh``, like any late relation."""
        store, trajectory = self.store, self.trajectory
        if store is None or trajectory is None:
            return
        for node in nodes:
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["announces"] = store.related(ANNOUNCES, claim["id"])
            for fact in node.symbolic_state_elements.get("facts", []):
                fact["announced_by"] = store.related_from(
                    ANNOUNCES, fact["grounded_in"]
                )

    def _link_system_tasks(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link every agent call to the standing tasks of its agent: ``has_system_task`` (AgentCall ->
        SystemTask). The tasks are extracted once per agent (see ``SystemTaskExtractor``) and shared by
        all its calls, the earlier ones included (the call that ran before the prompt was read); no
        LLM request is made here. The calls come from the trajectory, so without one nothing is
        recorded. The whole store is looked at each time, so it serves ``process`` and ``live_process``.
        """
        if trajectory is None:
            return
        tasks = store.by_kind("system_tasks")
        if not tasks:
            return
        calls = self._calls_by_agent(trajectory)
        links = 0
        for stored in tasks:
            for call_id in calls.get(stored.element.agent_id, []):
                store.link(HAS_SYSTEM_TASK, call_id, stored.id)
                links += 1
        if links:
            logger.info("has_system_task: %d agent call -> standing task links", links)

    @staticmethod
    def _calls_by_agent(trajectory: SessionTrajectory) -> dict[str, list[str]]:
        """agent -> ids of its agent calls, from the trajectory."""
        calls: dict[str, list[str]] = {}
        for step in trajectory.steps:
            if (
                step.get("kind") == "agent"
                and step.get("agent_id")
                and step.get("call_id")
            ):
                calls.setdefault(step["agent_id"], []).append(step["call_id"])
        return calls

    def annotate_system_tasks(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each AgentCall of ``nodes`` its ``system_tasks``: the ids of the standing tasks of its
        agent (``[]`` for an agent without any), from the ``has_system_task`` relations of ``self.store``.
        Only when a ``SystemTaskExtractor`` is in the pipeline and there is a trajectory; otherwise the
        key stays None.

        In live mode only the node being processed is annotated: a call returned before the system
        prompt of its agent was read has no ids there, but the relation is in ``self.store``.
        """
        store, trajectory = self.store, self.trajectory
        if (
            store is None
            or trajectory is None
            or not any(e.element_key == "system_tasks" for e in self._extractors)
        ):
            return
        for node in nodes:
            if node.node_type == "AgentCall" and node.node_id:
                node.system_tasks = store.related(HAS_SYSTEM_TASK, node.node_id)

    def _link_capabilities(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link every agent call to the capabilities of its agent: ``has_capability`` (AgentCall ->
        AgentCapability). Same as ``_link_system_tasks``: the capabilities are extracted once per agent (see
        ``SystemCapabilityExtractor``) and shared by all its calls, the earlier ones included; no LLM
        request is made here, and without a trajectory nothing is recorded."""
        if trajectory is None:
            return
        capabilities = store.by_kind("capabilities")
        if not capabilities:
            return
        calls = self._calls_by_agent(trajectory)
        links = 0
        for stored in capabilities:
            for call_id in calls.get(stored.element.agent_id, []):
                store.link(HAS_CAPABILITY, call_id, stored.id)
                links += 1
        if links:
            logger.info("has_capability: %d agent call -> capability links", links)

    def annotate_capabilities(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each AgentCall of ``nodes`` its ``capabilities``: the ids of the capabilities of its agent
        (``[]`` for an agent without any), from the ``has_capability`` relations of ``self.store``. Only when a
        ``SystemCapabilityExtractor`` is in the pipeline and there is a trajectory; otherwise the key stays
        None. In live mode only the node being processed is annotated (see ``annotate_system_tasks``)."""
        store, trajectory = self.store, self.trajectory
        if (
            store is None
            or trajectory is None
            or not any(e.element_key == "capabilities" for e in self._extractors)
        ):
            return
        for node in nodes:
            if node.node_type == "AgentCall" and node.node_id:
                node.capabilities = store.related(HAS_CAPABILITY, node.node_id)

    def _link_constraints(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Link every constraint to what it bounds: ``bounded_by`` (Goal -> Constraint, Task -> Constraint,
        ``mas:boundedBy``). Deterministic, nothing is judged and no LLM request is made.

        * a ``user`` constraint bounds the goals read from the same Conversation node;
        * a ``system`` constraint holds for every call of its agent, so it bounds the standing tasks of that
          agent (``SystemTask``, found through ``agent_id``, no trajectory needed) and the tasks of all the
          agent calls of that agent (the ones before its prompt was read included). The agent calls come from
          the trajectory, so without one nothing is recorded for them.

        The whole store is looked at each time, so it serves ``process`` and ``live_process``."""
        links = 0
        for stored in store.by_kind("constraints"):
            constraint = stored.element
            if constraint.origin == "user":
                targets = [g.id for g in store.for_node(stored.node_id, "goals")]
            else:
                targets = [
                    s.element.id
                    for s in store.by_kind("system_tasks")
                    if s.element.agent_id == constraint.agent_id
                ]
                if trajectory is not None:
                    targets += [
                        t.id
                        for step in trajectory.steps
                        if step.get("kind") == "agent"
                        and step.get("agent_id") == constraint.agent_id
                        for t in store.for_node(step["call_id"], "tasks")
                    ]
            for target in targets:
                store.link(BOUNDED_BY, target, constraint.id)
                links += 1
        if links:
            logger.info("bounded_by: %d goal or task -> constraint links", links)

    def annotate_constraints(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each goal, task and system task of ``nodes`` its ``bounded_by``: the ids of the constraints that
        bound it (``[]`` when none), from the ``bounded_by`` relations of ``self.store``. Only when a
        constraint extractor is in the pipeline; the tasks also need the trajectory (the system constraints
        are linked to them through the agent calls of the agent).

        In live mode only the node being processed is annotated: a task returned before the system prompt
        of its agent was read has no ids there, but the relation is in ``self.store``."""
        store = self.store
        if store is None or not any(
            e.element_key == "constraints" for e in self._extractors
        ):
            return
        for node in nodes:
            for goal in node.symbolic_state_elements.get("goals", []):
                goal["bounded_by"] = store.related(BOUNDED_BY, goal["id"])
            for task in node.symbolic_state_elements.get("system_tasks", []):
                task["bounded_by"] = store.related(BOUNDED_BY, task["id"])
            if self.trajectory is not None:
                for task in node.symbolic_state_elements.get("tasks", []):
                    task["bounded_by"] = store.related(BOUNDED_BY, task["id"])

    def _link_source_claims(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Record, for each AgentCall that has delegation claims, the claims its brief could restate
        (see ``source_claims``) as its ``input_claim`` relation, so ``ClaimRelationExtractor`` treats
        them as the earlier claims of the delegation claims. The caller is read back from the claims.
        Needs the trajectory and the ``call_preceded_by`` / ``call_returned_from`` relations (recorded
        by ``_track_input_claims``)."""
        if trajectory is None:
            return
        links = 0
        for stored in store.by_kind("delegation_claims"):
            node_id = stored.node_id
            if store.related("input_claim", node_id):
                continue  # already done for the other claims of this brief
            claim = stored.element
            caller = Caller(claim.generated_by, claim.caller_call, claim.caller_agent)
            for source in source_claims(store, trajectory, caller):
                store.link("input_claim", node_id, source.id)
                links += 1
        if links:
            logger.info("input_claim: %d delegation brief -> source claim links", links)
        self._link_brief_to_llm_calls(store, trajectory)

    @staticmethod
    def _link_brief_to_llm_calls(
        store: SymbolicSessionStore, trajectory: SessionTrajectory
    ) -> None:
        """The brief an agent call received is in the context of every LLM call of that agent call, whether
        or not its text can be read in the (cut) input of the call. So the delegation claims of the brief
        are recorded as ``input_claim`` of each LLM call of the agent call that generated claims (a call
        with no claim has nothing to relate and is skipped), and ``ClaimRelationExtractor`` compares the
        claims of the call with them. Not embedded, so ``same_claims`` does not see them."""
        by_call: dict[str, list[str]] = {}
        for stored in store.by_kind("delegation_claims"):
            by_call.setdefault(stored.node_id, []).append(stored.id)
        links = 0
        for step in trajectory.steps:
            ids = by_call.get(step.get("parent_id") or "")
            if (
                step.get("kind") != "llm"
                or not ids
                or not store.claims_for_node(step["call_id"])
            ):
                continue
            for claim_id in ids:
                store.link("input_claim", step["call_id"], claim_id)
                links += 1
        if links:
            logger.info(
                "input_claim: %d LLM call -> delegation claim (brief of its agent call) links",
                links,
            )

    def _link_replies(
        self, store: SymbolicSessionStore, trajectory: SessionTrajectory | None
    ) -> None:
        """Which claims of a delegated agent call B travel back to its caller A (the reply), and where A reads
        them. The reply of B is its output, the text the caller gets back (``delegated_reply``); the claims
        of B's LLM calls whose ``verbatim_source`` occurs in it are the reply claims (``reply_claim``,
        AgentCall -> claim), the others stay in B. The reply claims are recorded as ``input_claim`` of the
        next LLM call of A that generated claims (``reply_receiver``), so ``ClaimRelationExtractor``
        compares the claims of that call with them. The input of that call is cut, so its text cannot be
        used to find them (``input_claims`` by content). Needs the trajectory; the whole store is looked at
        each time, so the link appears once the receiving call has its claims."""
        if trajectory is None:
            return
        links = 0
        for step in trajectory.steps:
            if step.get("kind") != "agent":
                continue
            call_id = step["call_id"]
            delegated = delegated_reply(trajectory, call_id)
            if delegated is None:
                continue
            caller, reply = delegated
            claims = reply_claims(store, trajectory, call_id, reply)
            for claim in claims:
                store.link(REPLY_CLAIM, call_id, claim.id)
            receiver = (
                reply_receiver(store, trajectory, call_id, caller) if claims else None
            )
            for claim in claims if receiver else []:
                store.link("input_claim", receiver, claim.id)
                links += 1
        if links:
            logger.info(
                "input_claim: %d LLM call -> reply claim (output of a delegated agent call) links",
                links,
            )

    def annotate_replies(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each claim of the LLM calls of ``nodes`` that belong to a delegated agent call (see
        ``delegated_reply``) a ``reply`` flag: True when it travels back to the caller, False when it stays
        in the agent call. The other claims (calls that were not delegated to, no trajectory) have no key.
        In live mode the flag is set on the node being processed only."""
        store, trajectory = self.store, self.trajectory
        if store is None or trajectory is None:
            return
        for node in nodes:
            if node.node_type != "LLMCall" or not node.node_id:
                continue
            agent_call = trajectory.agent_call_id(node.node_id)
            if agent_call is None or delegated_reply(trajectory, agent_call) is None:
                continue
            replied = set(store.related(REPLY_CLAIM, agent_call))
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["reply"] = claim["id"] in replied

    def annotate_source_claims(self, nodes: Sequence[AnnotatedNode]) -> None:
        """Give each AgentCall of ``nodes`` that has delegation claims its ``source_claims`` (the ids
        recorded by ``_link_source_claims``, ``[]`` when none); the other nodes keep None. An LLMCall
        linked to the delegation claims of its agent call (``_link_brief_to_llm_calls``) or to the reply
        claims of an agent call it delegated to (``_link_replies``) gets them added to its
        ``input_claims``."""
        store = self.store
        if store is None:
            return
        for node in nodes:
            if not node.node_id:
                continue
            if node.node_type == "AgentCall" and node.symbolic_state_elements.get(
                "delegation_claims"
            ):
                node.source_claims = store.related("input_claim", node.node_id)
            elif node.node_type == "LLMCall":
                # what the content tracking found is already in input_claims: the rest comes from the links above
                extra = [
                    i
                    for i in store.related("input_claim", node.node_id)
                    if i not in (node.input_claims or [])
                ]
                if extra:
                    node.input_claims = list(
                        dict.fromkeys([*(node.input_claims or []), *extra])
                    )

    def _relate_delegation_claims(
        self, node: AnnotatedNode, store: SymbolicSessionStore
    ) -> LLMUsage:
        """Relate the delegation claims of ``node`` to its source claims and to each other, with the
        ``ClaimRelationExtractor`` (same prompt as for the claims of an LLM call); each delegation
        claim gets ``conflicts_with`` / ``same_as`` / ``derived_from``. Nothing when there is no such
        extractor, no delegation claim, or nothing to compare (no source and a single claim)."""
        usage = LLMUsage()
        extractor = next(
            (
                e
                for e in self._relation_extractors
                if isinstance(e, ClaimRelationExtractor)
            ),
            None,
        )
        if extractor is None or not node.node_id:
            return usage
        stored = store.for_node(node.node_id, "delegation_claims")
        if not stored:
            return usage
        try:
            relations = extractor.extract(store, [s.element for s in stored])
        except Exception:
            logger.warning(
                "ClaimRelationExtractor failed on the brief of %s",
                node.node_id,
                exc_info=True,
            )
            return extractor.last_usage
        usage = extractor.last_usage
        if len(stored) > 1 or store.related("input_claim", node.node_id):
            ids = {s.id for s in stored}
            for claim in node.symbolic_state_elements.get("delegation_claims", []):
                claim["conflicts_with"] = self._partners(
                    relations, claim["id"], ids, "conflict"
                )
                claim["same_as"] = self._partners(relations, claim["id"], ids, "same")
                claim["derived_from"] = [
                    r.other_id
                    for r in relations
                    if r.claim_id == claim["id"] and r.relation == "derived_from"
                ]
        return usage

    def _link_preceding_tasks(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Record the raw order of the tasks: each task of an agent call is ``preceded_by`` the tasks
        of the closest agent call before it that has at least one task. The agent calls are walked
        up through ``call_preceded_by`` (from the trajectory) and the walk stops at the first one
        with a task, so a call without task (a router turn, a skipped call) is passed over. When the
        root is reached without finding any, the task has no predecessor.

        Each task of the nodes of ``result`` gets ``preceded_by`` (ids of those tasks, ``[]`` when
        none), and the store gets the ``preceded_by`` relation (Task -> Task). Nothing is judged: it
        is what ``TaskRelationExtractor`` starts from. Needs the trajectory: without one the order is
        unknown, nothing is recorded and the tasks have no ``preceded_by`` key.
        """
        if result.symbolic_state is None:
            return
        links = 0
        for node in result.nodes:
            tasks = node.symbolic_state_elements.get("tasks", [])
            if not tasks or not node.node_id:
                continue
            previous = self._closest_tasks(store, node.node_id)
            for task in tasks:
                for other_id in previous:
                    store.link(PRECEDED_BY, task["id"], other_id)
                task["preceded_by"] = store.related(PRECEDED_BY, task["id"])
                links += len(task["preceded_by"])
        if links:
            logger.info(
                "preceded_by: %d task -> task of an earlier agent call links", links
            )

    @staticmethod
    def _closest_tasks(store: SymbolicSessionStore, call_id: str) -> list[str]:
        """Ids of the tasks of the first agent call, going up from ``call_id`` through
        ``call_preceded_by``, that has any (``[]`` when the root is reached first). Each level is
        all the calls that preceded the previous level; a call is visited once. Passing through a
        call without task (a router), the walk also follows ``call_returned_from``: the worker that
        handed control back to it is the work that came before. The start call is not expanded that
        way: a task is judged against what delegated to it, not against what returned before."""
        visited = {call_id}
        level = [call_id]
        first = True
        while level:
            level = [
                p
                for c in level
                for p in (
                    store.related(CALL_PRECEDED_BY, c)
                    + ([] if first else store.related(CALL_RETURNED_FROM, c))
                )
                if p not in visited
            ]
            first = False
            visited.update(level)
            found = [s.id for p in level for s in store.for_node(p, "tasks")]
            if found:
                return found
        return []

    def _extract_relations(
        self, result: ExtractionResult, store: SymbolicSessionStore
    ) -> None:
        """Run the semantic relation extractors on each node of ``result`` (one node in live mode,
        all of them in ``process``): on the claims generated by the node and the facts linked to
        them (``in_context``). The usage of their LLM requests is added to ``result.llm_usage``
        (and to ``llm_usage_by_element['relations']``).

        A relation extractor that fails (an LLM error, an unusable reply) is logged and skipped:
        it must not lose the extraction of the node.

        When a ``FactClaimLinker`` has judged the claims against the facts in their context, each
        claim of the node also gets ``supported_by`` in the result: the ids of the facts that
        support it (``[]`` when none), like ``same_claims``. Claims that were not judged (no fact
        in context, or the linker failed) have no ``supported_by`` key.

        When a ``ClaimRelationExtractor`` has compared the claims with the claims in the input of
        the call and with each other, each claim of the node also gets ``conflicts_with``,
        ``same_as`` (ids of the claims it conflicts with / restates: input claims, or other claims
        of the node) and ``derived_from`` (ids of the claims it is built on), ``[]`` when none.
        A call with no input claims and a single claim (nothing to compare), or whose comparison
        failed, has none of these keys.

        A ``TaskRelationExtractor`` runs on the tasks of the AgentCall nodes (they have no claims):
        each task also gets ``implements`` in the result, the ids of the tasks of the earlier agent
        call it implements, and ``achieves``, the ids of the goals it achieves (``[]`` when none;
        only a task with no task before it is compared with the goal). It needs the trajectory (the
        preceding agent call), so without one it is not run and the tasks have neither key.
        """
        if not self._relation_extractors:
            return
        has_trajectory = result.symbolic_state is not None
        usage = LLMUsage()
        for node in result.nodes:
            usage = usage + self._extract_node_relations(node, store, has_trajectory)
        if usage.calls:
            result.llm_usage = result.llm_usage + usage
            result.llm_usage_by_element["relations"] = (
                result.llm_usage_by_element.get("relations", LLMUsage()) + usage
            )

    def _extract_node_relations(
        self,
        node: AnnotatedNode,
        store: SymbolicSessionStore,
        has_trajectory: bool = True,
    ) -> LLMUsage:
        """``_extract_relations`` for one node; returns the usage of the LLM requests made.
        ``has_trajectory`` is whether the agent calls could be ordered (task relations need it)."""
        usage = self._relate_delegation_claims(node, store)
        usage = usage + self._judge_tool_arguments(node, store)
        self._link_tool_arguments(node, store)
        claims = store.claims_for_node(node.node_id) if node.node_id else []
        tasks = store.for_node(node.node_id, "tasks") if node.node_id else []
        if not claims and not tasks:
            return usage
        claim_ids = {c.id for c in claims}
        facts = [
            f
            for f in store.by_kind("facts")
            if claim_ids & set(store.related(IN_CONTEXT, f.id))
        ]
        elements: list[SymbolicStateElement] = [
            *(c.element for c in claims),
            *(f.element for f in facts),
            *(t.element for t in tasks),
        ]
        linked = False  # a FactClaimLinker judged every (fact, claim) pair
        claim_relations: list[ClaimRelation] | None = (
            None  # set when a ClaimRelationExtractor ran
        )
        tasks_related = False  # a TaskRelationExtractor judged the tasks of the node
        for extractor in self._relation_extractors:
            if isinstance(extractor, GoalClaimRelationExtractor):
                continue  # judged once, on the final answer: see ``_evaluate_goals``
            if isinstance(extractor, CapabilityTaskRelationExtractor):
                continue  # judged once per task, when its agent's capabilities are known: see ``_evaluate_capabilities``
            if isinstance(extractor, ArgumentOriginExtractor):
                continue  # judged once per tool call, against the claims of its agent call: see ``_judge_tool_arguments``
            if isinstance(extractor, ArgumentFactLinker):
                continue  # run once per tool call, on its fact: see ``_link_tool_arguments``
            if isinstance(extractor, TaskClaimRelationExtractor):
                usage = usage + self._judge_node_tasks(extractor, node, claims, store)
                continue
            if isinstance(extractor, TaskRelationExtractor):
                if not tasks or not has_trajectory:
                    continue
            elif not claims:
                continue
            try:
                relations = extractor.extract(store, elements)
            except Exception:
                logger.warning(
                    "%s failed on node %s",
                    type(extractor).__name__,
                    node.node_id,
                    exc_info=True,
                )
                usage = usage + extractor.last_usage
                continue
            usage = usage + extractor.last_usage
            linked = linked or isinstance(extractor, FactClaimLinker)
            tasks_related = tasks_related or isinstance(
                extractor, TaskRelationExtractor
            )
            if isinstance(extractor, ClaimRelationExtractor):
                claim_relations = [*(claim_relations or []), *relations]
            logger.info(
                "%s: %d relations on %d claims and %d facts of node %s",
                type(extractor).__name__,
                len(relations),
                len(claims),
                len(facts),
                node.node_id,
            )
        if linked and facts:
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["supported_by"] = store.related(SUPPORTED_BY, claim["id"])
        if claim_relations is not None and (
            len(claims) > 1 or store.related("input_claim", node.node_id)
        ):
            for claim in node.symbolic_state_elements.get("claims", []):
                claim["conflicts_with"] = self._partners(
                    claim_relations, claim["id"], claim_ids, "conflict"
                )
                claim["same_as"] = self._partners(
                    claim_relations, claim["id"], claim_ids, "same"
                )
                claim["derived_from"] = [
                    r.other_id
                    for r in claim_relations
                    if r.claim_id == claim["id"] and r.relation == "derived_from"
                ]
        if tasks_related:
            for task in node.symbolic_state_elements.get("tasks", []):
                task["implements"] = store.related(IMPLEMENTS, task["id"])
                task["achieves"] = store.related(ACHIEVES, task["id"])
        return usage

    def _link_tool_arguments(
        self, node: AnnotatedNode, store: SymbolicSessionStore
    ) -> None:
        """Where do the arguments of the tool call ``node`` come from among the results of the earlier tool
        calls? With the ``ArgumentFactLinker`` (if any), on the fact of the tool call: ``supported_by``
        (fact -> earlier facts, attribute ``arguments``), by text matching and timestamps, no LLM. The earlier facts are in the store when
        the ToolCall node is processed (nodes come in start order). Nothing is done for a node that is not a
        tool call, or without a trajectory. A failing extractor is logged and skipped."""
        trajectory = self.trajectory
        extractors = [
            e for e in self._relation_extractors if isinstance(e, ArgumentFactLinker)
        ]
        if (
            not extractors
            or trajectory is None
            or node.node_type != "ToolCall"
            or not node.node_id
        ):
            return
        elements = [s.element for s in store.for_node(node.node_id, "facts")]
        for extractor in extractors:
            try:
                extractor.extract(store, elements, trajectory)
            except Exception:
                logger.warning(
                    "%s failed on tool call %s",
                    type(extractor).__name__,
                    node.node_id,
                    exc_info=True,
                )

    def _judge_tool_arguments(
        self, node: AnnotatedNode, store: SymbolicSessionStore
    ) -> LLMUsage:
        """Where do the arguments of the tool call ``node`` come from? With the ``ArgumentOriginExtractor``
        (if any): the fact of the tool call (``grounded_in`` the node) is compared with the claims its agent
        call had made before the call (the claims of its earlier LLM calls, the one that requested it
        included, and the claims of the brief it received), and the claims its arguments come from are
        linked (``supported_by``, fact of the tool call -> claim). Those claims are in the store when the ToolCall node
        is processed (nodes come in start order), so it is judged once, here.

        Nothing is asked for a node that is not a tool call with an output and arguments, when the agent
        call has no claim before the call, and without a trajectory (the agent call is unknown). The
        verdicts are added to ``self.argument_evaluations``. A failing request (an LLM error, an unusable
        reply) is logged and skipped. Returns the usage of the requests made.
        """
        usage = LLMUsage()
        trajectory = self.trajectory
        extractors = [
            e
            for e in self._relation_extractors
            if isinstance(e, ArgumentOriginExtractor)
        ]
        if (
            not extractors
            or trajectory is None
            or node.node_type != "ToolCall"
            or not node.node_id
        ):
            return usage
        for extractor in extractors:
            for stored in store.for_node(node.node_id, "facts"):
                if not extractor.has_candidates(store, trajectory, stored.element):
                    continue
                try:
                    evaluations = extractor.extract(store, [stored.element], trajectory)
                except Exception:
                    logger.warning(
                        "%s failed on tool call %s",
                        type(extractor).__name__,
                        node.node_id,
                        exc_info=True,
                    )
                    usage = usage + extractor.last_usage
                    continue
                usage = usage + extractor.last_usage
                self.argument_evaluations.extend(evaluations)
                for evaluation in evaluations:
                    logger.info(
                        "supported_by: %d of the %d arguments of %s traced to claims",
                        len(evaluation.traced),
                        len(evaluation.origins),
                        evaluation.tool_name,
                    )
        return usage

    def _judge_node_tasks(
        self,
        extractor: TaskClaimRelationExtractor,
        node: AnnotatedNode,
        claims: Sequence[StoredElement],
        store: SymbolicSessionStore,
    ) -> LLMUsage:
        """Which of the claims of ``node`` fulfil the tasks they are ``produced_by``? One request per
        task, on the claims of this node only (see ``TaskClaimRelationExtractor``). The tasks are
        found through ``produced_by`` (recorded by ``_link_produced_by`` before the relations are
        extracted), so a claim whose task is not in the store yet is not judged.

        Ordering: the task of an agent call is extracted from its AgentCall node, the claims from the
        LLM calls it owns. ``process`` has the whole session, so the order does not matter. In live
        mode the AgentCall node must be processed before its LLM calls (nodes sorted by start time,
        as ``--live`` replays them, guarantees it); an LLM node that arrives first has claims with no
        ``produced_by``, and they are never judged. A task of an agent call that has several LLM calls
        is judged once for each of them.

        The verdicts are added to ``self.task_evaluations`` and the ``fulfils_task`` relations
        are recorded in the store. Each claim of ``node`` that was judged gets ``fulfils_task``: the ids
        of the tasks it fulfils (``[]`` when none); a claim that was not judged has no such key.

        One task is judged at a time: a failing one (an LLM error, an unusable reply) is logged and
        skipped without costing the others theirs. Returns the usage of the requests made.
        """
        usage = LLMUsage()
        task_ids = list(
            dict.fromkeys(t for c in claims for t in store.related(PRODUCED_BY, c.id))
        )
        judged: set[str] = set()
        for task_id in task_ids:
            task = store.get(task_id)
            if task is None:
                continue
            try:
                evaluations = extractor.extract(
                    store, [task.element, *(c.element for c in claims)]
                )
            except Exception:
                logger.warning(
                    "%s failed on task %s, node %s",
                    type(extractor).__name__,
                    task_id,
                    node.node_id,
                    exc_info=True,
                )
                usage = usage + extractor.last_usage
                continue
            usage = usage + extractor.last_usage
            self.task_evaluations.extend(evaluations)
            for evaluation in evaluations:
                judged.update(evaluation.claim_ids)
                logger.info(
                    "fulfils: %d of the %d claims of node %s produced by task %s fulfil it: %s",
                    len(evaluation.fulfils),
                    len(evaluation.claim_ids),
                    node.node_id,
                    evaluation.task_id,
                    evaluation.reasoning,
                )
        for claim in node.symbolic_state_elements.get("claims", []):
            if claim["id"] in judged:
                claim["fulfils_task"] = store.related(FULFILS_TASK, claim["id"])
        return usage

    @staticmethod
    def _partners(
        relations: Sequence[ClaimRelation],
        claim_id: str,
        node_claim_ids: set[str],
        kind: str,
    ) -> list[str]:
        """Ids of the claims joined to ``claim_id`` by the symmetric relation ``kind``. A relation
        is stored from the new claim to the other one; when the other is also a claim of the node,
        it is a partner of that one too."""
        out = []
        for r in relations:
            if r.relation != kind:
                continue
            if r.claim_id == claim_id:
                out.append(r.other_id)
            elif (
                r.other_id == claim_id
                and claim_id in node_claim_ids
                and r.claim_id in node_claim_ids
            ):
                out.append(r.claim_id)
        return out

    def _track_input_claims(
        self,
        inputs: Mapping[str, str],
        annotated: list[AnnotatedNode],
        store: SymbolicSessionStore,
        trajectory: SessionTrajectory | None,
    ) -> SymbolicStateTrace | None:
        """Set ``input_claims`` / ``input_facts`` on the LLMCall nodes of ``annotated`` and record
        them as store relations (``input_claim`` / ``input_fact``: LLMCall -> element).

        For each input fact of a call, ``in_context`` (Fact -> Claim) is also recorded for every
        claim the call generated: the fact was in the input the claim was produced from. It says
        nothing about whether the claim is derived from the fact (see ``FactClaimLinker``).

        ``inputs`` maps an LLMCall id to its input text (the trajectory's ``input_text`` is used for
        the calls that are not in it). The trace covers the whole trajectory, but only the nodes
        of ``annotated`` are updated: in a live session the earlier nodes were returned already.
        """
        if trajectory is None:
            logger.info("No trajectory: input_claims not computed")
            return None
        trace = track_symbolic_state(
            trajectory.steps,
            store,
            inputs,
            trajectory.handoffs,
            self._match_input_facts,
        )
        for node in annotated:
            if node.node_type == "LLMCall" and node.node_id in trace.input_claims:
                node.input_claims = trace.input_claims[node.node_id]
            if node.node_type == "LLMCall" and node.node_id in trace.input_facts:
                node.input_facts = trace.input_facts[node.node_id]
        # The agent call each one follows. A worker handing control back to its router is not a
        # delegation, so it is recorded apart (``call_returned_from``).
        for transition in trace.transitions:
            if transition.via == "return":
                store.link(CALL_RETURNED_FROM, transition.to_call, transition.from_call)
            else:
                store.link(CALL_PRECEDED_BY, transition.to_call, transition.from_call)
        for node in annotated:
            if node.node_type != "LLMCall" or node.node_id is None:
                continue
            for claim_id in trace.input_claims.get(node.node_id, []):
                store.link("input_claim", node.node_id, claim_id)
            for fact_id in trace.input_facts.get(node.node_id, []):
                store.link("input_fact", node.node_id, fact_id)
                # The fact was in the context of every claim the call generated.
                for claim in store.claims_for_node(node.node_id):
                    store.link(IN_CONTEXT, fact_id, claim.id)
        return trace
