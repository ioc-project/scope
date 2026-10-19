#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import json
from types import SimpleNamespace

import oxp.client.challenges  # noqa: F401  (so mocker can patch it)
import oxp.client.dal  # noqa: F401
import pytest
from conftest import TRAJECTORY
from worker_base.queue_message import BaseQueueMessage

from cognitive_challenge_worker.detector import Completion, DetectionAnswer
from cognitive_challenge_worker.worker import CognitiveChallengeWorker


def _worker(rabbit_url, tmp_path=None, config=None, **kwargs):
    path = None
    if config is not None:
        path = tmp_path / "cognitive_challenge_config.yaml"
        path.write_text(config, encoding="utf-8")
        path = str(path)
    return CognitiveChallengeWorker(
        rabbit_url=rabbit_url,
        input_queue="test_input",
        llm_api_key="key",
        llm_model_name="gpt-4o",
        llm_base_model_url="https://example.test/v1",
        config_path=path,
        **kwargs,
    )


@pytest.fixture
def message():
    return BaseQueueMessage(job_id="job", session_id="session-1", workflow_id="wf")


@pytest.fixture
def kg(mocker):
    kg = mocker.Mock(spec=["execute", "execute_command", "ensure_connected"])
    mocker.patch.object(CognitiveChallengeWorker, "_get_kg", return_value=kg)
    return kg


def _answer(*items):
    return Completion(json.dumps({"detections": list(items)}), prompt_tokens=10, completion_tokens=5)


# ---------------------------------------------------------------- config


def test_without_a_config_file_nothing_is_detected(rabbit_url):
    assert _worker(rabbit_url).challenges == []


@pytest.mark.parametrize("text", ["", "challenges:\n", "challenges: []\n", "challenges:\n  # - IR-1\n"])
def test_an_empty_config_detects_nothing(rabbit_url, tmp_path, text):
    assert _worker(rabbit_url, tmp_path, text).challenges == []


def test_the_configured_challenges_are_loaded(rabbit_url, tmp_path):
    worker = _worker(rabbit_url, tmp_path, "challenges:\n  - ic-2\n  - CC-3\n")

    assert [c["code"] for c in worker.challenges] == ["IC-2", "CC-3"]


def test_an_unknown_challenge_stops_the_worker_at_startup(rabbit_url, tmp_path):
    with pytest.raises(ValueError, match="IC-99"):
        _worker(rabbit_url, tmp_path, "challenges:\n  - IC-2\n  - IC-99\n")


def test_an_unknown_setting_stops_the_worker_at_startup(rabbit_url, tmp_path):
    with pytest.raises(ValueError, match="challenge"):
        _worker(rabbit_url, tmp_path, "challenge:\n  - IC-2\n")


def test_a_missing_config_file_stops_the_worker_at_startup(rabbit_url, tmp_path):
    with pytest.raises(FileNotFoundError):
        CognitiveChallengeWorker(rabbit_url=rabbit_url, input_queue="q", config_path=str(tmp_path / "nope.yaml"))


def test_the_shipped_example_config_loads(rabbit_url):
    from pathlib import Path

    example = Path(__file__).resolve().parents[3] / "cognitive_challenge_config.yaml"
    worker = CognitiveChallengeWorker(rabbit_url=rabbit_url, input_queue="q", config_path=str(example))

    assert {c["code"] for c in worker.challenges} == {
        "IR-1",
        "IC-1",
        "IC-2",
        "IE-1",
        "DR-2",
        "DC-3",
        "DC-4",
        "DE-1",
        "CC-3",
        "CE-1",
    }


# ---------------------------------------------------------------- messages


@pytest.mark.asyncio
async def test_detections_are_written_and_the_message_is_forwarded(
    rabbit_url, tmp_path, kg, message, mocker
):
    mocker.patch("oxp.client.dal.get_session_trajectory", return_value=TRAJECTORY)
    write = mocker.patch("oxp.client.challenges.write_cognitive_challenge_reports", return_value=1)
    worker = _worker(rabbit_url, tmp_path, "challenges: [IC-2]\n")
    mocker.patch.object(
        worker,
        "_complete",
        return_value=_answer(
            {"code": "IC-2", "confidence": "high", "reasoning": "kept going", "call_id": "toolcall:2"}
        ),
    )

    assert await worker.handle_message(message) is True

    write.assert_called_once_with(
        kg, "session-1", [{"code": "IC-2", "confidence": "high", "reasoning": "kept going", "call_id": "toolcall:2"}]
    )
    (forwarded,) = worker.output_messages
    assert (forwarded.session_id, forwarded.job_id, forwarded.workflow_id) == ("session-1", "job", "wf")


@pytest.mark.asyncio
async def test_a_session_with_nothing_detected_writes_an_empty_list(rabbit_url, tmp_path, kg, message, mocker):
    mocker.patch("oxp.client.dal.get_session_trajectory", return_value=TRAJECTORY)
    write = mocker.patch("oxp.client.challenges.write_cognitive_challenge_reports", return_value=0)
    worker = _worker(rabbit_url, tmp_path, "challenges: [IC-2]\n")
    mocker.patch.object(worker, "_complete", return_value=_answer())

    await worker.handle_message(message)

    write.assert_called_once_with(kg, "session-1", [])


@pytest.mark.asyncio
async def test_an_unusable_answer_writes_nothing_but_still_forwards(
    rabbit_url, tmp_path, kg, message, mocker
):
    mocker.patch("oxp.client.dal.get_session_trajectory", return_value=TRAJECTORY)
    write = mocker.patch("oxp.client.challenges.write_cognitive_challenge_reports")
    worker = _worker(rabbit_url, tmp_path, "challenges: [IC-2]\n")
    mocker.patch.object(worker, "_complete", return_value=Completion("I cannot help with that"))

    assert await worker.handle_message(message) is True

    write.assert_not_called()
    assert len(worker.output_messages) == 1


@pytest.mark.asyncio
async def test_a_session_without_steps_is_skipped(rabbit_url, tmp_path, kg, message, mocker):
    mocker.patch("oxp.client.dal.get_session_trajectory", return_value={"session_id": "session-1", "steps": []})
    write = mocker.patch("oxp.client.challenges.write_cognitive_challenge_reports")
    worker = _worker(rabbit_url, tmp_path, "challenges: [IC-2]\n")
    complete = mocker.patch.object(worker, "_complete")

    await worker.handle_message(message)

    complete.assert_not_called()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_with_no_challenge_configured_the_graph_and_the_model_are_not_touched(rabbit_url, kg, message, mocker):
    get_trajectory = mocker.patch("oxp.client.dal.get_session_trajectory")
    worker = _worker(rabbit_url)
    complete = mocker.patch.object(worker, "_complete")

    assert await worker.handle_message(message) is True

    get_trajectory.assert_not_called()
    complete.assert_not_called()
    kg.execute.assert_not_called()
    assert len(worker.output_messages) == 1


# ---------------------------------------------------------------- the model call


class _Client:
    """A model endpoint that rejects the given response format types."""

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        kind = kwargs.get("response_format", {}).get("type")
        if kind in self.reject:
            raise RuntimeError(f"{kind} is not supported")
        message = SimpleNamespace(content='{"detections": []}')
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3)
        )


def _formats(client):
    return [c.get("response_format", {}).get("type", "none") for c in client.calls]


def test_the_answer_schema_is_enforced_and_the_usage_returned(rabbit_url):
    worker = _worker(rabbit_url)
    worker._client = _Client()

    completion = worker._complete("system", "user")

    (call,) = worker._client.calls
    assert call["model"] == "gpt-4o" and call["temperature"] == 0
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    schema = call["response_format"]["json_schema"]["schema"]
    assert call["response_format"]["type"] == "json_schema"
    assert schema == DetectionAnswer.model_json_schema()
    assert set(schema["$defs"]["DetectionItem"]["properties"]) == {"code", "confidence", "reasoning", "call_id"}
    assert (completion.text, completion.prompt_tokens, completion.completion_tokens) == ('{"detections": []}', 7, 3)


def test_an_endpoint_without_schema_support_gets_json_mode(rabbit_url):
    worker = _worker(rabbit_url)
    worker._client = _Client(reject={"json_schema"})

    worker._complete("system", "user")

    assert _formats(worker._client) == ["json_schema", "json_object"]


def test_an_endpoint_with_no_format_support_gets_a_plain_call(rabbit_url):
    worker = _worker(rabbit_url)
    worker._client = _Client(reject={"json_schema", "json_object"})

    completion = worker._complete("system", "user")

    assert _formats(worker._client) == ["json_schema", "json_object", "none"]
    assert completion.text == '{"detections": []}'


def test_the_format_the_endpoint_accepted_is_remembered(rabbit_url):
    worker = _worker(rabbit_url)
    worker._client = _Client(reject={"json_schema"})
    worker._complete("system", "user")
    worker._client.calls.clear()

    worker._complete("system", "user")

    assert _formats(worker._client) == ["json_object"]


def test_a_failing_endpoint_raises_once_every_format_failed(rabbit_url):
    worker = _worker(rabbit_url)
    worker._client = _Client(reject={"json_schema", "json_object", None})
    worker._client.chat.completions.create = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("401 unauthorized"))

    with pytest.raises(RuntimeError, match="401"):
        worker._complete("system", "user")
