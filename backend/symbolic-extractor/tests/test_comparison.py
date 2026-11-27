import pytest

from symbolic_extractor.comparison import ElementComparator, EmbeddingComparator
from symbolic_extractor.embeddings import ElementEmbedding, EmbeddingSet
from symbolic_extractor.extractors.claims import Claim


def claim(cid):
    return Claim(
        id=cid, claim_text=cid, claim_force="assertive", claim_kind="factual", generated_by="l"
    )


def embedding_set(**elements):
    """elements: id=(text_vector, snippet_vector or None)."""
    return EmbeddingSet(
        session_id="s",
        model="fake",
        elements=[
            ElementEmbedding(
                element_id=eid,
                element_type="Claim",
                embeddings={"claim_text": text, **({"verbatim_source": snip} if snip else {})},
            )
            for eid, (text, snip) in elements.items()
        ],
    )


def test_base_comparator_always_answers_zero():
    comparator = ElementComparator()
    assert comparator.same_probability(claim("a"), claim("a")) == 0.0
    assert comparator.same_probability(claim("a"), claim("b")) == 0.0


def test_identical_vectors_are_almost_certainly_the_same():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], [0, 1]), b=([1, 0], [0, 1])))
    assert comparator.similarity(claim("a"), claim("b")) == pytest.approx(1.0)
    assert comparator.same_probability(claim("a"), claim("b")) > 0.9


def test_orthogonal_vectors_are_almost_certainly_different():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], [1, 0]), b=([0, 1], [0, 1])))
    assert comparator.similarity(claim("a"), claim("b")) == pytest.approx(0.0)
    assert comparator.same_probability(claim("a"), claim("b")) < 0.01


def test_symmetric_and_in_unit_interval():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0.2], [1, 1]), b=([0.9, 0.5], [0.2, 1])))
    ab = comparator.same_probability(claim("a"), claim("b"))
    assert ab == pytest.approx(comparator.same_probability(claim("b"), claim("a")))
    assert 0.0 <= ab <= 1.0


def test_fields_are_combined_with_the_weight():
    # text identical (cos 1), snippets orthogonal (cos 0)
    elements = {"a": ([1, 0], [1, 0]), "b": ([1, 0], [0, 1])}
    assert EmbeddingComparator(embedding_set(**elements), text_weight=0.7).similarity(
        claim("a"), claim("b")
    ) == pytest.approx(0.7)
    assert EmbeddingComparator(embedding_set(**elements), text_weight=1.0).similarity(
        claim("a"), claim("b")
    ) == pytest.approx(1.0)
    assert EmbeddingComparator(embedding_set(**elements), text_weight=0.0).similarity(
        claim("a"), claim("b")
    ) == pytest.approx(0.0)


def test_shared_snippet_alone_does_not_make_different_claims_the_same():
    # same sentence (snippet cos 1) holding two different claims (text cos 0.3)
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], [1, 0]), b=([0.3, 0.954], [1, 0])))
    assert comparator.same_probability(claim("a"), claim("b")) < 0.1


def test_text_only_when_a_snippet_is_missing():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], None), b=([1, 0], [0, 1])))
    assert comparator.similarity(claim("a"), claim("b")) == pytest.approx(1.0)


def test_no_text_vector_gives_zero_probability():
    embeddings = EmbeddingSet(
        session_id="s",
        model="fake",
        elements=[
            ElementEmbedding(element_id="a", element_type="Claim", embeddings={}),
            ElementEmbedding(
                element_id="b", element_type="Claim", embeddings={"claim_text": [1, 0]}
            ),
        ],
    )
    comparator = EmbeddingComparator(embeddings)
    assert comparator.similarity(claim("a"), claim("b")) is None
    assert comparator.same_probability(claim("a"), claim("b")) == 0.0


def test_unknown_element_is_an_error_not_a_zero():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], None)))
    with pytest.raises(KeyError):
        comparator.same_probability(claim("a"), claim("missing"))


def test_vectors_of_different_sizes_are_rejected():
    comparator = EmbeddingComparator(embedding_set(a=([1, 0], None), b=([1, 0, 0], None)))
    with pytest.raises(ValueError):
        comparator.same_probability(claim("a"), claim("b"))


def test_threshold_is_the_half_probability_point():
    # cos(text) = 0.8 exactly, no snippets
    comparator = EmbeddingComparator(
        embedding_set(a=([1, 0], None), b=([0.8, 0.6], None)), threshold=0.8
    )
    assert comparator.same_probability(claim("a"), claim("b")) == pytest.approx(0.5)


def test_invalid_parameters():
    with pytest.raises(ValueError):
        EmbeddingComparator(embedding_set(a=([1, 0], None)), text_weight=1.5)
    with pytest.raises(ValueError):
        EmbeddingComparator(embedding_set(a=([1, 0], None)), steepness=0)


class _FakeComparator(ElementComparator):
    def __init__(self, table):
        self.table = table

    def same_probability(self, a, b):
        return self.table.get((a.id, b.id), 0.0)


def _annotated_setup():
    from symbolic_extractor.models import AnnotatedNode, ExtractionResult
    from symbolic_extractor.store import SymbolicSessionStore

    store = SymbolicSessionStore("s")
    for cid, node in (("a", "l1"), ("b", "l1"), ("c", "l2"), ("d", "l2")):
        store.add("claims", claim(cid), node)
    dump = lambda cid: store.get(cid).element.model_dump(mode="json")  # noqa: E731
    result = ExtractionResult(
        session_id="s",
        nodes=[
            AnnotatedNode(
                node_type="LLMCall",
                node={"id": "l1"},
                symbolic_state_elements={"claims": [dump("a"), dump("b")]},
                input_claims=None,  # not computed: untouched
            ),
            AnnotatedNode(
                node_type="LLMCall",
                node={"id": "l2"},
                symbolic_state_elements={"claims": [dump("c"), dump("d")]},
                input_claims=["a", "b", "d"],  # d is a sibling claim, never itself
            ),
        ],
    )
    return result, store


def test_annotate_same_claims_lists_input_claims_above_threshold_best_first():
    from symbolic_extractor.comparison import annotate_same_claims

    result, store = _annotated_setup()
    comparator = _FakeComparator({("c", "a"): 0.8, ("c", "b"): 0.95, ("c", "d"): 0.71})

    links = annotate_same_claims(result, store, comparator, threshold=0.7)

    first, second = result.nodes
    assert all("same_claims" not in c for c in first.symbolic_state_elements["claims"])
    c, d = second.symbolic_state_elements["claims"]
    assert c["same_claims"] == ["b", "a", "d"]
    assert d["same_claims"] == []  # computed, nothing matches (and never itself)
    assert links == 3
    assert store.related("same_claim", "c") == ["b", "a", "d"]


def test_annotate_same_claims_threshold_is_strictly_above():
    from symbolic_extractor.comparison import annotate_same_claims

    result, store = _annotated_setup()
    annotate_same_claims(result, store, _FakeComparator({("c", "a"): 0.7, ("c", "b"): 0.9}))

    c = result.nodes[1].symbolic_state_elements["claims"][0]
    assert c["same_claims"] == ["b"]  # default threshold 0.7
