#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""``ingest_insights`` validates an Insight against its shape, which requires the ``concerns`` edge to its target."""

import datetime
from unittest.mock import MagicMock

import pytest
from scope_ontology import OntologyValidationError
from scope_ontology.models.nodes.insight import Insight

from scope.client import dal


def _insight(**overrides) -> Insight:
    fields = {
        "id": "insight-1",
        "dataType": "text",
        "templateId": "high_cost_agent",
        "description": "Agent X spent 3x the median cost.",
        "scope": "Agent",
        "labels": "[]",
        "targetNodeId": "agent-x",
        "createdAt": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    }
    fields.update(overrides)
    return Insight(**fields)


@pytest.fixture()
def graph(monkeypatch):
    nodes, rels = [], []
    monkeypatch.setattr(
        dal, "_ingest_node", lambda db, label, props: nodes.append(props)
    )
    monkeypatch.setattr(dal, "_create_rel", lambda *args: rels.append(args))
    return nodes, rels


def test_an_insight_with_a_target_is_written_and_attached(graph):
    nodes, rels = graph
    db = MagicMock()

    dal.ingest_insights(db, [_insight()])

    assert [n["id"] for n in nodes] == ["insight-1"]
    assert rels[0][1:4] == ("Agent", ["agentName"], ["agent-x"])
    assert rels[0][-1] == "hasInsight"
    db.execute.assert_called_once()  # clean-up of the outdated insights of this template


def test_an_insight_without_a_target_is_rejected(graph):
    nodes, _ = graph

    with pytest.raises(OntologyValidationError):
        dal.ingest_insights(MagicMock(), [_insight(targetNodeId="")])

    assert nodes == []
