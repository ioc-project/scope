#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Metric results are stored as the ontology's MetricResult, with the evaluation cost."""

import pytest
from oxp_ontology.models.nodes import MetricResult as OntologyMetricResult
from pydantic import ValidationError

from oxp.client.local import LocalClient
from oxp.interfaces.models import MetricResult
from oxp.providers import OXPMetricsProvider
from oxp.query_builders import metrics as metrics_qb

COST_METADATA = {
    "evaluation_model": "gpt-4o",
    "evaluation_prompt_tokens": 1000,
    "evaluation_completion_tokens": 500,
    "evaluation_total_tokens": 1500,
    "evaluation_cost_usd": 0.0075,
}
COST_FIELDS = {
    "evaluationModel": "gpt-4o",
    "evaluationPromptTokenCount": 1000,
    "evaluationCompletionTokenCount": 500,
    "evaluationTotalTokenCount": 1500,
    "evaluationCost": 0.0075,
}
MODEL_FIELDS = set(OntologyMetricResult.model_fields)


class _Db:
    def __init__(self):
        self.commands = []

    def ensure_connected(self):
        return None

    def execute_command(self, query, params):
        self.commands.append((query, params))


def _result(**kwargs):
    defaults = dict(
        metric_id="Groundedness", resource_id="s1", provider="Native", value=0.75
    )
    return MetricResult(**{**defaults, **kwargs})


# ---------------------------------------------------------------- the unified model


def test_every_metric_has_the_same_fields_as_the_ontology_model():
    judged = metrics_qb.metric_result_properties(
        "Groundedness", "s1", 0.75, "ok", dict(COST_METADATA)
    )
    deterministic = metrics_qb.metric_result_properties(
        "ToolErrorRate", "s1", 0.0, "0/4 failed", {"raw": 1}
    )
    assert set(judged) == set(deterministic) == MODEL_FIELDS


def test_a_judged_metric_carries_its_evaluation_cost():
    props = metrics_qb.metric_result_properties(
        "Groundedness", "s1", 0.75, "ok", dict(COST_METADATA)
    )
    assert {k: props[k] for k in COST_FIELDS} == COST_FIELDS
    assert (props["name"], props["result"], props["reasoning"]) == (
        "Groundedness",
        0.75,
        "ok",
    )


def test_a_metric_without_llm_calls_carries_the_model_defaults():
    props = metrics_qb.metric_result_properties("ToolErrorRate", "s1", 0.0, "", None)
    assert props["evaluationModel"] == ""
    assert (
        props["evaluationPromptTokenCount"],
        props["evaluationCompletionTokenCount"],
        props["evaluationTotalTokenCount"],
    ) == (0, 0, 0)
    assert props["evaluationCost"] == 0.0


def test_partial_cost_metadata_fills_the_rest_with_defaults():
    props = metrics_qb.metric_result_properties(
        "X", "s1", 1.0, "", {"evaluation_total_tokens": 5, "evaluation_cost_usd": None}
    )
    assert props["evaluationTotalTokenCount"] == 5
    assert props["evaluationCost"] == 0.0


def test_values_are_validated_and_coerced_by_the_model():
    props = metrics_qb.metric_result_properties(
        "X",
        "s1",
        "0.5",
        "",
        {"evaluation_prompt_tokens": "1000", "evaluation_cost_usd": "0.0075"},
    )
    assert (
        props["result"] == 0.5
        and props["evaluationPromptTokenCount"] == 1000
        and props["evaluationCost"] == 0.0075
    )


@pytest.mark.parametrize(
    ("value", "metadata"),
    [
        ("high", {}),  # result is not a number
        (None, {}),
        (0.5, {"evaluation_cost_usd": "free"}),
        (0.5, {"evaluation_prompt_tokens": 12.5}),  # a fractional token count
        (0.5, {"evaluation_model": ["gpt-4o"]}),
    ],
)
def test_observations_that_do_not_fit_the_model_are_rejected(value, metadata):
    with pytest.raises(ValidationError):
        metrics_qb.metric_result_properties("X", "s1", value, "", metadata)


def test_unrelated_metadata_is_not_stored():
    props = metrics_qb.metric_result_properties(
        "X", "s1", 1.0, "", {"raw_score": 4.0, "scale": "1-5", "scores": [1, 2]}
    )
    assert set(props) == MODEL_FIELDS


def test_ids_are_stable_per_metric_and_resource():
    first = metrics_qb.metric_result_properties("Groundedness", "s1", 0.1, "", None)[
        "id"
    ]
    again = metrics_qb.metric_result_properties("Groundedness", "s1", 0.9, "", None)[
        "id"
    ]
    other_resource = metrics_qb.metric_result_properties(
        "Groundedness", "s2", 0.1, "", None
    )["id"]
    other_metric = metrics_qb.metric_result_properties(
        "Relevancy", "s1", 0.1, "", None
    )["id"]
    assert first == again
    assert len({first, other_resource, other_metric}) == 3
    assert first.startswith("metricresult:")


# ---------------------------------------------------------------- queries


def test_every_write_query_sets_the_ontology_properties():
    props = metrics_qb.metric_result_properties(
        "Groundedness", "s1", 0.75, "ok", dict(COST_METADATA)
    )
    for query, params in (
        metrics_qb.write_generic_metric_query(
            "s1", "Groundedness", 0.75, ontology=props
        ),
        metrics_qb.write_span_metric_query(
            "s1", "sp1", "Groundedness", 0.75, ontology=props
        ),
        metrics_qb.write_session_metric_query(
            "s1", "Groundedness", 0.75, ontology=props
        ),
    ):
        assert "SET m += $ontology" in query
        assert params["ontology"] == props


def test_queries_without_ontology_properties_pass_an_empty_map():
    _, params = metrics_qb.write_generic_metric_query("s1", "Groundedness", 0.75)
    assert params["ontology"] == {}


# ---------------------------------------------------------------- provider


def test_save_metrics_stores_the_model_with_the_evaluation_cost():
    db = _Db()
    assert (
        OXPMetricsProvider(db=db).save_metrics(
            [_result(metadata=dict(COST_METADATA), reasoning="ok")]
        )
        is True
    )
    ((_, params),) = db.commands
    assert set(params["ontology"]) == MODEL_FIELDS
    assert {k: params["ontology"][k] for k in COST_FIELDS} == COST_FIELDS
    assert params["metric_value"] == params["ontology"]["result"] == 0.75
    assert params["reasoning"] == "ok"


def test_save_metrics_without_cost_metadata_stores_the_defaults():
    db = _Db()
    OXPMetricsProvider(db=db).save_metrics(
        [_result(metadata={"raw_score": 4.0, "scale": "1-5"})]
    )
    assert set(db.commands[0][1]["ontology"]) == MODEL_FIELDS
    assert db.commands[0][1]["ontology"]["evaluationCost"] == 0.0


def test_save_span_metrics_stores_the_same_model():
    db = _Db()
    OXPMetricsProvider(db=db).save_span_metrics(
        "s1", "sp1", [_result(metadata=dict(COST_METADATA))]
    )
    assert {k: db.commands[0][1]["ontology"][k] for k in COST_FIELDS} == COST_FIELDS
    assert (
        db.commands[0][1]["ontology"]["id"]
        != metrics_qb.metric_result_properties("Groundedness", "s1", 0.75, "", None)[
            "id"
        ]
    )


def test_a_result_that_does_not_fit_the_model_is_not_saved_and_the_rest_still_are():
    db = _Db()
    bad = _result(metric_id="Bad", metadata={"evaluation_cost_usd": "free"})
    good = _result(metric_id="Good", metadata=dict(COST_METADATA))
    assert OXPMetricsProvider(db=db).save_metrics([bad, good]) is False
    assert [params["metric_name"] for _, params in db.commands] == ["Good"]


def test_a_non_numeric_value_is_not_saved():
    db = _Db()
    assert OXPMetricsProvider(db=db).save_metrics([_result(value="high")]) is False
    assert db.commands == []


def test_failed_results_are_not_persisted():
    """A failed metric is skipped entirely, so a failure never becomes a stored 0."""
    db = _Db()
    failed = _result(
        value=0.0, error="No conversation data", metadata=dict(COST_METADATA)
    )
    assert OXPMetricsProvider(db=db).save_metrics([failed]) is True
    assert db.commands == []


# ---------------------------------------------------------------- client payloads


def _client(db):
    return LocalClient(db=db, metrics_provider=OXPMetricsProvider(db=db))


def test_write_span_metrics_forwards_the_cost_from_the_payload():
    db = _Db()
    payload = [
        {
            "name": "mdt.Relevancy",
            "value": 0.5,
            "provider": "stateful_evals",
            "source": "StatefulEval",
            **COST_METADATA,
        }
    ]
    result = _client(db).write_span_metrics("s1", "sp1", payload)
    assert result == {"written": 1, "errors": []}
    assert {k: db.commands[0][1]["ontology"][k] for k in COST_FIELDS} == COST_FIELDS


def test_write_session_metrics_forwards_the_cost_from_the_payload():
    db = _Db()
    payload = [
        {
            "name": "trajectory_score",
            "value": 1,
            "provider": "stateful_evals",
            "source": "StatefulEval",
            **COST_METADATA,
        }
    ]
    _client(db).write_session_metrics("s1", payload)
    assert {k: db.commands[0][1]["ontology"][k] for k in COST_FIELDS} == COST_FIELDS


def test_payloads_without_cost_still_write_the_full_model():
    db = _Db()
    _client(db).write_span_metrics(
        "s1", "sp1", [{"name": "mdt.Relevancy", "value": 1.0}]
    )
    assert set(db.commands[0][1]["ontology"]) == MODEL_FIELDS
