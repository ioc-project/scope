from scope.interfaces.models import (
    AggregateFacet,
    EdgeFacet,
    NodeFacet,
    RetrievalRequest,
    RetrievalScope,
)
from scope.providers.kg import OXPKGProvider


class FakeConnector:
    def __init__(self) -> None:
        self.fetch_session_calls = 0

    def ensure_connected(self) -> None:
        return None

    def execute(self, query: str, params: dict):
        if "RETURN s.sessionId AS session_id, ts AS timestamp" in query:
            return [
                {"session_id": "session-1", "timestamp": "2026-03-01T10:00:00"},
                {"session_id": "session-2", "timestamp": "2026-03-01T11:00:00"},
            ]

        if "MATCH (t)-[:representsExecution]->(tc:ToolCall)" in query:
            return [
                {
                    "transitionId": "trans-call-1",
                    "executionId": "t-1",
                    "toolName": "planner",
                    "inputParams": "{}",
                    "inputContent": "find documents",
                    "outputContent": "tool-result",
                },
            ]

        if (
            "RETURN n AS node" in query
            and "ToolCall" in query
            and params.get("session_id") == "session-1"
        ):
            return [
                {"node": {"toolName": "planner", "timestamp": 4, "executionId": "t-1"}},
            ]

        session_id = params.get("session_id")
        if (
            "RETURN n AS node" in query
            and "AgentCall" in query
            and session_id == "session-1"
        ):
            return [
                {"node": {"agentName": "B", "timestamp": 2, "executionId": "2"}},
                {"node": {"agentName": "A", "timestamp": 1, "executionId": "1"}},
            ]
        if (
            "RETURN n AS node" in query
            and "AgentCall" in query
            and session_id == "session-2"
        ):
            return [
                {"node": {"agentName": "C", "timestamp": 3, "executionId": "3"}},
            ]

        if (
            "MATCH (s:Session {sessionId: $session_id})" in query
            and "RETURN s" in query
        ):
            self.fetch_session_calls += 1
            if self.fetch_session_calls == 1 and "HAS_OPERATION" not in query:
                return [
                    {
                        "s": {"sessionId": session_id},
                        "agent_calls": [{"executionId": "a-1", "agentName": "planner"}],
                        "llm_calls": [],
                        "tool_calls": [],
                    }
                ]
            if self.fetch_session_calls >= 2 and "HAS_OPERATION" in query:
                return [
                    {
                        "s": {"sessionId": session_id},
                        "agent_calls": [{"executionId": "a-1", "agentName": "planner"}],
                        "llm_calls": [
                            {
                                "executionId": "l-1",
                                "status": "ERROR",
                                "inputContent": "What is 2+2?",
                                "outputContent": "4",
                            }
                        ],
                        "tool_calls": [
                            {
                                "executionId": "t-1",
                                "toolName": "search",
                                "toolArguments": "{}",
                                "outputContent": "ok",
                            }
                        ],
                    }
                ]
            return [{"s": {"sessionId": session_id}}]

        return []


class PartialConversationConnector:
    def ensure_connected(self) -> None:
        return None

    def execute(self, query: str, params: dict):
        session_id = params.get("session_id")

        if (
            "MATCH (s:Session {sessionId: $session_id})" in query
            and "RETURN s" in query
        ):
            return [
                {
                    "s": {"sessionId": session_id},
                    "agent_calls": [],
                    "llm_calls": [
                        {
                            "executionId": "l-1",
                            "inputContent": "What is 2+2?",
                            "outputContent": "4",
                            "status": "OK",
                        }
                    ],
                    "tool_calls": [
                        {
                            "executionId": "t-1",
                            "inputParams": '{"q": "math"}',
                            "status": "ERROR",
                            "outputContent": "calculator failed",
                        }
                    ],
                }
            ]

        if (
            "MATCH (input_state:State)-[:inputTo]->(t:Transition {sessionId: $session_id})"
            in query
        ):
            return [
                {
                    "transId": "tr-1",
                    "timestamp": 1,
                    "input": "What is 2+2?",
                    "output": "",
                    "input_stateId": "s-in",
                    "output_stateId": "s-out",
                }
            ]

        return []


def test_retrieve_supports_time_interval_and_count_aggregate() -> None:
    provider = OXPKGProvider(db=FakeConnector())

    request = RetrievalRequest(
        scope=RetrievalScope(
            start_time="2026-03-01T09:00:00",
            end_time="2026-03-01T12:00:00",
        ),
        nodes=[
            NodeFacet(
                entity_type="mas:AgentCall",
                alias="agent_calls",
                order_by=["timestamp"],
            )
        ],
        edges=[
            EdgeFacet(
                "hasMASCalls", source_entity="mas:Session", target_entity="mas:MASCall"
            ),
            EdgeFacet(
                "hasAgentCalls",
                source_entity="mas:MASCall",
                target_entity="mas:AgentCall",
            ),
        ],
        aggregates=[
            AggregateFacet(
                function="count",
                entity_type="mas:AgentCall",
                alias="agent_call_count",
            )
        ],
    )

    result = provider.retrieve(request)

    assert result["session_ids"] == ["session-1", "session-2"]
    assert result["contexts"]["session-1"]["agent_calls"][0]["agentName"] == "A"
    assert result["contexts"]["session-1"]["agent_call_count"] == 2
    assert result["contexts"]["session-2"]["agent_call_count"] == 1


def test_fetch_uses_retrieval_request_for_single_session() -> None:
    provider = OXPKGProvider(db=FakeConnector())
    result = provider.fetch(
        "session-1",
        requirements=type(
            "Req",
            (),
            {
                "retrieval": RetrievalRequest(
                    nodes=[NodeFacet(entity_type="mas:AgentCall", alias="agent_calls")],
                    edges=[
                        EdgeFacet(
                            "hasMASCalls",
                            source_entity="mas:Session",
                            target_entity="mas:MASCall",
                        ),
                        EdgeFacet(
                            "hasAgentCalls",
                            source_entity="mas:MASCall",
                            target_entity="mas:AgentCall",
                        ),
                    ],
                )
            },
        )(),
    )

    assert result["session_id"] == "session-1"
    assert [call["agentName"] for call in result["agent_calls"]] == ["A", "B"]


def test_retrieve_compiles_generic_toolcall_chain() -> None:
    provider = OXPKGProvider(db=FakeConnector())
    result = provider.retrieve(
        RetrievalRequest(
            scope=RetrievalScope(session_ids=["session-1"]),
            nodes=[NodeFacet(entity_type="mas:ToolCall", alias="tool_calls")],
            edges=[
                EdgeFacet(
                    "hasMASCalls",
                    source_entity="mas:Session",
                    target_entity="mas:MASCall",
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
        )
    )

    assert result["contexts"]["session-1"]["tool_calls"][0]["toolName"] == "planner"


def test_retrieve_enriches_toolcall_output_from_transition_state() -> None:
    provider = OXPKGProvider(db=FakeConnector())
    result = provider.retrieve(
        RetrievalRequest(
            scope=RetrievalScope(session_ids=["session-1"]),
            nodes=[
                NodeFacet(
                    entity_type="mas:ToolCall",
                    alias="tool_calls",
                    fields=["executionId", "toolName", "outputContent", "toolOutput"],
                )
            ],
            edges=[
                EdgeFacet(
                    "hasMASCalls",
                    source_entity="mas:Session",
                    target_entity="mas:MASCall",
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
        )
    )

    assert result["contexts"]["session-1"]["tool_calls"] == [
        {
            "executionId": "t-1",
            "toolName": "planner",
            "outputContent": "tool-result",
            "toolOutput": "tool-result",
        }
    ]


def test_normalize_tool_call_aliases_output_content_and_error_flag() -> None:
    node = OXPKGProvider._normalize_tool_call(
        {
            "executionId": "tool-1",
            "inputParams": '{"q": "x"}',
            "outputContent": "done",
            "status": "ERROR",
        }
    )

    assert node["toolArguments"] == '{"q": "x"}'
    assert node["toolOutput"] == "done"
    assert node["outputContent"] == "done"
    assert node["contains_error"] is True


def test_normalize_llm_call_aliases_prompt_completion_and_error_flag() -> None:
    node = OXPKGProvider._normalize_llm_call(
        {
            "executionId": "llm-1",
            "inputContent": "prompt",
            "outputContent": "completion",
            "error": "timeout",
        }
    )

    assert node["prompt"] == "prompt"
    assert node["completion"] == "completion"
    assert node["contains_error"] is True


def test_fetch_retries_with_workaround_when_primary_lacks_execution_children() -> None:
    provider = OXPKGProvider(db=FakeConnector())

    result = provider.fetch(
        "session-1",
        requirements=type("Req", (), {"retrieval": None})(),
    )

    assert result["session_id"] == "session-1"
    assert [call["executionId"] for call in result["llm_spans"]] == ["l-1"]
    assert [call["executionId"] for call in result["tool_spans"]] == ["t-1"]
    assert result["llm_spans"][0]["prompt"] == "What is 2+2?"
    assert result["llm_spans"][0]["completion"] == "4"
    assert result["input_text"] == "What is 2+2?"
    assert result["output_text"] == "4"


def test_derive_session_query_response_prefers_session_fields_then_llm_spans() -> None:
    query, response = OXPKGProvider._derive_session_query_response(
        {
            "input_query": "session question",
            "final_response": "session answer",
        },
        [
            {
                "prompt": "llm question",
                "completion": "llm answer",
            }
        ],
    )

    assert query == "session question"
    assert response == "session answer"

    fallback_query, fallback_response = OXPKGProvider._derive_session_query_response(
        {},
        [
            {"prompt": "first question", "completion": "first answer"},
            {"prompt": "second question", "completion": "final answer"},
        ],
    )

    assert fallback_query == "first question"
    assert fallback_response == "final answer"


def test_fetch_fills_partial_conversation_from_llm_spans_and_normalizes_tool_spans() -> (
    None
):
    provider = OXPKGProvider(db=PartialConversationConnector())

    result = provider.fetch(
        "session-1",
        requirements=type("Req", (), {"retrieval": None})(),
    )

    assert result["conversation_data"]["query"] == "What is 2+2?"
    assert result["conversation_data"]["response"] == "4"
    assert result["input_text"] == "What is 2+2?"
    assert result["output_text"] == "4"
    assert result["llm_spans"][0]["prompt"] == "What is 2+2?"
    assert result["llm_spans"][0]["completion"] == "4"
    assert result["tool_spans"][0]["toolArguments"] == '{"q": "math"}'
    assert result["tool_spans"][0]["toolOutput"] == "calculator failed"
    assert result["tool_spans"][0]["contains_error"] is True
