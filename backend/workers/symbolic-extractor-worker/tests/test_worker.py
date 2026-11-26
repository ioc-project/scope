import pytest
from symbolic_extractor.models import ExtractionResult, SessionData, SessionTrajectory
from worker_base.queue_message import BaseQueueMessage

from symbolic_extractor_worker.report import WriteReport
from symbolic_extractor_worker.worker import SymbolicExtractorWorker

SOURCE = {
    "session": {
        "session_id": "s1",
        "session": {"sessionId": "s1"},
        "agent_spans": [],
        "llm_spans": [],
        "tool_spans": [],
    },
    "trajectory": {"session_id": "s1", "steps": []},
}


class FakePipeline:
    def __init__(self):
        self.calls = []

    def process(self, data, trajectory=None):
        self.calls.append((data, trajectory))
        return ExtractionResult(session_id=data.session_id, nodes=[])


class Kg:
    """What the worker asks of the API library, recorded."""

    def __init__(self, written=None):
        self.inputs, self.writes = [], []
        self.written = written or {
            "nodes": {},
            "edges": {},
            "missing_edges": {},
            "missing_nodes": {},
            "deleted": 3,
        }

    def get_symbolic_extraction_input(self, db, session_id):
        self.inputs.append((db, session_id))
        return SOURCE

    def write_symbolic_state(self, db, session_id, nodes, edges, *, replace=False, batch_size=200):
        self.writes.append((db, session_id, nodes, edges, replace))
        return self.written


@pytest.fixture
def kg(monkeypatch):
    kg = Kg()
    monkeypatch.setattr(
        "scope.client.neurosymbolic.get_symbolic_extraction_input", kg.get_symbolic_extraction_input
    )
    monkeypatch.setattr("scope.client.neurosymbolic.write_symbolic_state", kg.write_symbolic_state)
    return kg


@pytest.fixture
def pipeline(monkeypatch):
    pipeline = FakePipeline()
    monkeypatch.setattr(
        "symbolic_extractor.pipeline.build_pipeline", lambda llm, embedder=None: pipeline
    )
    return pipeline


@pytest.fixture
def worker(kg, pipeline):
    w = SymbolicExtractorWorker("amqp://x", "in", ["out"])
    w._db, w._llm = "db", object()
    return w


def message():
    return BaseQueueMessage(job_id="j", session_id="s1", workflow_id="w", local_file=None)


def test_the_embedder_is_given_to_the_pipeline_unless_same_claims_is_off(kg, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "symbolic_extractor.pipeline.build_pipeline",
        lambda llm, embedder=None: seen.append(embedder) or FakePipeline(),
    )
    for same_claims in (True, False):
        w = SymbolicExtractorWorker("amqp://x", "in", same_claims=same_claims)
        w._db, w._llm = "db", object()
        w._embedder = "embedder" if same_claims else None
        w.extract("s1")
    assert seen == ["embedder", None]


def test_the_session_comes_from_the_api_library_as_library_objects(worker, kg, pipeline):
    worker.extract_result("s1")

    assert kg.inputs == [("db", "s1")]
    [(data, trajectory)] = pipeline.calls
    assert isinstance(data, SessionData) and isinstance(trajectory, SessionTrajectory)
    assert kg.writes == []  # extract_result does not write


def test_a_session_is_extracted_and_written_replacing_the_previous_run(worker, kg):
    report = worker.extract("s1")

    assert isinstance(report, WriteReport) and report.complete and report.deleted == 3
    [(db, session_id, nodes, edges, replace)] = kg.writes
    assert (db, session_id, nodes, edges, replace) == ("db", "s1", [], [], True)


def test_a_write_with_gaps_is_reported_as_incomplete(worker, kg):
    kg.written = {
        "nodes": {"Claim": 1},
        "edges": {"generatedBy": 0},
        "missing_edges": {"generatedBy": 1},
        "missing_nodes": {"LLMCall": ["llmcall:l1"]},
        "deleted": 0,
    }
    report = worker.extract("s1")

    assert not report.complete
    assert "1 KG nodes missing" in report.summary()


async def test_the_message_is_forwarded_after_the_write(worker):
    assert await worker.handle_message(message()) is True
    [out] = worker.output_messages
    assert out.session_id == "s1" and out.job_id == "j"


async def test_a_failed_extraction_is_dropped_and_not_forwarded(worker, monkeypatch):
    def boom(session_id):
        raise RuntimeError("llm down")

    monkeypatch.setattr(worker, "extract", boom)
    assert await worker.handle_message(message()) is None
    assert not worker.output_messages
