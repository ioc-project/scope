from scope.interfaces.models import EdgeFacet
from scope.query_builders.kg import (
    fetch_agent_call_sequence_query,
    fetch_session_query,
    fetch_traversed_nodes_query,
    list_sessions_in_interval_query,
)


def test_fetch_agent_call_sequence_query_has_ontology_and_workaround_paths() -> None:
    ontology, workaround = fetch_agent_call_sequence_query("sess-123")

    ontology_query, ontology_params = ontology
    workaround_query, workaround_params = workaround

    assert ontology_params == {"session_id": "sess-123"}
    assert workaround_params == {"session_id": "sess-123"}
    assert (
        "[:hasMASCalls]->(:MASCall)-[:hasAgentCalls]->(ac:AgentCall)" in ontology_query
    )
    assert "MATCH (ac:AgentCall {sessionId: $session_id})" in workaround_query
    assert "RETURN ac AS agent_call" in ontology_query
    assert "RETURN ac AS agent_call" in workaround_query


def test_list_sessions_in_interval_query_supports_optional_bounds() -> None:
    query, params = list_sessions_in_interval_query(
        "2026-03-01T00:00:00", "2026-03-02T00:00:00"
    )

    assert params["start_time"] == "2026-03-01T00:00:00"
    assert params["end_time"] == "2026-03-02T00:00:00"
    assert "($start_time IS NULL OR ts >= $start_time)" in query
    assert "($end_time IS NULL OR ts <= $end_time)" in query


def test_fetch_traversed_nodes_query_builds_generic_edge_chain() -> None:
    ontology, workaround = fetch_traversed_nodes_query(
        "sess-123",
        "mas:ToolCall",
        edges=[
            EdgeFacet(
                "hasMASCalls", source_entity="mas:Session", target_entity="mas:MASCall"
            ),
            EdgeFacet(
                "hasAgentCalls",
                source_entity="mas:MASCall",
                target_entity="mas:AgentCall",
            ),
            EdgeFacet(
                "invokesTool",
                source_entity="mas:AgentCall",
                target_entity="mas:ToolCall",
            ),
        ],
        order_by=["timestamp"],
    )

    ontology_query, ontology_params = ontology
    workaround_query, workaround_params = workaround

    assert ontology_params == {"session_id": "sess-123"}
    assert workaround_params == {"session_id": "sess-123"}
    assert "MATCH (s:Session {sessionId: $session_id})" in ontology_query
    assert "MATCH (s)-[:hasMASCalls]->(v0:MASCall)" in ontology_query
    assert "MATCH (v0)-[:hasAgentCalls]->(v1:AgentCall)" in ontology_query
    assert "MATCH (v1)-[:invokesTool]->(n:ToolCall)" in ontology_query
    assert "RETURN n AS node" in ontology_query
    assert "MATCH (n:ToolCall {sessionId: $session_id})" in workaround_query


def test_fetch_traversed_nodes_query_uses_only_edges_needed_for_target() -> None:
    ontology, _ = fetch_traversed_nodes_query(
        "sess-123",
        "mas:LLMCall",
        edges=[
            EdgeFacet(
                "hasMASCalls", source_entity="mas:Session", target_entity="mas:MASCall"
            ),
            EdgeFacet(
                "hasAgentCalls",
                source_entity="mas:MASCall",
                target_entity="mas:AgentCall",
            ),
            EdgeFacet(
                "invokesTool",
                source_entity="mas:AgentCall",
                target_entity="mas:ToolCall",
            ),
            EdgeFacet(
                "invokesLLM", source_entity="mas:AgentCall", target_entity="mas:LLMCall"
            ),
        ],
        order_by=["timestamp"],
    )

    ontology_query, ontology_params = ontology

    assert ontology_params == {"session_id": "sess-123"}
    assert "MATCH (s)-[:hasMASCalls]->(v0:MASCall)" in ontology_query
    assert "MATCH (v0)-[:hasAgentCalls]->(v1:AgentCall)" in ontology_query
    assert "MATCH (v1)-[:invokesLLM]->(n:LLMCall)" in ontology_query
    assert "invokesTool" not in ontology_query


def test_fetch_session_query_workaround_covers_has_operation_shape() -> None:
    _, workaround = fetch_session_query("sess-123")

    workaround_query, workaround_params = workaround

    assert workaround_params == {"session_id": "sess-123"}
    assert "MATCH (s:Session {sessionId: $session_id})" in workaround_query
    assert "OPTIONAL MATCH (ac:AgentCall {sessionId: $session_id})" in workaround_query
    assert "OPTIONAL MATCH (agent_call)-[llm_rel]->(lc:LLMCall)" in workaround_query
    assert "WHERE type(llm_rel) = 'HAS_OPERATION'" in workaround_query
    assert "OPTIONAL MATCH (agent_call)-[tool_rel]->(tc:ToolCall)" in workaround_query
    assert "WHERE type(tool_rel) = 'HAS_OPERATION'" in workaround_query
    assert (
        "OPTIONAL MATCH (lc_flat:LLMCall {sessionId: $session_id})" in workaround_query
    )
    assert (
        "OPTIONAL MATCH (tc_flat:ToolCall {sessionId: $session_id})" in workaround_query
    )
