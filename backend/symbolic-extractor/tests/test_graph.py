"""The graph of the neurosymbolic layer: result -> labelled nodes and edges."""

from __future__ import annotations

from typing import Any

from symbolic_extractor.graph import camel, to_graph
from symbolic_extractor.models import AnnotatedNode, ExtractionResult
from symbolic_extractor.store import Edge


def _node(node_type: str, node_id: str, **elements: list[dict[str, Any]]) -> AnnotatedNode:
    return AnnotatedNode(
        node_type=node_type, node={"id": node_id}, symbolic_state_elements=elements
    )


def _result() -> ExtractionResult:
    conversation = _node(
        "Conversation",
        "conversation:s1",
        goals=[
            {
                "id": "g1",
                "goal_text": "plan",
                "success_criteria": ["a"],
                "status": "set",
                "bounded_by": [],
            }
        ],
        constraints=[
            {
                "id": "k1",
                "constraint_text": "budget",
                "constraint_strength": "hard",
                "origin": "user",
                "read_from": "conversation:s1",
            }
        ],
    )
    conversation.node.update(query="plan a trip", response="done")
    return ExtractionResult(
        session_id="s1",
        nodes=[
            conversation,
            _node(
                "AgentCall",
                "agentcall:a1",
                tasks=[
                    {
                        "id": "t1",
                        "task_text": "do it",
                        "status": "assigned",
                        "owned_by": "agentcall:a1",
                        "same_as": [],
                    }
                ],
                capabilities=[
                    {
                        "id": "cap1",
                        "capability_text": "price",
                        "agent_id": "concierge",
                        "system_prompt_id": "sp1",
                    }
                ],
                system_tasks=[
                    {
                        "id": "st1",
                        "task_text": "standing",
                        "agent_id": "concierge",
                        "system_prompt_id": "sp1",
                    }
                ],
                system_prompts=[
                    {
                        "id": "sp1",
                        "agent_id": "concierge",
                        "system_prompt": "You price things",
                        "truncated": False,
                    }
                ],
                constraints=[
                    {
                        "id": "k2",
                        "constraint_text": "no luxury",
                        "constraint_strength": "soft",
                        "origin": "system",
                        "system_prompt_id": "sp1",
                    }
                ],
            ),
            _node(
                "AgentCall",
                "agentcall:a2",
                delegation_claims=[
                    {
                        "id": "dc1",
                        "claim_text": "fare is 5",
                        "claim_force": "assertive",
                        "claim_kind": "factual",
                        "caller_agent": "moderator",
                        "caller_call": "agentcall:a0",
                        "delegated_to": "agentcall:a2",
                    }
                ],
            ),
            _node(
                "LLMCall",
                "llmcall:l1",
                claims=[
                    {
                        "id": "c1",
                        "claim_text": "fare 5",
                        "claim_force": "assertive",
                        "claim_kind": "factual",
                        "generated_by": "llmcall:l1",
                        "supported_by": ["f1"],
                        "tool_call_ids": [],
                    }
                ],
            ),
            _node(
                "ToolCall",
                "toolcall:x1",
                facts=[
                    {
                        "id": "f1",
                        "fact_text": "5",
                        "fact_source_type": "tool_result",
                        "grounded_in": "toolcall:x1",
                        "tool_name": "calc",
                        "tool_arguments": {"expression": "2+3"},
                    }
                ],
            ),
        ],
        edges=[
            Edge(edge_type="supported_by", source_id="c1", target_id="f1"),
            Edge(edge_type="supports", source_id="cap1", target_id="t1"),
            Edge(edge_type="has_capability", source_id="agentcall:a1", target_id="cap1"),
            Edge(edge_type="produced_by", source_id="c1", target_id="nope"),
        ],
    )


def test_names_follow_the_ontology():
    assert camel("claim_text") == "claimText"
    assert camel("call_preceded_by") == "callPrecededBy"


def test_elements_become_labelled_nodes_with_ontology_properties_only():
    nodes = {n.id: n for n in to_graph(_result()).nodes}
    assert nodes["c1"].label == "Claim"
    assert nodes["c1"].props == {
        "claimText": "fare 5", "claimForce": "assertive", "claimKind": "factual",
    }  # fmt: skip
    assert nodes["cap1"].props == {"agentCapabilityText": "price", "source": "system"}
    assert nodes["f1"].props == {"factText": "5", "factSourceType": "tool_result"}  # tool name and arguments are not ontology


def test_a_system_prompt_is_a_node_attached_to_its_agent():
    graph = to_graph(_result())
    prompt = next(n for n in graph.nodes if n.id == "sp1")
    assert prompt.label == "SystemPrompt"
    assert prompt.props == {"systemPromptText": "You price things"}
    edges = {(e.type, e.from_id, e.to_id, e.to_label) for e in graph.edges}
    assert ("systemPromptOf", "sp1", "concierge", "Agent") in edges
    assert "concierge" not in {n.id for n in graph.nodes}  # the Agent node is matched, never written


def test_what_comes_from_a_system_prompt_is_attached_to_it_and_marked():
    graph = to_graph(_result())
    nodes = {n.id: n for n in graph.nodes}
    assert nodes["st1"].label == "Task" and nodes["st1"].props["source"] == "system"
    assert nodes["k2"].props["source"] == "system" and nodes["k1"].props["source"] == "user"
    assert "source" not in nodes["t1"].props  # a task assigned during the run
    edges = {(e.type, e.from_id, e.to_id) for e in graph.edges}
    assert {("definedIn", x, "sp1") for x in ("st1", "cap1", "k2")} <= edges
    assert not any(e[0] == "ownedBy" and e[1] == "st1" for e in edges)  # a standing task has no AgentCall


def test_delegation_claims_are_written_as_claims():
    nodes = {n.id: n for n in to_graph(_result()).nodes}
    assert nodes["dc1"].label == "Claim"
    assert set(nodes["dc1"].props) == {"claimText", "claimForce", "claimKind"}


def test_original_nodes_are_not_written():
    labels = {n.label for n in to_graph(_result()).nodes}
    assert not labels & {"AgentCall", "LLMCall", "ToolCall"}


def test_nothing_outside_the_ontology_is_written():
    graph = to_graph(_result())
    assert {n.label for n in graph.nodes} == {
        "Claim", "Fact", "Task", "AgentCapability", "Goal", "Constraint", "SystemPrompt",
    }  # fmt: skip
    assert not any(e.props for e in graph.edges)
    assert graph.skipped == {"has_capability": 1}  # AgentCall -> capability: no such property
    assert graph.dangling == [("produced_by", "c1", "nope")]
    assert "conversation:s1" not in {n.id for n in graph.nodes}


def test_a_link_to_the_synthetic_conversation_is_skipped_not_dangling():
    result = _result()
    result.edges.append(Edge(edge_type="bounded_by", source_id="g1", target_id="conversation:s1"))
    graph = to_graph(result)
    assert graph.skipped["bounded_by"] == 1
    assert ("bounded_by", "g1", "conversation:s1") not in graph.dangling


def test_pointer_fields_become_edges_to_the_original_nodes():
    edges = {(e.type, e.from_id, e.to_id, e.to_label) for e in to_graph(_result()).edges}
    assert ("generatedBy", "c1", "llmcall:l1", "LLMCall") in edges
    assert ("groundedIn", "f1", "toolcall:x1", "ToolCall") in edges
    assert ("ownedBy", "t1", "agentcall:a1", "AgentCall") in edges
    assert ("supports", "cap1", "t1", "Task") in edges
    assert not any(e[0] in ("goalOf",) for e in edges)  # its target is a MASCall


def test_rows_are_plain_dicts_ready_to_write():
    graph = to_graph(_result())

    nodes, edges = graph.node_rows(), graph.edge_rows()
    assert {n["label"] for n in nodes} >= {"Claim", "Task", "Goal"}
    assert all(set(n) == {"label", "id", "props"} for n in nodes)
    assert all(
        set(e) == {"type", "from_id", "from_label", "to_id", "to_label", "props"} for e in edges
    )
    assert any(e["type"] == "generatedBy" and e["to_label"] == "LLMCall" for e in edges)
