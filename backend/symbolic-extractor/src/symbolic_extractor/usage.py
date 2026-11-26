"""Token and cost accounting for the LLM calls made by the extractors."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, computed_field


class LLMUsage(BaseModel):
    """Tokens and cost of one or more LLM calls.

    ``cost_usd`` is None when the cost is unknown (no price configured and the endpoint
    did not report one). When summing, unknown costs are skipped, so the sum is None only
    if no call has a known cost. ``estimated_cost_calls`` counts the calls whose cost is not
    the one the endpoint reported but computed from the configured prices, and
    ``unpriced_calls`` those with no cost at all: when either is not 0 the total is not the
    endpoint's figure (an estimate, or a lower bound).
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    estimated_cost_calls: int = 0
    unpriced_calls: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: LLMUsage) -> LLMUsage:
        costs = [c for c in (self.cost_usd, other.cost_usd) if c is not None]
        return LLMUsage(
            calls=self.calls + other.calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=sum(costs) if costs else None,
            estimated_cost_calls=self.estimated_cost_calls + other.estimated_cost_calls,
            unpriced_calls=self.unpriced_calls + other.unpriced_calls,
        )


@dataclass(frozen=True)
class TokenPricing:
    """Price in USD per million tokens."""

    input_per_1m: float
    output_per_1m: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.input_per_1m + completion_tokens * self.output_per_1m
        ) / 1_000_000
