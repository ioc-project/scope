from conftest import SESSION
from failure_detection import EDGE_TYPES
from worker_base.queue_message import BaseQueueMessage

from failure_detection_worker.worker import FailureDetectionWorker


def make(failures=None):
    w = FailureDetectionWorker("amqp://x", "in", ["out"], failures=failures)
    w._db = "db"
    return w


def message():
    return BaseQueueMessage(job_id="j", session_id=SESSION, workflow_id="w", local_file=None)


def test_the_session_comes_from_the_api_library_with_the_relations_the_detectors_read(kg):
    make().detect(SESSION)

    assert kg.inputs == [("db", SESSION, list(EDGE_TYPES))]


def test_detect_writes_one_report_per_finding_through_the_api_library(kg):
    written = make().detect(SESSION)

    [(db, session_id, reports)] = kg.writes
    assert (db, session_id) == ("db", SESSION)
    assert written == len(reports) > 0
    assert len({r["key"] for r in reports}) == len(reports)  # one node per finding
    assert all(r["level"] in ("detection", "risk") and r["rule"] for r in reports)
    assert all(r["confidence"] in ("low", "high") for r in reports)


def test_failures_filters_the_codes(kg):
    make().detect(SESSION)
    code = sorted({r["code"] for r in kg.writes[0][2]})[0]

    make([code.lower()]).detect(SESSION)  # codes are matched ignoring case

    assert {r["code"] for r in kg.writes[1][2]} == {code}


def test_a_session_without_symbolic_state_writes_nothing(kg):
    kg.nodes = []

    assert make().detect(SESSION) == 0 and kg.writes == []


def test_a_session_with_nothing_to_detect_on_writes_nothing(kg):
    kg.nodes = [("sp-a", "SystemPrompt", {"systemPromptText": "x"})]  # no claim, fact, goal or task

    assert make().detect(SESSION) == 0 and kg.writes == []


async def test_a_processed_session_is_forwarded(kg):
    worker = make()

    assert await worker.handle_message(message()) is True
    assert [m.session_id for m in worker.output_messages] == [SESSION]


async def test_a_failing_detection_is_not_forwarded(kg):
    worker = make()

    def boom(*args, **kwargs):
        raise RuntimeError("neo4j down")

    worker.detect = boom

    assert await worker.handle_message(message()) is None
