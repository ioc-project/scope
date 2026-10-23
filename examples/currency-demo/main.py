#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""A currency conversion done by two agents, instrumented with the AGNTCY Observe SDK.

A rate provider agent looks up the exchange rate, then hands over to the assistant agent, which
converts the amount. There is no model call, so no API key is needed. Each run is one session
that SCOPE reconstructs from the exported spans.

    OTLP_HTTP_ENDPOINT=http://localhost:4318 uv run main.py 100
"""

import os
import sys

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph, tool
from ioa_observe.sdk.tracing import session_start

RATES = {"EUR": 0.859}  # example rate


@tool(name="lookup_rate", description="Looks up the USD exchange rate of a currency")
def lookup_rate(currency: str) -> float:
    return RATES[currency]


@tool(name="convert_currency", description="Converts an amount with an exchange rate, rounded to cents")
def convert_currency(amount: float, rate: float) -> float:
    return round(amount * rate, 2)


@agent(name="rate_provider", description="Looks up the exchange rate for the requested currency")
def rate_provider(currency: str) -> float:
    return lookup_rate(currency)


@agent(name="currency_assistant", description="Converts the amount with the rate it is handed and writes the answer")
def assistant(amount: float, rate: float) -> str:
    converted = convert_currency(amount, rate)
    return f"{amount:.2f} USD = {converted:.2f} EUR (example rate)"


@graph(name="currency_conversion", description="Converts US dollars to euros with two cooperating agents")
def run(amount: float) -> str:
    return assistant(amount, rate_provider("EUR"))


def handle_request(amount: float) -> str:
    with session_start():  # one session per independent request
        return run(amount)


if __name__ == "__main__":
    Observe.init("currency-demo", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318"))
    print(handle_request(float(sys.argv[1]) if len(sys.argv) > 1 else 100))
