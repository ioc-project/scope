"""Deciding whether two symbolic state elements are the same one (same claim, same goal, ...).

``ElementComparator`` is the interface: ``same_probability(a, b)`` is the probability, in
[0, 1], that ``a`` and ``b`` express the same thing. The base class knows nothing, so it
always answers 0. Subclasses bring a signal: ``EmbeddingComparator`` uses the distance between
the embeddings of the elements (see ``symbolic_extractor.embeddings``).

For now every element is assumed to have the two embedded fields ``claim_text`` and
``verbatim_source``, whatever its type. Variations per element type can be added once other
kinds exist.
"""

from __future__ import annotations

import math
from typing import Any

from symbolic_extractor.embeddings import EmbeddingSet
from symbolic_extractor.models import ExtractionResult
from symbolic_extractor.store import SymbolicSessionStore

#: Default minimum ``same_probability`` for an input claim to be listed in ``same_claims``.
DEFAULT_SAME_CLAIM_THRESHOLD = 0.7
#: Default combined cosine similarity at which ``EmbeddingComparator`` gives probability 0.5.
DEFAULT_SIMILARITY_THRESHOLD = 0.85


class ElementComparator:
    """Probability that two symbolic state elements are the same. The base answer is always 0."""

    def same_probability(self, a: Any, b: Any) -> float:
        return 0.0


class EmbeddingComparator(ElementComparator):
    """Same-ness from the cosine similarity of the elements' embeddings.

    The similarities of the two fields are combined, not used in isolation::

        similarity = text_weight * cos(claim_text) + (1 - text_weight) * cos(verbatim_source)

    The text is the main signal. The snippet only says which wording a claim comes from, so
    two different claims taken from one sentence have identical snippets; it confirms a match
    but must not carry it. When either element lacks the snippet vector, the text similarity
    alone is used.

    The similarity is mapped to a probability with a logistic curve centred on ``threshold``
    (probability 0.5 there), ``steepness`` setting how sharp the switch is. The defaults are a
    starting point, not calibrated: tune them on labelled pairs.

    Elements are looked up by ``element.id`` in the given ``EmbeddingSet``; asking about an
    element that has no embeddings is an error (it would otherwise look like "different").
    """

    TEXT_FIELD = "claim_text"
    SNIPPET_FIELD = "verbatim_source"

    def __init__(
        self,
        embeddings: EmbeddingSet,
        text_weight: float = 0.7,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        steepness: float = 20.0,
    ) -> None:
        if not 0.0 <= text_weight <= 1.0:
            raise ValueError(f"text_weight must be in [0, 1], got {text_weight}")
        if steepness <= 0:
            raise ValueError(f"steepness must be positive, got {steepness}")
        self.text_weight = text_weight
        self.threshold = threshold
        self.steepness = steepness
        self.model = embeddings.model
        self._vectors = {
            e.element_id: {f: _unit(v) for f, v in e.embeddings.items()}
            for e in embeddings.elements
        }

    def similarity(self, a: Any, b: Any) -> float | None:
        """Combined cosine similarity of ``a`` and ``b``, or None when they have no embedded
        text to compare (the text vector is what a comparison needs)."""
        va, vb = self._vectors_of(a), self._vectors_of(b)
        text_a, text_b = va.get(self.TEXT_FIELD), vb.get(self.TEXT_FIELD)
        if text_a is None or text_b is None:
            return None
        text = _dot(text_a, text_b)
        snippet_a, snippet_b = va.get(self.SNIPPET_FIELD), vb.get(self.SNIPPET_FIELD)
        if snippet_a is None or snippet_b is None:
            return text
        snippet = _dot(snippet_a, snippet_b)
        return self.text_weight * text + (1.0 - self.text_weight) * snippet

    def same_probability(self, a: Any, b: Any) -> float:
        similarity = self.similarity(a, b)
        if similarity is None:
            return 0.0
        z = self.steepness * (similarity - self.threshold)
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))

    def _vectors_of(self, element: Any) -> dict[str, list[float]]:
        element_id = getattr(element, "id", None)
        if element_id not in self._vectors:
            raise KeyError(f"No embeddings for element {element_id!r}")
        return self._vectors[element_id]


def annotate_same_claims(
    result: ExtractionResult,
    store: SymbolicSessionStore,
    comparator: ElementComparator,
    threshold: float = DEFAULT_SAME_CLAIM_THRESHOLD,
) -> int:
    """Add ``same_claims`` to every claim of ``result``: the ids of the input claims of its
    LLM call whose ``same_probability`` with the claim is above ``threshold``, best first.

    LLM calls whose input claims were not computed (``input_claims`` is None) are left
    untouched; the others always get the list, possibly empty. Matches are also recorded in
    ``store`` as ``same_claim`` relations (claim -> input claim). Returns the number of links.
    """
    links = 0
    for node in result.nodes:
        if node.input_claims is None:
            continue
        inputs = [s.element for i in node.input_claims if (s := store.get(i)) is not None]
        for claim in node.symbolic_state_elements.get("claims", []):
            subject = store.get(claim["id"])
            if subject is None:
                continue
            scored = [
                (comparator.same_probability(subject.element, other), other.id)
                for other in inputs
                if other.id != subject.id
            ]
            matches = sorted((s for s in scored if s[0] > threshold), key=lambda s: -s[0])
            claim["same_claims"] = [other_id for _, other_id in matches]
            for _, other_id in matches:
                store.link("same_claim", subject.id, other_id)
            links += len(matches)
    return links


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def _dot(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"Embedding sizes differ ({len(a)} vs {len(b)}): different models?")
    return sum(x * y for x, y in zip(a, b, strict=True))
