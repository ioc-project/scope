"""The session of a small trip planner, as ``get_failure_detection_input`` returns it, and a fake API library."""

from __future__ import annotations

from typing import Any

import pytest

SESSION = "s1"

# (id, label, properties)
NODES = [
    ("sp-a", "SystemPrompt", {"systemPromptText": "You price things"}),
    ("cap-a", "AgentCapability", {"agentCapabilityText": "price trains", "source": "system"}),
    ("st-a", "Task", {"taskText": "standing", "source": "system"}),
    ("t1", "Task", {"taskText": "book a hotel", "acceptanceCriteria": ["booked"]}),
    ("g1", "Goal", {"goalText": "plan a trip", "successCriteria": ["done"]}),
    (
        "k1",
        "Constraint",
        {"constraintText": "budget", "constraintStrength": "hard", "source": "user"},
    ),
    (
        "c1",
        "Claim",
        {"claimText": "the fare is 5", "claimForce": "assertive", "claimKind": "factual"},
    ),
    (
        "c2",
        "Claim",
        {"claimText": "the total is 9", "claimForce": "assertive", "claimKind": "factual"},
    ),
    ("f1", "Fact", {"factText": "fare: 5", "factSourceType": "tool_result"}),
]
# (source, relation, target)
EDGES = [
    ("sp-a", "systemPromptOf", "agent-a"),
    ("cap-a", "definedIn", "sp-a"),
    ("st-a", "definedIn", "sp-a"),
    ("t1", "ownedBy", "A2"),
    ("t1", "achieves", "g1"),
    ("c1", "generatedBy", "L1"),
    ("c2", "generatedBy", "L2"),
    ("c2", "derivedFrom", "c1"),
    ("c1", "supportedBy", "f1"),
    ("f1", "groundedIn", "T1"),
    ("L2", "inputClaim", "c1"),
    ("L2", "inputFact", "f1"),
    ("c1", "conflictsWith", "c2"),
]
STEPS = [
    {
        "call_id": "A1",
        "kind": "agent",
        "agent_id": "agent-m",
        "parent_id": "",
        "start_time": 0.0,
        "duration_ms": 10000.0,
    },
    {
        "call_id": "A2",
        "kind": "agent",
        "agent_id": "agent-a",
        "parent_id": "",
        "start_time": 1.0,
        "duration_ms": 8000.0,
    },
    {
        "call_id": "L1",
        "kind": "llm",
        "agent_id": "agent-a",
        "parent_id": "A2",
        "start_time": 2.0,
        "duration_ms": 1000.0,
    },
    {
        "call_id": "T1",
        "kind": "tool",
        "agent_id": "agent-a",
        "parent_id": "A2",
        "start_time": 3.0,
        "duration_ms": 500.0,
        "success": False,
    },
    {
        "call_id": "L2",
        "kind": "llm",
        "agent_id": "agent-a",
        "parent_id": "A2",
        "start_time": 4.0,
        "duration_ms": 1000.0,
    },
]


HANDOFFS = [{"from_call": "A1", "to_call": "A2", "kind": "handoff"}]


def source(nodes=None) -> dict[str, Any]:
    """What ``get_failure_detection_input`` returns."""
    return {
        "nodes": [
            {"id": i, "label": label, "props": dict(p)}
            for i, label, p in (NODES if nodes is None else nodes)
        ],
        "edges": [{"source": s, "type": r, "target": t} for s, r, t in EDGES],
        "trajectory": {
            "session_id": SESSION,
            "steps": [dict(s) for s in STEPS],
            "handoffs": HANDOFFS,
            "final_agent_call": "A2",
        },
    }


class Kg:
    """What the worker asks of the API library, recorded."""

    def __init__(self) -> None:
        self.inputs: list[tuple[Any, str, list[str]]] = []
        self.writes: list[tuple[Any, str, list[dict[str, Any]]]] = []
        self.nodes = None  # None: the fixture's; [] a session with no symbolic state

    def get_failure_detection_input(self, db, session_id, edge_types):
        from scope.core.exceptions import NotFoundError

        self.inputs.append((db, session_id, list(edge_types)))
        if self.nodes == []:
            raise NotFoundError("no symbolic state")
        return source(self.nodes)

    def write_cognitive_failure_reports(self, db, session_id, detections):
        detections = list(detections)
        self.writes.append((db, session_id, detections))
        return len(detections)


@pytest.fixture
def kg(monkeypatch) -> Kg:
    kg = Kg()
    monkeypatch.setattr(
        "scope.client.neurosymbolic.get_failure_detection_input", kg.get_failure_detection_input
    )
    monkeypatch.setattr(
        "scope.client.failures.write_cognitive_failure_reports", kg.write_cognitive_failure_reports
    )
    return kg
