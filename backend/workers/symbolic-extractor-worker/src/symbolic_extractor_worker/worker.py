import asyncio
import logging
from typing import Optional

from worker_base.base_worker import BaseWorker
from worker_base.queue_message import BaseQueueMessage

from symbolic_extractor_worker.report import WriteReport

logger = logging.getLogger(__name__)


class SymbolicExtractorWorker(BaseWorker):
    """Extracts the symbolic state of a session and writes it to the KG.

    The worker is the only place where the extraction library and the KG meet. For each session it reads the
    session and its trajectory through the API library (``scope.client.neurosymbolic``), hands them to the
    extraction library (``symbolic_extractor.pipeline.build_pipeline``: claims, facts, goals, tasks, constraints,
    capabilities, system prompts and the relations between them, with an LLM), turns the result into nodes and
    edges (``symbolic_extractor.graph.to_graph``) and writes them through the API library
    (``replace=True``: the symbolic state already written for the session is deleted first, because a new
    extraction can word claims differently and so change their ids).
    ``same_claims`` is computed with the embedding model of the environment (``EMBEDDING_MODEL_NAME``, through
    dem's ``OpenAIEmbedder``, as in scope's embedding worker) unless ``same_claims=False``; the KG has no
    relation for it, so it only shows in the extraction result.
    """

    def __init__(
        self,
        rabbitmq_url: str,
        input_queue: str,
        output_queue: Optional[list[str]] = None,
        feedback_queue=None,
        message_limit: int = -1,
        max_inflight_messages: int = 2,
        same_claims: bool = True,
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
        self.name = "SymbolicExtractorWorker"
        self.input_message_class = BaseQueueMessage
        self.debug = debug
        self._db = None
        self._llm = None
        self._embedder = None
        self.same_claims = same_claims

    def _components(self):
        """The KG connector, the LLM client and (unless ``same_claims=False``) the embedder, built on first use
        (not at import)."""
        if self._db is None:
            from dotenv import find_dotenv, load_dotenv

            # The SCOPE API reads NEO4J_* from the process environment at import time and does not load .env
            # files itself: load ours first. Variables already exported keep precedence.
            load_dotenv(find_dotenv(usecwd=True))
            from scope.dependencies import get_neo4j_connector
            from symbolic_extractor.llm import OpenAIChatClient

            self._db = get_neo4j_connector()
            self._llm = OpenAIChatClient.from_env()
            if self.same_claims:
                from symbolic_extractor.embeddings import TextEmbedder

                # Same OpenAI-compatible embedder as scope's embedding worker (dem's OpenAIEmbedder).
                self._embedder = TextEmbedder.from_env()
        return self._db, self._llm

    def extract_result(self, session_id: str):
        """Read one session from the KG (through the API library) and extract its symbolic state, without
        writing it: the library's ``ExtractionResult``."""
        from scope.client.neurosymbolic import get_symbolic_extraction_input
        from symbolic_extractor.models import SessionData, SessionTrajectory
        from symbolic_extractor.pipeline import build_pipeline

        db, llm = self._components()
        source = get_symbolic_extraction_input(db, session_id)
        data = SessionData.from_payload(source["session"])
        trajectory = SessionTrajectory.model_validate(source["trajectory"])
        # A pipeline holds the state of one session: build one per message.
        pipeline = build_pipeline(llm, embedder=self._embedder)
        return pipeline.process(data, trajectory=trajectory)

    def extract(self, session_id: str) -> WriteReport:
        """Extract one session and write it to the KG (through the API library); the write report."""
        from scope.client.neurosymbolic import write_symbolic_state
        from symbolic_extractor.graph import to_graph

        result = self.extract_result(session_id)
        graph = to_graph(result)
        written = write_symbolic_state(
            self._db, session_id, graph.node_rows(), graph.edge_rows(), replace=True
        )
        report = WriteReport.from_write(session_id, written, graph)
        logger.info("Session %s: %s", session_id, report.summary())
        return report

    async def handle_message(self, msg: BaseQueueMessage):
        logger.info("SymbolicExtractorWorker: extracting session %s", msg.session_id)
        try:
            report = await asyncio.to_thread(self.extract, msg.session_id)
        except Exception:
            logger.exception("Extraction failed for session_id=%s", msg.session_id)
            return None
        if not report.complete:
            logger.warning("Session %s was written with gaps: %s", msg.session_id, report.summary())
        self.output_messages = [
            BaseQueueMessage(
                job_id=msg.job_id,
                session_id=msg.session_id,
                workflow_id=msg.workflow_id,
                local_file=msg.local_file,
            )
        ]
        return True
