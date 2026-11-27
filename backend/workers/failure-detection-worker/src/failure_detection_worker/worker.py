import asyncio
import logging
from typing import Optional

from worker_base.base_worker import BaseWorker
from worker_base.queue_message import BaseQueueMessage

logger = logging.getLogger(__name__)


class FailureDetectionWorker(BaseWorker):
    """Runs the deterministic cognitive-failure detectors over the symbolic state of a session.

    The worker is the only place where the detection library and the KG meet, and all KG access goes through
    the API library. For each session it reads the symbolic state the extractor wrote (claims, facts, goals,
    tasks, constraints, capabilities, system prompts) and the trajectory
    (``scope.client.neurosymbolic.get_failure_detection_input``), builds the graph the detectors read
    (``failure_detection.build_delta``), runs every rule (``failure_detection.Engine``) and writes one
    ``CognitiveFailureReport`` per finding (``failure_detection.detections``, then
    ``scope.client.failures.write_cognitive_failure_reports``), attached to the session and to the call: the same
    node the LLM-based cognitive-failure-worker writes. Re-running a session overwrites the same reports. No LLM
    is called.
    """

    def __init__(
        self,
        rabbitmq_url: str,
        input_queue: str,
        output_queue: Optional[list[str]] = None,
        feedback_queue=None,
        message_limit: int = -1,
        max_inflight_messages: int = 4,
        failures: Optional[list[str]] = None,
        debug: bool = False,
    ):
        super().__init__(
            rabbitmq_url=rabbitmq_url,
            input_queue=input_queue,
            output_queue=output_queue or [],
            feedback_queue=feedback_queue,
            message_limit=message_limit,
            max_inflight_messages=max_inflight_messages,
        )
        self.name = "FailureDetectionWorker"
        self.input_message_class = BaseQueueMessage
        self.debug = debug
        #: Failure codes to report (upper case); empty: all.
        self.failures = {code.strip().upper() for code in failures or [] if code.strip()}
        self._db = None

    def _get_db(self):
        """The KG connector, built on first use (not at import)."""
        if self._db is None:
            from dotenv import find_dotenv, load_dotenv

            # The SCOPE API reads NEO4J_* from the process environment at import time and does not load .env
            # files itself: load ours first. Variables already exported keep precedence.
            load_dotenv(find_dotenv(usecwd=True))
            from scope.dependencies import get_neo4j_connector

            self._db = get_neo4j_connector()
        return self._db

    def detect(self, session_id: str) -> int:
        """Detect and write the failures of one session; the number of reports written (0 when the session has
        no symbolic state)."""
        from failure_detection import (
            EDGE_TYPES,
            Engine,
            SymbolicState,
            build_delta,
            detections,
        )
        from scope.client.failures import write_cognitive_failure_reports
        from scope.client.neurosymbolic import get_failure_detection_input
        from scope.core.exceptions import NotFoundError
        from symbolic_extractor.models import SessionTrajectory

        db = self._get_db()
        try:
            source = get_failure_detection_input(db, session_id, EDGE_TYPES)
        except NotFoundError:
            logger.warning("Session %s has no symbolic state in the KG", session_id)
            return 0
        state = SymbolicState.from_rows(source["nodes"], source["edges"])
        if not state.has_elements():
            logger.warning("Session %s has no claim, fact, goal or task", session_id)
            return 0
        trajectory = SessionTrajectory.model_validate(source["trajectory"])
        engine = Engine(session_id)
        findings = engine.process(build_delta(state, trajectory)) + engine.finalize()
        if self.failures:
            findings = [f for f in findings if f.failure.upper() in self.failures]
        reports = detections(findings, engine.graph)
        written = write_cognitive_failure_reports(db, session_id, reports)
        logger.info(
            "Session %s: %d finding(s), %d report(s) written", session_id, len(findings), written
        )
        return written

    async def handle_message(self, msg: BaseQueueMessage):
        logger.info("FailureDetectionWorker: checking session %s", msg.session_id)
        try:
            await asyncio.to_thread(self.detect, msg.session_id)
        except Exception:
            logger.exception("Failure detection failed for session_id=%s", msg.session_id)
            return None
        self.output_messages = [
            BaseQueueMessage(
                job_id=msg.job_id,
                session_id=msg.session_id,
                workflow_id=msg.workflow_id,
                local_file=msg.local_file,
            )
        ]
        return True
