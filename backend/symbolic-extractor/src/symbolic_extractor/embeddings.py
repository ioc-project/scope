"""Text embeddings for the extracted symbolic state elements.

Two layers:

* ``TextEmbedder``: generic, strings in, vectors out. It runs on any backend with dem's
  ``encode(list[str]) -> array`` interface; ``TextEmbedder.from_env`` uses dem's
  ``OpenAIEmbedder`` against the same OpenAI-compatible proxy as the LLM (the embedding model
  comes from ``EMBEDDING_MODEL_NAME``). dem is imported lazily (with its ``embedding`` extra, which is API only, torch is not loaded).
* ``embed_result``: embeds the text fields of every symbolic state element of an
  ``ExtractionResult`` (for a Claim: ``claim_text`` and ``verbatim_source``, kept apart) and
  returns an ``EmbeddingSet``, written to its own file (``embeddings_<session_id>.json``)
  since vectors are too large for the extraction output.

Which fields are embedded for each element kind is in ``ELEMENT_FIELDS``; supporting a new
kind (goals, tasks, ...) is one entry there.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field

from symbolic_extractor.models import AnnotatedNode, ExtractionResult

logger = logging.getLogger(__name__)

#: Used when ``EMBEDDING_MODEL_NAME`` is not set; served by the LiteLLM proxy.
DEFAULT_EMBEDDING_MODEL = "azure/text-embedding-3-small"


@dataclass(frozen=True)
class ElementFields:
    """What to embed for one kind of symbolic state element."""

    element_type: str  # ontology class name, e.g. "Claim"
    fields: tuple[str, ...]  # element attributes whose text is embedded, one vector each


#: Element kind (key of ``AnnotatedNode.symbolic_state_elements``) -> fields to embed.
ELEMENT_FIELDS: dict[str, ElementFields] = {
    "claims": ElementFields("Claim", ("claim_text", "verbatim_source")),
}


#: Element kinds deliberately not embedded (no entry in ``ELEMENT_FIELDS``, and no warning).
NOT_EMBEDDED_KINDS = frozenset(
    {"facts", "system_prompts", "system_tasks", "constraints", "capabilities", "delegation_claims"}
)


class EncodeBackend(Protocol):
    """The part of dem's ``EmbeddingModel`` that is used."""

    model_id: str

    def encode(self, sentences: list[str]) -> Any:  # array-like, one row per sentence
        ...


class TextEmbedder:
    """Strings in, vectors out."""

    def __init__(self, backend: EncodeBackend) -> None:
        self._backend = backend
        #: Texts sent to the backend so far (after dropping blanks and duplicates).
        self.texts_embedded = 0

    @property
    def model_id(self) -> str:
        return self._backend.model_id

    @classmethod
    def from_env(cls) -> TextEmbedder:
        """Resolve settings from the environment (a ``.env`` file is loaded if present).

        Key and base URL are those of the LLM client (``OPENAI_API_KEY`` / ``LLM_API_KEY``,
        ``LLM_BASE_URL`` / ``OPENAI_BASE_URL``); the model is ``EMBEDDING_MODEL_NAME``.
        """
        load_dotenv(find_dotenv(usecwd=True))
        from dem.embedding import OpenAIEmbedder

        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise ValueError("No API key found (OPENAI_API_KEY / LLM_API_KEY) for embeddings")
        backend = OpenAIEmbedder(
            model_id=os.getenv("EMBEDDING_MODEL_NAME", DEFAULT_EMBEDDING_MODEL),
            base_url=os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            api_key=api_key,
        )
        return cls(backend)

    def embed(self, texts: Sequence[str | None]) -> list[list[float] | None]:
        """One vector per input text, in order. Blank or ``None`` texts give ``None`` (they
        are not sent); identical texts are embedded once."""
        cleaned = [t.strip() if isinstance(t, str) else "" for t in texts]
        unique = list(dict.fromkeys(t for t in cleaned if t))
        vectors: dict[str, list[float]] = {}
        if unique:
            rows = self._backend.encode(unique)
            if len(rows) != len(unique):
                raise RuntimeError(
                    f"Embedding backend returned {len(rows)} vectors for {len(unique)} texts"
                )
            vectors = {t: [float(x) for x in row] for t, row in zip(unique, rows, strict=True)}
            self.texts_embedded += len(unique)
        return [vectors.get(t) for t in cleaned]


class ElementEmbedding(BaseModel):
    """Embeddings of one symbolic state element."""

    element_id: str
    element_type: str  # "Claim"
    #: KG node the element refers to (the LLMCall for a Claim).
    node_id: str | None = None
    #: Element attribute -> vector of its text (``claim_text``, ``verbatim_source``). An
    #: attribute with no text (e.g. a claim without verbatim source) is absent.
    embeddings: dict[str, list[float]] = Field(default_factory=dict)


class EmbeddingSet(BaseModel):
    """Embeddings of the elements of one session; a file of its own."""

    session_id: str
    model: str
    dimensions: int | None = None
    elements: list[ElementEmbedding] = Field(default_factory=list)


def embed_nodes(nodes: Sequence[AnnotatedNode], embedder: TextEmbedder) -> list[ElementEmbedding]:
    """Embed the text fields of every symbolic state element of ``nodes`` (see ``ELEMENT_FIELDS``).

    One embedding call for all of them; this is what a live session uses for one new node.
    """
    rows: list[tuple[ElementFields, str | None, dict[str, Any]]] = []
    skipped: set[str] = set()
    for node in nodes:
        for kind, elements in node.symbolic_state_elements.items():
            spec = ELEMENT_FIELDS.get(kind)
            if spec is None:
                if kind not in NOT_EMBEDDED_KINDS:
                    skipped.add(kind)
                continue
            rows.extend((spec, node.node_id, element) for element in elements)
    if skipped:
        logger.warning("No embedding fields defined for element kinds %s; skipped", sorted(skipped))

    texts = [element.get(field) for spec, _, element in rows for field in spec.fields]
    vectors = iter(embedder.embed(texts))

    embedded: list[ElementEmbedding] = []
    for spec, node_id, element in rows:
        by_field = {field: next(vectors) for field in spec.fields}
        embedded.append(
            ElementEmbedding(
                element_id=element["id"],
                element_type=spec.element_type,
                node_id=node_id,
                embeddings={f: v for f, v in by_field.items() if v is not None},
            )
        )
    logger.info(
        "Embedded %d elements (%d distinct texts so far, model %s)",
        len(embedded),
        embedder.texts_embedded,
        embedder.model_id,
    )
    return embedded


def embed_result(result: ExtractionResult, embedder: TextEmbedder) -> EmbeddingSet:
    """Embed the text fields of every symbolic state element of ``result`` (see ``ELEMENT_FIELDS``)."""
    embedded = embed_nodes(result.nodes, embedder)
    dimensions = next((len(v) for e in embedded for v in e.embeddings.values()), None)
    return EmbeddingSet(
        session_id=result.session_id,
        model=embedder.model_id,
        dimensions=dimensions,
        elements=embedded,
    )
