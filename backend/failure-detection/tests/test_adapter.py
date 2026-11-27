import json
from pathlib import Path

import pytest

from failure_detection import Engine, delta_from_extraction

SAMPLE = (
    Path(__file__).parents[3]
    / "oxp-workers/neurosymbolic-layer/output"
    / "b2be7e18-37ef-403b-83fb-56d78e6ff303.json"
)


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample extraction not available")
def test_sample_extraction_loads_and_runs():
    result = json.loads(SAMPLE.read_text())
    delta = delta_from_extraction(result)
    elements = [n.get("symbolic_state_elements") or {} for n in result["nodes"]]
    assert len(delta.claims) == sum(len(e.get("claims", [])) for e in elements)
    assert len(delta.facts) == sum(len(e.get("facts", [])) for e in elements)
    supported = sum(len(c.get("supported_by") or []) for e in elements for c in e.get("claims", []))
    assert sum(e.relation == "supportedBy" for e in delta.edges) == supported
    assert {c.agent for c in delta.calls if c.kind == "llm"} >= {
        "itinerary_agent",
        "concierge_agent",
    }
    assert len(delta.transitions) == len(result["symbolic_state"]["transitions"])
    agent_calls = [c for c in delta.calls if c.kind == "agent"]
    assert all(
        c.state_claims == tuple(result["symbolic_state"]["agent_states"].get(c.id, ()))
        for c in agent_calls
    )
    engine = Engine(result["session_id"])
    engine.process(delta)
    engine.finalize()


def test_goals_tasks_and_constraints_and_their_links():
    result = {
        "nodes": [
            {
                "node_type": "Conversation",
                "node": {"id": "conv"},
                "symbolic_state_elements": {
                    "goals": [
                        {
                            "id": "g1",
                            "goal_text": "Plan a trip",
                            "success_criteria": ["a", "b"],
                            "status": "set",
                        }
                    ],
                    "constraints": [
                        {
                            "id": "k1",
                            "constraint_text": "Budget under 500",
                            "constraint_strength": "hard",
                            "provided_to": ["ac1"],
                        }
                    ],
                },
            },
            {
                "node_type": "AgentCall",
                "node": {"id": "ac1", "agentName": "a", "startTime": 0, "endTime": 9},
                "symbolic_state_elements": {
                    "tasks": [
                        {
                            "id": "t1",
                            "task_text": "Find flights",
                            "acceptance_criteria": [],
                            "status": "assigned",
                            "owned_by": "ac1",
                            "achieves": ["g1"],
                            "preceded_by": ["t0"],
                            "same_as": [],
                            "implements": [],
                        }
                    ],
                    "claims": [
                        {
                            "id": "c1",
                            "claim_text": "x",
                            "claim_force": "assertive",
                            "generated_by": "ac1",
                            "produced_by": ["t1"],
                            "fulfils_task": ["t1"],
                        }
                    ],
                },
            },
            {
                "node_type": "LLMCall",
                "node": {"id": "l1", "startTime": 1, "endTime": 2},
                "symbolic_state_elements": {
                    "system_tasks": [
                        {
                            "id": "s1",
                            "task_text": "Route work",
                            "acceptance_criteria": ["r"],
                            "agent_id": "a",
                            "system_prompt_id": "sp1",
                        }
                    ],
                    "system_prompts": [{"id": "sp1", "agent_id": "a"}],
                    "capabilities": [
                        {
                            "id": "cap1",
                            "capability_text": "x",
                            "agent_id": "a",
                            "system_prompt_id": "sp1",
                        }
                    ],
                },
            },
        ]
    }
    d = delta_from_extraction(result)
    assert [g.success_criteria for g in d.goals] == [("a", "b")]
    assert [(k.strength, k.text) for k in d.constraints] == [("hard", "Budget under 500")]
    assert {(t.id, t.standing, t.owned_by) for t in d.tasks} == {
        ("t1", False, "ac1"),
        ("s1", True, None),
    }
    assert {(e.source, e.relation, e.target) for e in d.edges} == {
        ("k1", "providedTo", "ac1"),
        ("t1", "achieves", "g1"),
        ("t1", "precededBy", "t0"),
        ("c1", "producedBy", "t1"),
        ("c1", "fulfils", "t1"),
    }
    assert {"goal", "task", "constraint", "claim", "call"} <= d.kinds()
    engine = Engine()
    engine.process(d)
    assert set(engine.graph.goals) == {"g1"} and set(engine.graph.tasks) == {"t1", "s1"}
    assert engine.graph.out("t1", "achieves") == ["g1"] and engine.graph.into("g1", "achieves") == [
        "t1"
    ]
    # as in the KG, the agent of a standing task or a capability is that of its system prompt
    from failure_detection.primitives import prompt_agent

    g = engine.graph
    assert prompt_agent(g, g.tasks["s1"]) == "a" and prompt_agent(g, g.capabilities["cap1"]) == "a"
