#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Estimate the USD cost of LLM calls from token counts and a local price table.

``data/model_prices.json`` is a trimmed copy of the LiteLLM-style price list
(input/output cost per token and aliases per model). A model with no entry is
priced like ``gpt-4o``, so a cost is always an estimate, never a billed amount.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_PRICE_FILE = Path(__file__).resolve().parent / "data" / "model_prices.json"
DEFAULT_PRICING_MODEL = "gpt-4o"

# model name -> (input cost per token, output cost per token)
_Price = tuple[float, float]


@lru_cache(maxsize=1)
def _price_table() -> dict[str, _Price]:
    try:
        raw = json.loads(_PRICE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Unable to load model price table: %s", _PRICE_FILE)
        return {}
    table: dict[str, _Price] = {}
    for name, meta in raw.items():
        price = (meta["input_cost_per_token"], meta["output_cost_per_token"])
        for alias in (name, *meta.get("aliases", ())):
            table[alias.strip().lower()] = price
    return table


def resolve_model_price(model: str) -> _Price | None:
    """Price of ``model`` by exact name or alias, also without a provider prefix."""
    table = _price_table()
    key = (model or "").strip().lower()
    for candidate in (key, key.rsplit("/", 1)[-1]):
        if candidate in table:
            return table[candidate]
    return None


def estimate_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Estimated USD cost, or ``None`` when no price (not even the default) exists."""
    price = resolve_model_price(model)
    if price is None:
        price = resolve_model_price(DEFAULT_PRICING_MODEL)
        if price is None:
            return None
        logger.debug(
            "No price for model %r; using %r pricing", model, DEFAULT_PRICING_MODEL
        )
    return round(prompt_tokens * price[0] + completion_tokens * price[1], 8)
