import pytest

from symbolic_extractor import KGSessionReader
from symbolic_extractor.kg_reader import fetch_handoffs, fetch_mas_name
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


class _Provider:
    def fetch(self, resource_id, requirements):
        return {"session_id": resource_id}


def _reader(fetched=None):
    calls = fetched if fetched is not None else []
    return KGSessionReader(
        _Provider(), trajectory_fetcher=lambda sid: calls.append(sid) or TRAJECTORY
    )


def test_trajectory_maps_calls_to_their_agent_call():
    trajectory = _reader().read_trajectory("s1")

    assert trajectory.agent_call_id("llmcall:l1") == "agentcall:a"
    assert trajectory.agent_name("toolcall:t1") == "worker"
    assert trajectory.agent_call_id("agentcall:a") is None  # an AgentCall has no owner
    assert trajectory.agent_call_id("unknown") is None
    assert trajectory.step_for("unknown") is None


def test_reader_fetches_the_trajectory_of_the_requested_session():
    fetched = []
    _reader(fetched).read_trajectory("s1")
    assert fetched == ["s1"]


def test_reader_without_fetcher_cannot_read_a_trajectory():
    with pytest.raises(ValueError):
        KGSessionReader(_Provider()).read_trajectory("s1")


def test_trajectory_file_is_named_after_the_session_and_round_trips(tmp_path):
    path = write_json(_reader().read_trajectory("s1"), trajectory_file(tmp_path, "s1"))

    assert path.name == "trajectory_s1.json"
    assert path.parent == session_file(tmp_path, "s1").parent  # same folder as the input
    assert read_trajectory_file(path) == _reader().read_trajectory("s1")


HANDOFFS = [{"from_call": "agentcall:a", "to_call": "agentcall:b", "kind": "handoff"}]


def _reader_with_handoffs(fetcher):
    return KGSessionReader(_Provider(), trajectory_fetcher=lambda sid: TRAJECTORY, handoff_fetcher=fetcher)


def test_trajectory_without_handoff_fetcher_has_no_handoffs():
    assert _reader().read_trajectory("s1").handoffs is None


def test_reader_attaches_the_handoffs_and_they_round_trip(tmp_path):
    seen = []
    reader = _reader_with_handoffs(lambda sid: seen.append(sid) or HANDOFFS)
    trajectory = reader.read_trajectory("s1")
    assert seen == ["s1"]
    assert trajectory.handoffs == HANDOFFS

    path = write_json(trajectory, trajectory_file(tmp_path, "s1"))
    assert read_trajectory_file(path).handoffs == HANDOFFS


def test_no_handoffs_found_is_an_empty_list_not_none():
    assert _reader_with_handoffs(lambda sid: []).read_trajectory("s1").handoffs == []


def test_failing_handoff_query_leaves_handoffs_unknown():
    def boom(sid):
        raise RuntimeError("no db")

    assert _reader_with_handoffs(boom).read_trajectory("s1").handoffs is None


class _Db:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, query, params):
        self.calls.append((query, params))
        return self.rows


def test_fetch_handoffs_dedupes_pairs_and_sorts():
    db = _Db([
        {"source": "b", "target": "c", "kind": "shared_state"},
        {"source": "a", "target": "b", "kind": "shared_state"},
        {"source": "a", "target": "b", "kind": "handoff"},
    ])
    assert fetch_handoffs(db, "s1") == [
        {"from_call": "a", "to_call": "b", "kind": "handoff"},
        {"from_call": "b", "to_call": "c", "kind": "shared_state"},
    ]
    assert db.calls[0][1] == {"session_id": "s1"}


def test_fetch_final_agent_call_is_the_call_sharing_the_final_state_or_none():
    from symbolic_extractor.kg_reader import fetch_final_agent_call

    db = _Db([{"id": "agentcall:b"}, {"id": "agentcall:a"}, {"id": "agentcall:b"}])
    assert fetch_final_agent_call(db, "s1") == "agentcall:a"  # first by id, for a stable output
    assert db.calls[0][1] == {"session_id": "s1"}
    assert fetch_final_agent_call(_Db([]), "s1") is None


def test_reader_attaches_the_final_agent_call_and_a_failing_query_leaves_it_unknown():
    def reader(fetcher):
        return KGSessionReader(_Provider(), trajectory_fetcher=lambda sid: TRAJECTORY, final_call_fetcher=fetcher)

    assert _reader().read_trajectory("s1").final_agent_call is None  # no fetcher
    assert reader(lambda sid: "agentcall:a").read_trajectory("s1").final_agent_call == "agentcall:a"

    def boom(sid):
        raise RuntimeError("no db")

    assert reader(boom).read_trajectory("s1").final_agent_call is None


def test_fetch_mas_name_is_the_name_of_the_mas_or_none():
    db = _Db([{"name": "NOA Trip Planner"}, {"name": "NOA Trip Planner"}])
    assert fetch_mas_name(db, "s1") == "NOA Trip Planner"
    assert db.calls[0][1] == {"session_id": "s1"}
    assert fetch_mas_name(_Db([]), "s1") is None
    assert fetch_mas_name(_Db([{"name": None}]), "s1") is None


def test_trajectory_file_rejects_unsafe_session_ids(tmp_path):
    with pytest.raises(ValueError):
        trajectory_file(tmp_path, "../x")
