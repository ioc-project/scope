#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import asyncio
import logging
import os
from typing import Optional

import yaml
from worker_base.base_worker import BaseWorker
from worker_base.queue_message import BaseQueueMessage

from cognitive_challenge_worker.challenges import select_challenges
from cognitive_challenge_worker.detector import Completion, DetectionAnswer, DetectorError, detect

logger = logging.getLogger(__name__)

# How the answer's format is enforced, strictest first; an endpoint that rejects one gets the next.
# Whatever the model returns is validated against ``DetectionAnswer`` afterwards.
_RESPONSE_FORMATS: tuple[dict | None, ...] = (
    {
        "type": "json_schema",
        "json_schema": {"name": "cognitive_challenge_detections", "schema": DetectionAnswer.model_json_schema()},
    },
    {"type": "json_object"},
    None,
)


class CognitiveChallengeWorker(BaseWorker):
    """Worker that detects cognitive challenges in a session.

    Reads the session's trajectory from the KG through the OXP API library, asks the judge model
    which of the configured challenges (from the challenges taxonomy) it shows, and writes a
    ``CognitiveChallengeReport`` for each, attached to the session and to the call where it
    happened. Re-running a session overwrites the same detections. Which challenges to look for is set in
    the config file; with none, the worker detects nothing.
    """

    def __init__(
        self,
        rabbit_url: str,
        input_queue: str,
        output_queue: Optional[list[str]] = None,
        feedback_queue=None,
        message_limit: int = -1,
        max_inflight_messages: int = 4,
        llm_api_key: Optional[str] = None,
        llm_model_name: Optional[str] = None,
        llm_base_model_url: Optional[str] = None,
        config_path: Optional[str] = None,
        debug: bool = False,
    ):
        super().__init__(
            rabbitmq_url=rabbit_url,
            input_queue=input_queue,
            output_queue=output_queue or [],
            feedback_queue=feedback_queue,
            message_limit=message_limit,
            max_inflight_messages=max_inflight_messages,
        )
        self.name = "CognitiveChallengeWorker"
        self.input_message_class = BaseQueueMessage
        self.debug = debug

        self.llm_api_key = llm_api_key or os.getenv("OPENAI_API_KEY", "")
        self.llm_model_name = llm_model_name or os.getenv("LLM_MODEL_NAME", "gpt-4o")
        self.llm_base_model_url = llm_base_model_url or os.getenv("LLM_BASE_MODEL_URL_MCE", "https://api.openai.com/v1")

        self.challenges = self._challenges_from(config_path)
        if not self.challenges:
            logger.warning("No cognitive challenge is configured: the worker will not detect anything")
        self._client = None
        self._format = 0  # index into _RESPONSE_FORMATS of the strictest one the endpoint accepted

    @staticmethod
    def _challenges_from(config_path: Optional[str]) -> list[dict]:
        """The challenges listed under ``challenges`` in the config file; none without a file or a list.

        Checked here, so a misspelt setting or challenge code stops the worker at startup and not
        on every message.
        """
        if not config_path:
            return []
        with open(config_path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        unknown = set(config) - {"challenges"}
        if unknown:
            raise ValueError(f"Unknown setting(s) in {config_path}: {', '.join(sorted(unknown))}")
        codes = config.get("challenges") or []
        return select_challenges([codes] if isinstance(codes, str) else codes)

    def _get_kg(self):
        """The shared Neo4j connector (owned and closed by the base worker)."""
        from oxp.dependencies import get_neo4j_connector

        return get_neo4j_connector()

    def _complete(self, system: str, user: str) -> Completion:
        """One chat completion from the judge model, with the answer format enforced as far as the endpoint allows."""
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.llm_api_key, base_url=self.llm_base_model_url)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for index in range(self._format, len(_RESPONSE_FORMATS)):
            response_format = _RESPONSE_FORMATS[index]
            options = {} if response_format is None else {"response_format": response_format}
            try:
                response = self._client.chat.completions.create(
                    model=self.llm_model_name, messages=messages, temperature=0, **options
                )
            except Exception as exc:
                if index == len(_RESPONSE_FORMATS) - 1:
                    raise
                logger.info("Endpoint rejected response format %s (%s); trying a looser one", options or "none", exc)
                continue
            self._format = index
            break
        usage = getattr(response, "usage", None)
        return Completion(
            text=response.choices[0].message.content or "",
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    def _detect_and_write(self, kg, session_id: str) -> None:
        from oxp.client.challenges import write_cognitive_challenge_reports
        from oxp.client.dal import get_session_trajectory

        trajectory = get_session_trajectory(kg, session_id)
        if not trajectory["steps"]:
            logger.warning("Session %s has no trajectory steps: nothing to detect", session_id)
            return
        try:
            result = detect(trajectory, self.challenges, self._complete)
        except DetectorError as exc:
            logger.error("Cognitive challenge detection failed for session %s: %s", session_id, exc)
            return
        written = write_cognitive_challenge_reports(kg, session_id, [d.as_dict() for d in result.detections])
        logger.info(
            "Session %s: %d challenge(s) detected, %d written | tokens=%d",
            session_id,
            len(result.detections),
            written,
            result.prompt_tokens + result.completion_tokens,
        )

    async def handle_message(self, msg: BaseQueueMessage):
        logger.info("CognitiveChallengeWorker: checking session %s", msg.session_id)
        if self.challenges:
            await asyncio.to_thread(self._detect_and_write, self._get_kg(), msg.session_id)

        self.output_messages = [
            BaseQueueMessage(
                job_id=msg.job_id,
                session_id=msg.session_id,
                workflow_id=msg.workflow_id,
                local_file=msg.local_file,
            )
        ]
        return True
