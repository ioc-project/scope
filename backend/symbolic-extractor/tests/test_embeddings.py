import numpy as np
import pytest

from symbolic_extractor.embeddings import (
    ELEMENT_FIELDS,
    ElementFields,
    EmbeddingSet,
    TextEmbedder,
    embed_result,
)
from symbolic_extractor.models import AnnotatedNode, ExtractionResult
from symbolic_extractor.output import embeddings_file, read_result_file, write_json


class FakeBackend:
    """Deterministic stand-in for a dem embedder; records what it was asked to encode."""

    model_id = "fake-embed"

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, sentences):
        self.calls.append(list(sentences))
        return np.array([[float(len(s)), float(s.count("a")), 1.0] for s in sentences])


def claim(cid, text, snippet):
    return {
        "id": cid,
        "claim_text": text,
        "claim_force": "assertive",
        "claim_kind": "factual",
        "verbatim_source": snippet,
        "generated_by": "llm-1",
    }


def result_with(*claims, node_id="llm-1", extra_kinds=None):
    elements = {"claims": list(claims)}
    elements.update(extra_kinds or {})
    return ExtractionResult(
        session_id="s1",
        nodes=[
            AnnotatedNode(
                node_type="LLMCall", node={"id": node_id}, symbolic_state_elements=elements
            )
        ],
    )


def test_embed_returns_one_vector_per_text_in_order():
    embedder = TextEmbedder(FakeBackend())
    vectors = embedder.embed(["a", "bbb", "cc"])
    assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]
    assert all(isinstance(x, float) for x in vectors[0])


def test_embed_skips_blank_and_none_and_dedups():
    backend = FakeBackend()
    embedder = TextEmbedder(backend)
    vectors = embedder.embed(["same", None, "  ", "same", "other", ""])
    assert backend.calls == [["same", "other"]]  # one request, no blanks, no duplicates
    assert vectors[1] is None and vectors[2] is None and vectors[5] is None
    assert vectors[0] == vectors[3] and vectors[0] is not None
    assert embedder.texts_embedded == 2


def test_embed_with_nothing_to_encode_does_not_call_backend():
    backend = FakeBackend()
    assert TextEmbedder(backend).embed([None, ""]) == [None, None]
    assert backend.calls == []


def test_embed_detects_a_backend_returning_the_wrong_number_of_rows():
    class Broken(FakeBackend):
        def encode(self, sentences):
            return np.zeros((1, 3))

    with pytest.raises(RuntimeError):
        TextEmbedder(Broken()).embed(["a", "b"])


def test_claims_get_one_vector_per_field_and_keep_ids_and_node():
    result = result_with(claim("c1", "Paris is nice", "Paris is nice, really."))
    embedded = embed_result(result, TextEmbedder(FakeBackend()))

    assert embedded.model == "fake-embed" and embedded.dimensions == 3
    (element,) = embedded.elements
    assert (element.element_id, element.element_type, element.node_id) == ("c1", "Claim", "llm-1")
    assert set(element.embeddings) == {"claim_text", "verbatim_source"}
    assert element.embeddings["claim_text"][0] == len("Paris is nice")
    assert element.embeddings["verbatim_source"][0] == len("Paris is nice, really.")


def test_claim_without_verbatim_source_only_has_a_text_vector():
    result = result_with(claim("c1", "Paris is nice", None))
    (element,) = embed_result(result, TextEmbedder(FakeBackend())).elements
    assert set(element.embeddings) == {"claim_text"}


def test_all_texts_are_sent_in_a_single_batch():
    backend = FakeBackend()
    result = result_with(claim("c1", "t1", "s1"), claim("c2", "t2", "s1"), claim("c3", "t3", None))
    embed_result(result, TextEmbedder(backend))
    assert len(backend.calls) == 1
    assert sorted(backend.calls[0]) == ["s1", "t1", "t2", "t3"]  # shared snippet embedded once


def test_unknown_element_kinds_are_skipped(caplog):
    result = result_with(
        claim("c1", "t", "s"), extra_kinds={"goals": [{"id": "g1", "goal_text": "x"}]}
    )
    embedded = embed_result(result, TextEmbedder(FakeBackend()))
    assert [e.element_id for e in embedded.elements] == ["c1"]
    assert "goals" in caplog.text


def test_a_new_element_kind_only_needs_a_registry_entry(monkeypatch):
    monkeypatch.setitem(ELEMENT_FIELDS, "goals", ElementFields("Goal", ("goal_text",)))
    result = result_with(
        claim("c1", "t", None), extra_kinds={"goals": [{"id": "g1", "goal_text": "reach Luminos"}]}
    )
    embedded = embed_result(result, TextEmbedder(FakeBackend()))
    goal = next(e for e in embedded.elements if e.element_id == "g1")
    assert goal.element_type == "Goal" and set(goal.embeddings) == {"goal_text"}


def test_empty_result_gives_an_empty_set():
    embedded = embed_result(ExtractionResult(session_id="s1"), TextEmbedder(FakeBackend()))
    assert embedded.elements == [] and embedded.dimensions is None


def test_embedding_set_file_round_trip(tmp_path):
    embedded = embed_result(
        result_with(claim("c1", "Paris", "Paris.")), TextEmbedder(FakeBackend())
    )
    path = write_json(embedded, embeddings_file(tmp_path, "s1"))
    assert path.name == "embeddings_s1.json"
    assert EmbeddingSet.model_validate_json(path.read_text()) == embedded


def test_read_result_file_round_trip(tmp_path):
    result = result_with(claim("c1", "Paris", "Paris."))
    path = write_json(result, tmp_path / "s1.json")
    assert read_result_file(path) == result


def test_embeddings_file_rejects_unsafe_session_ids(tmp_path):
    with pytest.raises(ValueError):
        embeddings_file(tmp_path, "../x")
