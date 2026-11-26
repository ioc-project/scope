"""Semantic relations between symbolic state elements (claim derived from a fact, ...).

A ``SemanticRelationExtractor`` looks at a set of symbolic state elements, in the context of the
session store, and finds relations between them. The base class is only the interface and
finds nothing; subclasses bring the method (an LLM judgement, embeddings, rules, ...).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from symbolic_extractor.models import SymbolicStateElement
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage


class SemanticRelationExtractor:
    """Finds semantic relations among symbolic state elements. The base class does nothing."""

    def __init__(self) -> None:
        #: LLM usage of the last ``extract`` call (zero when no LLM is involved).
        self.last_usage = LLMUsage()

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> Sequence[BaseModel]:
        """Relations found among ``elements``, with ``store`` (the session's elements and
        relations so far) as context. A subclass also records them in ``store``.

        The base class finds none.
        """
        self.last_usage = LLMUsage()
        return []
