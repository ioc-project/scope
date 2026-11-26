import pytest

from symbolic_extractor import SymbolicExtractor, SymbolicSessionStore
from symbolic_extractor.extractors import ClaimExtractionPrompt, ElementExtractor
from symbolic_extractor.extractors.claims import Claim
from symbolic_extractor.llm import LLMReply
from symbolic_extractor.models import SessionData
from symbolic_extractor.usage import LLMUsage


def _claim(claim_id, node_id, text="c"):
    return Claim(
        id=claim_id,
        claim_text=text,
        claim_force="assertive",
        claim_kind="factual",
        generated_by=node_id,
    )


def test_elements_are_returned_by_node_with_their_attributes():
    store = SymbolicSessionStore("s1")
    store.add("claims", _claim("c1", "l1", "A"), "l1")
    store.add("claims", _claim("c2", "l1", "B"), "l1")
    store.add("claims", _claim("c3", "l2", "C"), "l2")

    found = store.for_node("l1")
    assert [s.id for s in found] == ["c1", "c2"]  # insertion order
    assert [s.element.claim_text for s in found] == ["A", "B"]
    assert all(s.node_id == "l1" and s.kind == "claims" for s in found)
    assert [s.id for s in store.claims_for_node("l2")] == ["c3"]
    assert store.for_node("unknown") == []


def test_filter_by_kind_and_lookup_by_id():
    store = SymbolicSessionStore("s1")
    store.add("claims", _claim("c1", "l1"), "l1")
    store.add("goals", _claim("g1", "l1"), "l1")  # any element with an id

    assert [s.id for s in store.for_node("l1", "goals")] == ["g1"]
    assert {s.kind for s in store.for_node("l1")} == {"claims", "goals"}
    assert store.get("c1").node_id == "l1"
    assert store.get("nope") is None
    assert "c1" in store and len(store) == 2
    assert sorted(store.kinds()) == ["claims", "goals"]
    assert [s.id for s in store.by_kind("claims")] == ["c1"]


def test_adding_the_same_id_again_replaces_instead_of_duplicating():
    store = SymbolicSessionStore("s1")
    store.add("claims", _claim("c1", "l1", "old"), "l1")
    store.add("claims", _claim("c1", "l1", "new"), "l1")

    assert len(store) == 1
    assert [s.element.claim_text for s in store.for_node("l1")] == ["new"]


def test_node_id_and_element_id_are_required():
    store = SymbolicSessionStore("s1")
    with pytest.raises(ValueError):
        store.add("claims", _claim("c1", None), None)
    with pytest.raises(ValueError):
        store.add("claims", _claim("", "l1"), "l1")


class _FakeLLM:
    def complete_json(self, system, user):
        reply = {"claims": [{"claim_text": "c", "claim_force": "assertive", "claim_kind": "factual"}]}
        return LLMReply(data=reply, usage=LLMUsage(calls=1))


def test_pipeline_fills_the_store():
    data = SessionData(
        session_id="s1",
        llm_spans=[{"id": "l1", "completion": "X is true."}, {"id": "l2", "completion": "Y is true."}],
        tool_spans=[{"id": "t1"}],
    )
    pipeline = SymbolicExtractor(extractors=[ElementExtractor(_FakeLLM(), ClaimExtractionPrompt())])

    result = pipeline.process(data)

    store = pipeline.store
    assert store is not None and store.session_id == "s1"
    assert len(store) == 2
    (stored,) = store.claims_for_node("l1")
    assert stored.element.generated_by == "l1"
    # the store holds exactly what the result lists for that node
    node = next(n for n in result.nodes if n.node_id == "l1")
    assert [c["id"] for c in node.symbolic_state_elements["claims"]] == [stored.id]
    assert store.for_node("t1") == []


def test_a_link_is_an_edge_and_a_repeated_link_is_not_new():
    store = SymbolicSessionStore("s1")
    assert store.link("supports", "c1", "t1") is True
    assert store.link("supports", "c1", "t1") is False
    assert store.link("supports", "c1", "t2") is True
    assert [(e.edge_type, e.source_id, e.target_id) for e in store.edges()] == [
        ("supports", "c1", "t1"),
        ("supports", "c1", "t2"),
    ]
    assert store.related("supports", "c1") == ["t1", "t2"] and store.related_from("supports", "t1") == ["c1"]


def test_an_edge_keeps_its_attributes_and_linking_again_updates_them():
    store = SymbolicSessionStore("s1")
    store.link("derived_from", "k2", "k1", strength="weak")
    store.link("derived_from", "k2", "k1", strength="strong", mode="deduction")
    (edge,) = store.edges()
    assert edge.attributes == {"strength": "strong", "mode": "deduction"}
    assert edge.model_dump() == {
        "edge_type": "derived_from",
        "source_id": "k2",
        "target_id": "k1",
        "attributes": {"strength": "strong", "mode": "deduction"},
    }


def test_edges_since_lists_only_the_new_edges():
    store = SymbolicSessionStore("s1")
    store.link("supports", "c1", "t1")
    mark = store.edge_count()
    store.link("supports", "c1", "t1")  # already there
    store.link("bounded_by", "t1", "k1")
    assert [(e.edge_type, e.target_id) for e in store.edges_since(mark)] == [("bounded_by", "k1")]
    assert store.edges_since(store.edge_count()) == []
