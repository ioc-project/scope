#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import subprocess
import sys
from unittest.mock import patch

import httpx
import openai
import pytest

from stateful_evals_be.llm_client import LLMClient

CONFIG = {
    "LLM_MODEL_NAME": "gpt-4o",
    "LLM_BASE_MODEL_URL": "https://llm.example/v1",
    "LLM_API_KEY": "key",
}


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError(message, response=response, body=None)


def test_query_calls_the_openai_sdk():
    with patch("openai.OpenAI") as factory:
        create = factory.return_value.chat.completions.create
        create.return_value = "response"
        client = LLMClient(CONFIG)
        assert (
            client.query([{"role": "user", "content": "hi"}], max_tokens=5)
            == "response"
        )
    factory.assert_called_once_with(
        base_url="https://llm.example/v1", api_key="key", max_retries=3
    )
    create.assert_called_once_with(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=5
    )


def test_a_refused_optional_parameter_is_dropped_and_retried():
    with patch("openai.OpenAI") as factory:
        create = factory.return_value.chat.completions.create
        create.side_effect = [
            _bad_request("Unsupported parameter: reasoning_effort"),
            "ok",
        ]
        assert LLMClient(CONFIG).query([], reasoning_effort="low", max_tokens=5) == "ok"
    assert create.call_args_list[1].kwargs == {
        "model": "gpt-4o",
        "messages": [],
        "max_tokens": 5,
    }


def test_another_bad_request_is_raised():
    with patch("openai.OpenAI") as factory:
        factory.return_value.chat.completions.create.side_effect = _bad_request(
            "context too long"
        )
        with pytest.raises(openai.BadRequestError):
            LLMClient(CONFIG).query([], reasoning_effort="low")


def test_api_version_selects_azure():
    with patch("openai.AzureOpenAI") as factory:
        LLMClient({**CONFIG, "api_version": "2024-06-01"}).query([])
    assert factory.call_args.kwargs["api_version"] == "2024-06-01"


def test_a_client_without_a_key_is_built_without_a_gateway():
    LLMClient({**CONFIG, "LLM_API_KEY": ""})


def test_importing_the_library_and_the_metrics_base_does_not_load_litellm():
    code = (
        "import sys, stateful_evals_be\n"
        "from metrics_computation_engine.metrics.base import BaseMetric\n"
        "from metrics_computation_engine.llm_judge.jury import Jury\n"
        "from stateful_evals_be.llm_client import LLMClient\n"
        "assert Jury({'LLM_BASE_MODEL_URL': '', 'LLM_API_KEY': ''}).llms[0].__class__ is LLMClient\n"
        "assert 'litellm' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
