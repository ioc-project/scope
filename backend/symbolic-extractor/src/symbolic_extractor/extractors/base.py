"""Generic building blocks shared by every symbolic state element extractor.

An extractor is split in two:

* ``ExtractionPrompt``: everything specific to one element kind (which node types it
  applies to, the prompt text, the schema of the LLM output, how to turn an LLM item
  into the final element). One subclass per element kind (claims, goals, tasks, ...).
* ``ElementExtractor``: the generic runner. It renders the prompt for a node, calls the
  LLM, validates the reply and returns typed elements.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from symbolic_extractor.llm import LLMClient
from symbolic_extractor.models import KGNode, SymbolicStateElement
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

ItemT = TypeVar("ItemT", bound=BaseModel)
ElementT = TypeVar("ElementT", bound=SymbolicStateElement)


class Extractor(Protocol):
    """What ``SymbolicExtractor`` needs from an extractor, with or without an LLM."""

    last_usage: LLMUsage

    @property
    def element_key(self) -> str: ...

    def applies_to(self, node: KGNode) -> bool: ...

    def extract(
        self, node: KGNode, store: SymbolicSessionStore | None = None
    ) -> Sequence[SymbolicStateElement]:
        """Elements of ``node``. ``store`` holds what was extracted before in the session (the
        nodes are processed in time order); an extractor may use it as context.

        An extractor that needs to know which agent made a call sets ``uses_trajectory = True`` and
        accepts ``trajectory`` (a ``SessionTrajectory``, None when there is none) as a third,
        keyword argument; ``SymbolicExtractor`` then passes it. See ``AgentSystemPromptExtractor``."""
        ...


class ExtractionPrompt(ABC, Generic[ItemT, ElementT]):
    """Prompt and output contract for one kind of symbolic state element."""

    #: Key under which the elements are stored (``symbolic_state_elements[element_key]``) and
    #: under which the LLM is asked to return its list, e.g. ``"claims"``.
    element_key: str
    #: KG node types (``KGNode.node_type``) this prompt can extract from.
    node_types: frozenset[str]
    #: Schema each item of the LLM's list must satisfy.
    item_model: type[ItemT]
    system_prompt: str
    #: Key of the list in the LLM reply when it is not ``element_key`` (a second prompt that extracts
    #: claims under another element key still asks for ``"claims"``).
    reply_key: str | None = None

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    @abstractmethod
    def render(self, node: KGNode) -> str | None:
        """User prompt for ``node``, or None when the node has nothing to extract from."""

    def render_in_session(self, node: KGNode, store: SymbolicSessionStore | None) -> str | None:
        """``render`` with the elements already extracted in the session (``store``) available as
        context. By default the prompt does not use them."""
        return self.render(node)

    def build_element(self, item: ItemT, node: KGNode) -> ElementT:
        """Turn a validated LLM item into the final element (add ids, KG links, ...)."""
        return item  # type: ignore[return-value]


class ElementExtractor(Generic[ItemT, ElementT]):
    """Runs an ``ExtractionPrompt`` against the LLM for one node."""

    def __init__(self, llm: LLMClient, prompt: ExtractionPrompt[ItemT, ElementT]) -> None:
        self._llm = llm
        self._prompt = prompt
        #: Usage of the last ``extract`` call (zero when the node was skipped).
        self.last_usage = LLMUsage()
        #: Cumulative usage of this extractor.
        self.usage = LLMUsage()

    @property
    def element_key(self) -> str:
        return self._prompt.element_key

    def applies_to(self, node: KGNode) -> bool:
        return self._prompt.applies_to(node)

    def extract(self, node: KGNode, store: SymbolicSessionStore | None = None) -> list[ElementT]:
        self.last_usage = LLMUsage()
        user_prompt = self._prompt.render_in_session(node, store)
        if user_prompt is None:
            return []

        # Transport / API errors are not caught: a failed LLM call must not be silently
        # mistaken for "no elements found".
        reply = self._llm.complete_json(self._prompt.system_prompt, user_prompt)
        self.last_usage = reply.usage
        self.usage = self.usage + reply.usage

        elements: list[ElementT] = []
        for raw in self._raw_items(reply.data, node):
            try:
                item = self._prompt.item_model.model_validate(raw)
            except ValidationError as exc:
                logger.warning(
                    "Dropping invalid %s item from %s %s: %s",
                    self.element_key,
                    node.node_type,
                    node.node_id,
                    exc.errors(include_url=False),
                )
                continue
            elements.append(self._prompt.build_element(item, node))
        return elements

    def _raw_items(self, reply: dict[str, Any], node: KGNode) -> list[Any]:
        items = reply.get(self._prompt.reply_key or self.element_key, [])
        if not isinstance(items, list):
            logger.warning(
                "LLM reply for %s %s: '%s' is not a list, ignoring",
                node.node_type,
                node.node_id,
                self._prompt.reply_key or self.element_key,
            )
            return []
        return items
