import json

import pytest
from pydantic import ValidationError

from symbolic_extractor import ExtractionRequest, SessionData, SymbolicExtractor
from symbolic_extractor.output import read_session_file, session_file, write_json

PAYLOAD = {
    "session_id": "s1",
    "session": {"sessionId": "s1"},
    "agent_spans": [{"id": "a1"}],
    "llm_spans": [{"id": "l1"}, {"id": "l2"}],
    "tool_spans": [],
    "conversation": "User: hi\nAssistant: hello",
    "input_text": "hi",
    "output_text": "hello",
}


def test_request_requires_session_id():
    with pytest.raises(ValidationError):
        ExtractionRequest(session_id="")


def test_extractor_processes_session_data():
    result = SymbolicExtractor().process(SessionData.from_payload(PAYLOAD))

    assert result.session_id == "s1"
    assert [n.node_type for n in result.nodes].count("LLMCall") == 2
    assert len(result.nodes) == 4  # the Conversation node + 3 calls
    assert result.nodes[0].node_type == "Conversation"
    assert all(n.symbolic_state_elements == {} for n in result.nodes)  # no extractors configured


def test_input_dump_keeps_full_payload(tmp_path):
    data = SessionData.from_payload(PAYLOAD)
    path = write_json(data, session_file(tmp_path / "input", "s1"))

    assert path.name == "s1.json"
    assert json.loads(path.read_text()) == PAYLOAD  # unmodelled keys such as "conversation" kept


def test_saved_input_can_be_loaded_and_processed(tmp_path):
    saved = SessionData.from_payload(PAYLOAD)
    path = write_json(saved, session_file(tmp_path, "s1"))

    loaded = read_session_file(path)

    assert loaded == saved
    result = SymbolicExtractor().process(loaded)
    assert result.session_id == "s1"
    assert len(result.nodes) == 4  # the Conversation node + 3 calls


@pytest.mark.parametrize("bad", ["", "..", "a/b", "../x"])
def test_session_file_rejects_unsafe_ids(bad, tmp_path):
    with pytest.raises(ValueError):
        session_file(tmp_path, bad)
