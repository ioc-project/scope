import pytest

from symbolic_extractor import SessionTrajectory
from symbolic_extractor.output import (
    read_trajectory_file,
    session_file,
    trajectory_file,
    write_json,
)

TRAJECTORY = {
    "session_id": "s1",
    "steps": [
        {"call_id": "agentcall:a", "kind": "agent", "agent_id": "moderator", "parent_id": ""},
        {"call_id": "llmcall:l1", "kind": "llm", "agent_id": "moderator", "parent_id": "agentcall:a"},
        {"call_id": "toolcall:t1", "kind": "tool", "agent_id": "worker", "parent_id": "agentcall:b"},
    ],
}


def _trajectory(**extra):
    return SessionTrajectory.model_validate({**TRAJECTORY, **extra})


def test_trajectory_maps_calls_to_their_agent_call():
    trajectory = _trajectory()

    assert trajectory.agent_call_id("llmcall:l1") == "agentcall:a"
    assert trajectory.agent_name("toolcall:t1") == "worker"
    assert trajectory.agent_call_id("agentcall:a") is None  # an AgentCall has no owner
    assert trajectory.agent_call_id("unknown") is None
    assert trajectory.step_for("unknown") is None


def test_trajectory_file_is_named_after_the_session_and_round_trips(tmp_path):
    path = write_json(_trajectory(), trajectory_file(tmp_path, "s1"))

    assert path.name == "trajectory_s1.json"
    assert path.parent == session_file(tmp_path, "s1").parent  # same folder as the input
    assert read_trajectory_file(path) == _trajectory()


HANDOFFS = [{"from_call": "agentcall:a", "to_call": "agentcall:b", "kind": "handoff"}]


def test_trajectory_without_handoffs_has_none():
    assert _trajectory().handoffs is None
    assert _trajectory().final_agent_call is None


def test_handoffs_and_final_call_round_trip(tmp_path):
    trajectory = _trajectory(handoffs=HANDOFFS, final_agent_call="agentcall:a")
    assert trajectory.handoffs == HANDOFFS

    path = write_json(trajectory, trajectory_file(tmp_path, "s1"))
    loaded = read_trajectory_file(path)
    assert loaded.handoffs == HANDOFFS and loaded.final_agent_call == "agentcall:a"


def test_no_handoffs_found_is_an_empty_list_not_none():
    assert _trajectory(handoffs=[]).handoffs == []


def test_trajectory_file_rejects_unsafe_session_ids(tmp_path):
    with pytest.raises(ValueError):
        trajectory_file(tmp_path, "../x")
