"""Minimal LLM access: one JSON-returning call against an OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from dotenv import find_dotenv, load_dotenv

from symbolic_extractor.usage import LLMUsage, TokenPricing

#: Header set by a LiteLLM proxy with the cost (USD) of the call, when it knows the model price.
LITELLM_COST_HEADER = "x-litellm-response-cost"


@dataclass
class LLMReply:
    """Parsed JSON object returned by the LLM, with the usage of that call."""

    data: dict[str, Any]
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(calls=1))


class UnusableReply(ValueError):
    """The LLM answered but the reply is not a JSON object. The request was made and paid for, so the
    exception carries its ``usage``: a caller that retries must add it (``usage_of``)."""

    def __init__(self, message: str, usage: LLMUsage) -> None:
        super().__init__(message)
        self.usage = usage


def usage_of(exc: BaseException) -> LLMUsage:
    """The usage of the request that failed with ``exc`` (zero for an error that has none)."""
    return getattr(exc, "usage", None) or LLMUsage()


@dataclass
class TextReply:
    """Plain text returned by the LLM, with the usage of that call."""

    text: str
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(calls=1))


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str) -> LLMReply:
        """Send a system + user prompt and return the parsed JSON object of the reply."""
        ...


class TextLLMClient(Protocol):
    def complete_text(self, system: str, user: str) -> TextReply:
        """Send a system + user prompt and return the reply as plain text."""
        ...


class OpenAIChatClient:
    """``LLMClient`` for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        temperature: float = 0.0,
        pricing: TokenPricing | None = None,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._temperature = temperature
        self._pricing = pricing

    @classmethod
    def from_env(cls, model: str | None = None) -> OpenAIChatClient:
        """Resolve settings from the environment (a ``.env`` file is loaded if present).

        * API key: ``OPENAI_API_KEY``, else ``LLM_API_KEY``
        * Base URL: ``LLM_BASE_URL``, else ``OPENAI_BASE_URL``, else OpenAI
        * Model: ``model`` if given, else ``LLM_MODEL_NAME`` (default ``gpt-4o``)
        * Prices (optional, USD per million tokens): ``LLM_INPUT_COST_PER_1M`` and
          ``LLM_OUTPUT_COST_PER_1M``. Fallback used only when the proxy does not report the
          cost of a call (LiteLLM ``x-litellm-response-cost`` header); if neither is
          available the cost is None.
        """
        load_dotenv(find_dotenv(usecwd=True))
        input_price = os.getenv("LLM_INPUT_COST_PER_1M")
        output_price = os.getenv("LLM_OUTPUT_COST_PER_1M")
        pricing = (
            TokenPricing(float(input_price), float(output_price))
            if input_price and output_price
            else None
        )
        return cls(
            pricing=pricing,
            api_key=os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "",
            model=model or os.getenv("LLM_MODEL_NAME", "gpt-4o"),
            base_url=os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1",
        )

    def complete_json(self, system: str, user: str) -> LLMReply:
        text, usage = self._complete(system, user, json_mode=True)
        try:
            data = parse_json_object(text)
        except ValueError as exc:
            raise UnusableReply(str(exc), usage) from exc
        return LLMReply(data=data, usage=usage)

    def complete_text(self, system: str, user: str) -> TextReply:
        text, usage = self._complete(system, user, json_mode=False)
        return TextReply(text=text, usage=usage)

    def _complete(self, system: str, user: str, *, json_mode: bool) -> tuple[str, LLMUsage]:
        options: dict[str, Any] = {}
        if json_mode:
            options["response_format"] = {"type": "json_object"}
        raw = self._client.chat.completions.with_raw_response.create(
            model=self._model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **options,
        )
        response = raw.parse()

        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        # Cost priority: 1. what the proxy reports, 2. configured prices, 3. unknown (None).
        cost = _header_cost(raw.headers.get(LITELLM_COST_HEADER))
        estimated = False
        if cost is None and self._pricing is not None:
            cost = self._pricing.cost(prompt_tokens, completion_tokens)
            estimated = True

        usage = LLMUsage(
            calls=1,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            estimated_cost_calls=1 if estimated else 0,
            unpriced_calls=1 if cost is None else 0,
        )
        return response.choices[0].message.content or "", usage


def _header_cost(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM reply.

    Strict parsing first. Some models served through proxies do not produce clean JSON
    even in JSON mode: a markdown code fence, text around the object, or a stray prefix
    such as ``{"{ "claims": [...]}``. In those cases fall back to the first position in
    the text where a complete JSON object parses.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _first_json_object(text)
        if parsed is None:
            raise ValueError(f"No JSON object found in the LLM reply: {text[:300]!r}") from None

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object from the LLM, got {type(parsed).__name__}")
    return parsed


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return None
