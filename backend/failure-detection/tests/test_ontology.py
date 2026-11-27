"""The detectors read the neurosymbolic ontology: its labels, relations and properties."""

from conftest import NODES
from scope_ontology.models import edges as ontology_edges
from scope_ontology.models import nodes as ontology_nodes

from failure_detection import EDGE_TYPES, ELEMENT_LABELS
from failure_detection.state import RELATIONS


def test_every_label_is_a_node_of_the_ontology():
    for label in ELEMENT_LABELS:
        assert hasattr(ontology_nodes, label), label


def test_every_relation_is_an_edge_of_the_ontology():
    for relation in EDGE_TYPES:
        assert hasattr(ontology_edges, relation), relation
    assert set(RELATIONS.values()) - {"contradicts"} <= set(RELATIONS), "renamed relations"


def test_the_fixture_nodes_are_valid_ontology_nodes():
    for id, label, props in NODES:
        getattr(ontology_nodes, label)(id=id, **props)
