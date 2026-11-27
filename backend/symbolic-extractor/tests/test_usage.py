"""The usage report counts every request that was paid for, and says how its cost was priced."""

from types import SimpleNamespace

import pytest

from symbolic_extractor.llm import OpenAIChatClient, UnusableReply, usage_of
from symbolic_extractor.models import Claim, Goal, Task
from symbolic_extractor.relations import (
    ClaimRelationExtractor,
    GoalClaimRelationExtractor,
    TaskClaimRelationExtractor,
    TaskRelationExtractor,
)
from symbolic_extractor.usage import LLMUsage, TokenPricing

PAID = LLMUsage(calls=1, prompt_tokens=100, completion_tokens=7, cost_usd=0.001)

CLAIM_A = Claim(id="c1", claim_text="It is cheap.", claim_force="committing", generated_by="l1")
CLAIM_B = Claim(id="c2", claim_text="It costs 75.", claim_force="committing", generated_by="l2")
GOAL = Goal(id="g1", goal_text="Plan the trip.", success_criteria=["A plan"])
TASK = Task(id="t1", task_text="Find a hotel.", owned_by="a1")


class NotJsonLLM:
    """Every request comes back, but the reply is not a JSON object."""

    def __init__(self):
        self.requests = 0

    def complete_json(self, system, user):
        self.requests += 1
        raise UnusableReply("No JSON object found in the LLM reply", PAID)


# --- the client --------------------------------------------------------------------------


def _client(headers, pricing=None, text="not json at all"):
    """An ``OpenAIChatClient`` whose endpoint answers ``text`` with these response headers."""
    client = OpenAIChatClient(api_key="k", pricing=pricing)
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=7),
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )
    raw = SimpleNamespace(headers=headers, parse=lambda: response)
    create = SimpleNamespace(create=lambda **kwargs: raw)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=create))
    )
    return client


def test_a_reply_that_is_not_json_raises_with_the_usage_of_the_request_that_was_paid_for():
    client = _client({"x-litellm-response-cost": "0.002"})
    with pytest.raises(UnusableReply) as error:
        client.complete_json("system", "user")
    assert isinstance(error.value, ValueError)  # the callers that retry on ValueError still do
    assert (error.value.usage.calls, error.value.usage.prompt_tokens) == (1, 100)
    assert error.value.usage.cost_usd == pytest.approx(0.002)
    assert usage_of(error.value) is error.value.usage and usage_of(ValueError("x")) == LLMUsage()


def test_the_cost_of_the_endpoint_is_used_as_is():
    usage = (
        _client({"x-litellm-response-cost": "0.002"}, TokenPricing(2.5, 2.5), "{}")
        .complete_json("s", "u")
        .usage
    )
    assert usage.cost_usd == pytest.approx(0.002)
    assert (usage.estimated_cost_calls, usage.unpriced_calls) == (0, 0)


def test_a_call_with_no_cost_header_is_priced_with_the_configured_prices_and_flagged():
    usage = _client({}, TokenPricing(2.5, 2.5), "{}").complete_json("s", "u").usage
    assert usage.cost_usd == pytest.approx(107 * 2.5 / 1e6)
    assert (usage.estimated_cost_calls, usage.unpriced_calls) == (1, 0)


def test_a_call_with_no_cost_at_all_is_flagged_and_the_total_is_a_lower_bound():
    unpriced = _client({}, None, "{}").complete_json("s", "u").usage
    assert unpriced.cost_usd is None and unpriced.unpriced_calls == 1
    total = unpriced + PAID + unpriced
    assert total.cost_usd == pytest.approx(0.001) and total.unpriced_calls == 2 and total.calls == 3


def test_the_flags_are_summed():
    estimated = LLMUsage(calls=1, cost_usd=0.5, estimated_cost_calls=1)
    assert (estimated + estimated + PAID).estimated_cost_calls == 2


# --- the extractors that ask again ------------------------------------------------------


def _goal():
    return GoalClaimRelationExtractor(NotJsonLLM()), lambda e: e.evaluate(GOAL, [CLAIM_B])


def _claims():
    return ClaimRelationExtractor(NotJsonLLM()), lambda e: e.relate([CLAIM_A], [CLAIM_B])


def _task_claims():
    return TaskClaimRelationExtractor(NotJsonLLM()), lambda e: e.evaluate(TASK, [CLAIM_B])


def _tasks():
    return TaskRelationExtractor(NotJsonLLM()), lambda e: e.relate(TASK, [GOAL])


@pytest.mark.parametrize("make", [_goal, _claims, _task_claims, _tasks])
def test_both_requests_of_a_reply_that_is_never_usable_are_counted(make):
    extractor, run = make()
    with pytest.raises(UnusableReply):
        run(extractor)
    assert extractor._llm.requests == 2
    assert extractor.last_usage.calls == 2 and extractor.last_usage.prompt_tokens == 200
    assert extractor.last_usage.cost_usd == pytest.approx(0.002)
