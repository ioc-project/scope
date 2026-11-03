#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Stateful trajectory evaluation as an importable library."""

from typing import TYPE_CHECKING, Any

from stateful_evals_be import llm_client as _llm_client

# Before anything imports metrics_computation_engine: its LLMClient would load LiteLLM.
_llm_client.install()

if TYPE_CHECKING:
    from stateful_evals_be.api import (
        EvaluationEngine,
        LLMJudgeConfig,
        SessionResult,
        SpanRecord,
        TemporalMetricOptions,
        TemporalMetricsProcessor,
        available_metrics,
        evaluate_file,
        evaluate_spans,
        session_result_payload,
    )

__all__ = [
    "EvaluationEngine",
    "LLMJudgeConfig",
    "SessionResult",
    "SpanRecord",
    "TemporalMetricOptions",
    "TemporalMetricsProcessor",
    "available_metrics",
    "evaluate_file",
    "evaluate_spans",
    "session_result_payload",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from stateful_evals_be import api

        value = getattr(api, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
