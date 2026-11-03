#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest
from dem.intelligence import load_templates_from_disk

from intelligence_worker.wrapper.intelligence_wrapper import _build_insight

CATALOG = Path(__file__).resolve().parents[3] / "intelligence-catalog"


@pytest.fixture(scope="module")
def templates():
    return {t.scope + ":" + t.nameTemplate: t for t in load_templates_from_disk(CATALOG)}


def _find(templates, scope, text):
    return next(t for key, t in templates.items() if key.startswith(scope) and text in t.nameTemplate)


def test_the_cognitive_failure_templates_are_valid_and_loaded(templates):
    assert _find(templates, "Session", "detected in session")
    assert _find(templates, "SemanticGroup", "Recurring")


def test_a_session_insight_names_the_failure_and_takes_its_priority_from_the_confidence(templates):
    template = _find(templates, "Session", "detected in session")
    row = {
        "sessionId": "s1",
        "code": "IR-1",
        "failureName": "Context Drift",
        "confidence": "high",
        "reasoning": "Instruction lost",
        "occurrences": 2,
        "priority": "P1",
    }

    insight = _build_insight(template, row)

    assert insight.name == "Context Drift (IR-1) detected in session s1"
    assert insight.priority == "P1"
    assert insight.targetNodeId == "s1"
    assert "Instruction lost" in insight.description
    assert "IR-1" in insight.labels


def test_two_failures_of_one_session_are_two_insights(templates):
    template = _find(templates, "Session", "detected in session")
    base = {
        "sessionId": "s1",
        "failureName": "x",
        "confidence": "low",
        "reasoning": "r",
        "occurrences": 1,
        "priority": "P3",
    }

    first = _build_insight(template, {**base, "code": "IR-1"})
    second = _build_insight(template, {**base, "code": "IC-2"})

    assert first.id != second.id
