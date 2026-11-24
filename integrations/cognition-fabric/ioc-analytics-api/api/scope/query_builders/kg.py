"""Query builders for the **KG** (knowledge-graph) feature domain.

This module contains two groups of builders:

1. **Embedding / similarity** (pre-existing) — ``get_embedding_by_session_id``,
   ``neighbors_query`` — used by the similarity-search endpoints.

2. **KGProvider / DataProvider queries** (added during MCE migration) —
   ``list_sessions_query``, ``list_agents_query``, ``list_llm_calls_query``,
   ``resolve_session_id_queries``, ``fetch_session_query``,
   ``fetch_session_timing_query``, ``fetch_conversation_query``.

   These were previously Cypher strings inlined in ``providers/neo4j_mce.py``.
   Moving them here keeps all Cypher in one place and makes
   ``OXPMetricsProvider`` free of raw query strings.
"""

from __future__ import annotations

from typing import Any

from scope.interfaces.models import EdgeFacet

# Canonical type for a single executable query unit (cypher string + params).
# Each KGProvider builder that has ontology / workaround variants returns a
# ``tuple[QueryPair, QueryPair]`` — (ontology_query, workaround_query).
QueryPair = tuple[str, dict[str, Any]]


# ── similarity function map ──────────────────────────────────────────────────

_SIMILARITY_FUNCTIONS: dict[str, str] = {
    "cosine": "vector.similarity.cosine",
    "euclidean": "vector.similarity.euclidean",
}


# ── public query builders ────────────────────────────────────────────────────


def get_state_from_sessionId(id: str) -> str:
    return f'''MATCH (s:State {{sessionId: "{id}"}}) RETURN s'''


def get_embedding_by_session_id(session_id: str) -> tuple[str, dict[str, Any]]:
    """Return a Cypher query that fetches the embedding vector for a session."""
    query = """
    MATCH (session:Session {sessionId: $session_id})
          -[:hasInitialState]->(state:State)
          -[:hasEmbedding]->(emb:Embedding)
    RETURN emb.embedding AS embedding
    LIMIT 1
    """
    return query, {"session_id": session_id}


def neighbors_query(
    *,
    embedding: list[float],
    max_distance: float = 1.0,
    max_neighbors: int = 10,
    metric_names: list[str] | None = None,
    distance_metric: str = "cosine",
) -> tuple[str, dict[str, Any]]:
    """Build a Cypher query to find sessions whose embedding is close to *embedding*.

    Parameters
    ----------
    embedding:
        The reference embedding vector.
    max_distance:
        Maximum distance threshold (1 − similarity) for a session to be
        included.
    max_neighbors:
        Maximum number of neighbor sessions to return.
    metric_names:
        If provided, only include metrics whose ``metricName`` is in this
        list.  *None* or an empty list means "return all metrics".
    distance_metric:
        Which vector similarity function to use (``cosine`` | ``euclidean``).
    """
    similarity_fn = _SIMILARITY_FUNCTIONS.get(
        distance_metric, "vector.similarity.cosine"
    )

    query = f"""
    MATCH (emb:Embedding)
    WITH emb, {similarity_fn}(emb.embedding, $embedding) AS similarity
    WITH emb, (1 - similarity) AS distance
    WHERE distance <= $max_distance
    MATCH (emb)<-[:hasEmbedding]-(state:State)<-[:hasInitialState]-(session:Session)
    OPTIONAL MATCH (session)-[:hasMetric]->(metric:Metric)
    WHERE $metric_names IS NULL
        OR size($metric_names) = 0
        OR metric.metricName IN $metric_names
    RETURN
        session.sessionId AS sessionId,
        distance,
        emb.embedding   AS embedding,
        collect(DISTINCT metric {{.*}}) AS metrics
    ORDER BY distance ASC
    LIMIT $max_neighbors
    """

    return query, {
        "embedding": embedding,
        "max_distance": max_distance,
        "max_neighbors": max_neighbors,
        "metric_names": metric_names,
    }


# ── KGProvider query builders ────────────────────────────────────────────────


def list_sessions_query(limit: int = 100) -> tuple[str, dict[str, Any]]:
    """List available sessions ordered by most recent first."""
    query = """
    MATCH (s:Session)
    WITH s, coalesce(s.start_time, s.timestamp, s.created_at, s.sessionId) AS ts
    RETURN s.sessionId AS session_id, s.name AS name, ts AS timestamp
    ORDER BY ts DESC
    LIMIT $limit
    """
    return query, {"limit": limit}


def list_sessions_in_interval_query(
    start_time: str | None,
    end_time: str | None,
    limit: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """List sessions whose timestamps fall within an optional time interval."""
    query = """
    MATCH (s:Session)
    WITH s, coalesce(s.startTime, s.start_time, s.timestamp, s.created_at) AS ts
    WHERE ($start_time IS NULL OR ts >= $start_time)
      AND ($end_time IS NULL OR ts <= $end_time)
    RETURN s.sessionId AS session_id, ts AS timestamp
    ORDER BY ts ASC
    """
    if limit is not None:
        query += "\nLIMIT $limit"
    return query, {
        "start_time": start_time,
        "end_time": end_time,
        "limit": limit,
    }


def fetch_session_node_query(session_id: str) -> tuple[str, dict[str, Any]]:
    """Fetch the Session node for a single session identifier."""
    query = """
    MATCH (s:Session {sessionId: $session_id})
    RETURN s
    LIMIT 1
    """
    return query, {"session_id": session_id}


def fetch_agent_call_sequence_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return the ordered AgentCall sequence for a session.

    Ontology query : Session -[:hasMASCalls]-> MASCall -[:hasAgentCalls]-> AgentCall
    Workaround WK-01: flat ``{sessionId: $session_id}`` filter on AgentCall.
    """
    params: dict[str, Any] = {"session_id": session_id}
    ontology: QueryPair = (
        """
        MATCH (:Session {sessionId: $session_id})-[:hasMASCalls]->(:MASCall)-[:hasAgentCalls]->(ac:AgentCall)
        RETURN ac AS agent_call
        ORDER BY coalesce(ac.startTime, ac.start_time, ac.timestamp, ac.executionId)
        """,
        params,
    )
    workaround: QueryPair = (
        """
        MATCH (ac:AgentCall {sessionId: $session_id})
        RETURN ac AS agent_call
        ORDER BY coalesce(ac.startTime, ac.start_time, ac.timestamp, ac.executionId)
        """,
        params,
    )
    return ontology, workaround


def fetch_traversed_nodes_query(
    session_id: str,
    entity_type: str,
    edges: list[EdgeFacet],
    *,
    order_by: list[str] | None = None,
) -> tuple[QueryPair, QueryPair]:
    """Build a generic ontology traversal query plus a sessionId fallback.

    The ontology query starts from the session anchor and follows the declared
    EdgeFacet chain until the requested target entity type is reached.
    """
    target_label = _strip_namespace(entity_type)
    params: dict[str, Any] = {"session_id": session_id}

    if target_label == "Session":
        return fetch_session_node_query(session_id), fetch_session_node_query(
            session_id
        )

    edge_chain = _resolve_edge_chain(edges, target_label)

    if not edge_chain:
        raise ValueError(f"No edges provided for traversed entity {entity_type!r}")

    session_alias = "s"
    clauses = [f"MATCH ({session_alias}:Session {{sessionId: $session_id}})"]
    current_alias = session_alias

    for index, edge in enumerate(edge_chain):
        relation = edge.relation_type
        hops = ""
        if edge.min_hops != 1 or edge.max_hops != 1:
            max_hops = "" if edge.max_hops < 0 else str(edge.max_hops)
            hops = f"*{edge.min_hops}..{max_hops}"

        source_label = (
            _strip_namespace(edge.source_entity) if edge.source_entity else None
        )
        target_label_for_edge = (
            _strip_namespace(edge.target_entity) if edge.target_entity else None
        )

        if (
            source_label
            and current_alias == session_alias
            and source_label != "Session"
        ):
            raise ValueError(
                f"Edge chain must start from Session, got source_entity={edge.source_entity!r}"
            )

        target_alias = "n" if index == len(edge_chain) - 1 else f"v{index}"
        target_label_clause = (
            f":{target_label_for_edge}" if target_label_for_edge else ""
        )

        if edge.direction == "in":
            pattern = f"({target_alias}{target_label_clause})-[:{relation}{hops}]->({current_alias})"
        else:
            pattern = f"({current_alias})-[:{relation}{hops}]->({target_alias}{target_label_clause})"

        clauses.append(f"MATCH {pattern}")
        current_alias = target_alias

    if current_alias != "n":
        clauses.append(f"WITH {current_alias} AS n")

    order_clause = _render_node_order_clause(order_by)
    ontology_query = "\n".join(clauses + [f"RETURN n AS node{order_clause}"])
    workaround_query = (
        f"""
        MATCH (n:{target_label} {{sessionId: $session_id}})
        RETURN n AS node{order_clause}
        """,
        params,
    )

    return (ontology_query, params), workaround_query


def _resolve_edge_chain(edges: list[EdgeFacet], target_label: str) -> list[EdgeFacet]:
    """Resolve the minimal declared edge chain from Session to the target entity.

    Retrieval requests may aggregate edges contributed by several metrics. When that
    happens, the target node should only use the sub-chain that actually reaches its
    entity type rather than blindly traversing every declared edge in order.
    """
    if not edges:
        return []

    indexed_edges: list[tuple[int, EdgeFacet, str, str]] = []
    for index, edge in enumerate(edges):
        source_label = _strip_namespace(edge.source_entity)
        next_label = _strip_namespace(edge.target_entity)
        if not source_label or not next_label:
            return edges
        indexed_edges.append((index, edge, source_label, next_label))

    queue: list[tuple[str, list[EdgeFacet]]] = [("Session", [])]
    seen = {"Session"}

    while queue:
        current_label, chain = queue.pop(0)
        for _, edge, source_label, next_label in indexed_edges:
            if source_label != current_label:
                continue

            next_chain = [*chain, edge]
            if next_label == target_label:
                return next_chain

            if next_label in seen:
                continue
            seen.add(next_label)
            queue.append((next_label, next_chain))

    return edges


def _strip_namespace(entity_type: str | None) -> str:
    if not entity_type:
        return ""
    return entity_type.split(":")[-1]


def _render_node_order_clause(order_by: list[str] | None) -> str:
    if not order_by:
        return ""

    rendered_fields: list[str] = []
    for field in order_by:
        descending = field.startswith("-")
        field_name = field.lstrip("-")
        rendered_fields.append(f"n.{field_name} {'DESC' if descending else 'ASC'}")
    return "\nORDER BY " + ", ".join(rendered_fields)


def list_agents_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return (ontology_query, workaround_query) for listing AgentCall nodes.

    Ontology query : Session -[:hasMASCalls|hasAgentCalls*]-> AgentCall
    Workaround WK-01: flat ``{sessionId: $sid}`` property filter on AgentCall.
    """
    ontology = (
        """
        MATCH (s:Session {sessionId: $sid})-[:hasMASCalls|hasAgentCalls*]->(ac:AgentCall)
        RETURN coalesce(ac.agent_id, ac.id, ac.executionId) AS agent_id,
               coalesce(ac.name, ac.agentName, ac.service_name, 'Unknown') AS name,
               coalesce(ac.agent_type, ac.type, 'Generic') AS agent_type
        ORDER BY coalesce(ac.start_time, ac.timestamp, 0)
        """,
        {"sid": session_id},
    )
    flat = (
        """
        MATCH (ac:AgentCall {sessionId: $sid})
        RETURN coalesce(ac.agent_id, ac.id, ac.executionId) AS agent_id,
               coalesce(ac.name, ac.agentName, ac.service_name, 'Unknown') AS name,
               coalesce(ac.agent_type, ac.type, 'Generic') AS agent_type
        ORDER BY coalesce(ac.start_time, ac.timestamp, 0)
        """,
        {"sid": session_id},
    )
    return ontology, flat


def list_llm_calls_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return (ontology_query, workaround_query) for listing LLMCall nodes.

    Ontology query : Session -[:hasMASCalls|hasAgentCalls*]-> AgentCall -[:invokesLLM]-> LLMCall
    Workaround WK-01: flat ``{sessionId: $sid}`` property filter on LLMCall.
    """
    ontology = (
        """
        MATCH (s:Session {sessionId: $sid})-[:hasMASCalls|hasAgentCalls*]->
              (:AgentCall)-[:invokesLLM]->(lc:LLMCall)
        RETURN coalesce(lc.executionId, lc.spanId) AS id,
               coalesce(lc.modelName, lc.llmName, 'unknown-model') AS model,
               coalesce(lc.timestamp, lc.startTime) AS start
        ORDER BY start
        """,
        {"sid": session_id},
    )
    flat = (
        """
        MATCH (lc:LLMCall {sessionId: $sid})
        RETURN coalesce(lc.executionId, lc.spanId) AS id,
               coalesce(lc.modelName, lc.llmName, 'unknown-model') AS model,
               coalesce(lc.timestamp, lc.startTime) AS start
        ORDER BY start
        """,
        {"sid": session_id},
    )
    return ontology, flat


def resolve_session_id_queries(resource_id: str) -> list[tuple[str, dict]]:
    """Return a list of Cypher queries to try in order to resolve any resource ID
    to its parent session ID.  Each query returns ``sid`` when found.

    Ordered *ontology-first*:
    1. Direct Session node match (ontology-compliant, fast for session IDs).
    2. Workaround WK-01: any node whose ``sessionId`` / ``id`` / ``executionId``
       matches — covers execution elements that carry a flat sessionId property.
    3. Workaround WK-01: narrower execution-element match (executionId only).
    4. State / Transition node lookup.
    """
    return [
        # 1. Direct session match (ontology-compliant)
        (
            "MATCH (s:Session {sessionId: $rid}) RETURN s.sessionId AS sid LIMIT 1",
            {"rid": resource_id},
        ),
        # 2. WK-01 — broad node scan (any node with matching id/executionId/sessionId)
        (
            """
            MATCH (n)
            WHERE n.id = $rid OR n.executionId = $rid OR n.sessionId = $rid
            RETURN n.sessionId AS sid LIMIT 1
            """,
            {"rid": resource_id},
        ),
        # 3. WK-01 — execution element with sessionId property
        (
            """
            MATCH (n)
            WHERE (n.id = $rid OR n.executionId = $rid) AND n.sessionId IS NOT NULL
            RETURN n.sessionId AS sid LIMIT 1
            """,
            {"rid": resource_id},
        ),
        # 4. State / Transition node lookup
        (
            """
            MATCH (n)
            WHERE (n.stateNodeId = $rid OR n.transitionId = $rid) AND n.sessionId IS NOT NULL
            RETURN n.sessionId AS sid LIMIT 1
            """,
            {"rid": resource_id},
        ),
    ]


def fetch_session_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return (ontology_query, workaround_query) for fetching full session data.

    Ontology query : Session -[:hasMASCalls]-> MASCall -[:hasAgentCalls]->
                     AgentCall -[:invokesLLM/invokesTool]-> LLMCall/ToolCall
    Workaround WK-01: flat ``{sessionId}`` property filter on AgentCall plus
                      legacy ``AgentCall -[:HAS_OPERATION]-> (LLMCall|ToolCall)``
                      links, with flat LLMCall / ToolCall property scans as a
                      compatibility fallback. Active because the ingestion
                      pipeline does not yet create the structural relationships.
    """
    params: dict[str, Any] = {"session_id": session_id}
    ontology: QueryPair = (
        """
        MATCH (s:Session {sessionId: $session_id})
        OPTIONAL MATCH (s)-[:hasMASCalls]->(:MASCall)-[:hasAgentCalls]->(ac:AgentCall)
        OPTIONAL MATCH (ac)-[:invokesLLM]->(lc:LLMCall)
        OPTIONAL MATCH (ac)-[:invokesTool]->(tc:ToolCall)
        RETURN s,
               collect(DISTINCT ac) as agent_calls,
               collect(DISTINCT lc) as llm_calls,
               collect(DISTINCT tc) as tool_calls
        """,
        params,
    )
    workaround: QueryPair = (  # WK-01
        """
        MATCH (s:Session {sessionId: $session_id})
        OPTIONAL MATCH (ac:AgentCall {sessionId: $session_id})
        WITH s, collect(DISTINCT ac) AS agent_calls
        CALL (agent_calls) {
            UNWIND agent_calls AS agent_call
            OPTIONAL MATCH (agent_call)-[llm_rel]->(lc:LLMCall)
            WHERE type(llm_rel) = 'HAS_OPERATION'
            WITH collect(DISTINCT lc) AS linked_llm_calls
            OPTIONAL MATCH (lc_flat:LLMCall {sessionId: $session_id})
            WITH linked_llm_calls, collect(DISTINCT lc_flat) AS flat_llm_calls
            WITH linked_llm_calls + flat_llm_calls AS llm_candidates
            UNWIND llm_candidates AS llm_candidate
            WITH llm_candidate
            WHERE llm_candidate IS NOT NULL
            RETURN collect(DISTINCT llm_candidate) AS llm_calls
        }
        CALL (agent_calls) {
            UNWIND agent_calls AS agent_call
            OPTIONAL MATCH (agent_call)-[tool_rel]->(tc:ToolCall)
            WHERE type(tool_rel) = 'HAS_OPERATION'
            WITH collect(DISTINCT tc) AS linked_tool_calls
            OPTIONAL MATCH (tc_flat:ToolCall {sessionId: $session_id})
            WITH linked_tool_calls, collect(DISTINCT tc_flat) AS flat_tool_calls
            WITH linked_tool_calls + flat_tool_calls AS tool_candidates
            UNWIND tool_candidates AS tool_candidate
            WITH tool_candidate
            WHERE tool_candidate IS NOT NULL
            RETURN collect(DISTINCT tool_candidate) AS tool_calls
        }
        RETURN s,
               agent_calls,
               llm_calls,
               tool_calls
        """,
        params,
    )
    return ontology, workaround


def fetch_session_timing_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return (ontology_query, workaround_query) for session timing.

    Both queries are identical: read ``startTime`` / ``duration`` directly from
    the Session node, which is always populated by ingestion since 2026-03-25.
    """
    params: dict[str, Any] = {"session_id": session_id}
    query: QueryPair = (
        """
        MATCH (s:Session {sessionId: $session_id})
        WHERE s.duration IS NOT NULL
        RETURN s.startTime AS startTime, s.duration AS duration
        LIMIT 1
        """,
        params,
    )
    return query, query


def fetch_conversation_query(session_id: str) -> tuple[QueryPair, QueryPair]:
    """Return (ontology_query, workaround_query) for conversation data.

    Ontology query : State -[:inputTo]-> Transition -[:leadsTo]-> State
                     Returns rows: transId, timestamp, input, output,
                     input_stateId, output_stateId.
    Workaround     : flat State nodes with ``{sessionId}`` property.
                     Returns rows: content, stateId.
                     Caller can distinguish by checking ``rows[0].get('transId')``.
    """
    params: dict[str, Any] = {"session_id": session_id}
    ontology: QueryPair = (
        """
        MATCH (input_state:State)-[:inputTo]->(t:Transition {sessionId: $session_id})-[:leadsTo]->(output_state:State)
        RETURN t.transitionId AS transId,
               t.timestamp    AS timestamp,
               input_state.content  AS input,
               output_state.content AS output,
               input_state.stateId  AS input_stateId,
               output_state.stateId AS output_stateId
        ORDER BY t.timestamp
        """,
        params,
    )
    workaround: QueryPair = (  # fallback when Transition→State relationships are missing
        """
        MATCH (st:State {sessionId: $session_id})
        RETURN st.content AS content, st.stateId AS stateId
        ORDER BY st.stateId
        """,
        params,
    )
    return ontology, workaround
