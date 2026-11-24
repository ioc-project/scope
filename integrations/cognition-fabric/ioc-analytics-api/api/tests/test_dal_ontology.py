from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scope_ontology import OntologyValidationError
from scope_ontology.models.nodes.consistency_report import ConsistencyReport

from scope.client.dal import ingest_consistency_report
from scope.connectors.neo4j import Neo4JConnector


def test_ingest_metric_report_validates_ontology_relationships() -> None:
    report = ConsistencyReport(
        id="validation-test",
        dataType="metric",
        mean=0.87,
        confidenceInterval="{}",
        confidenceIndicator="high",
    )
    db = Mock(spec=Neo4JConnector)
    with pytest.raises(OntologyValidationError):
        ingest_consistency_report(db, "group-1", report)
    db.execute.assert_not_called()

    source = SimpleNamespace(metadata={"metric": "Cost"})
    assert ingest_consistency_report(db, "group-1", report, source=source)
