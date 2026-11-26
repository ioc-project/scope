from types import SimpleNamespace

import pytest

from symbolic_extractor.llm import LITELLM_COST_HEADER, OpenAIChatClient
from symbolic_extractor.usage import TokenPricing


class _RawResponse:
    def __init__(self, headers):
        self.headers = headers

    def parse(self):
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=500_000),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        )


def _client(headers, pricing):
    client = OpenAIChatClient(api_key="x", pricing=pricing)
    create = lambda **_: _RawResponse(headers)  # noqa: E731
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))
    )
    return client


PRICING = TokenPricing(input_per_1m=2.0, output_per_1m=10.0)  # 1M in + 0.5M out = 7.0


def test_proxy_cost_has_priority_over_env_prices():
    usage = _client({LITELLM_COST_HEADER: "0.25"}, PRICING).complete_json("s", "u").usage
    assert usage.cost_usd == pytest.approx(0.25)
    assert usage.total_tokens == 1_500_000


def test_env_prices_are_the_fallback_when_the_proxy_reports_nothing():
    assert _client({}, PRICING).complete_json("s", "u").usage.cost_usd == pytest.approx(7.0)


def test_unparseable_proxy_cost_falls_back_to_env_prices():
    usage = _client({LITELLM_COST_HEADER: "n/a"}, PRICING).complete_json("s", "u").usage
    assert usage.cost_usd == pytest.approx(7.0)


def test_cost_is_none_without_proxy_cost_and_prices():
    assert _client({}, None).complete_json("s", "u").usage.cost_usd is None
