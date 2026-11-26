"""Which earlier tool results a tool result was obtained with: ``supported_by`` (Fact -> Fact), the same
relation as the one between a claim and the fact that backs it. Deterministic: text matching and timestamps,
no LLM.

An agent often calls a tool with a value it just read in the result of another one (``list_services``
returns ``profile-cache``, then ``get_metrics(service="profile-cache")``). Nothing in the trace says so, since
the reasoning of the LLM is not recorded, so the link is a heuristic. For each string argument of a tool call:

1. *Visible results.* The call was requested by the last LLM call of its agent call that ended before it
   started. That LLM call saw the results of the tool calls of the same agent call that had finished when it
   started; the results of the tool calls running in parallel with the call (or later) were not visible.
2. *Match.* A visible result matches when the value occurs in its text (``verbatim_source`` of the fact).
3. *Echo filter.* A result whose own tool call was made with the same value is dropped: tools echo their
   inputs (``get_metrics(window="15m")`` returns ``"window": "15m"``), so it did not introduce the value.
4. *Earliest source.* Of the results left, the earliest one is kept: the one that introduced the value.

The link goes from the fact that is the result of the tool call (the one ``grounded_in`` it) to the fact that
is the source: the result is ``supported_by`` the results the arguments of its call were read from. Its attribute
``arguments`` maps each argument name to the value found there. Only string arguments of at least
``MIN_VALUE_CHARS`` characters are matched (a number such as ``50`` or a flag would match almost anything).

The LLM counterpart for claims is the ``supported_by`` of ``ArgumentOriginExtractor`` (Fact -> Claim, judged
against the claims the agent had made): the two are told apart by the kind of their ends. A ``supported_by``
between two facts has no LLM counterpart yet, so the heuristic can later be compared against one on the same
session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from symbolic_extractor.models import Fact, SessionTrajectory, SymbolicStateElement
from symbolic_extractor.relations.fact_claim_links import SUPPORTED_BY
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Shortest string argument that is matched.
MIN_VALUE_CHARS = 3


class ArgumentFactLink(BaseModel):
    """The fact ``fact_id`` (the result of a tool call) is ``supported_by`` the fact ``source_id`` (the result of an
    earlier one): the arguments in ``arguments`` were found there."""

    fact_id: str
    source_id: str
    #: argument name -> value found in the source fact
    arguments: dict[str, str]


def string_arguments(arguments: Any) -> dict[str, str]:
    """The arguments of a tool call that can be searched in a text: name -> value, for the string values of
    at least ``MIN_VALUE_CHARS`` characters (after stripping)."""
    if not isinstance(arguments, dict):
        return {}
    found = {}
    for name, value in arguments.items():
        if isinstance(value, str) and len(value.strip()) >= MIN_VALUE_CHARS:
            found[str(name)] = value.strip()
    return found


def _end(step: dict[str, Any]) -> float:
    return (step.get("start_time") or 0.0) + (step.get("duration_ms") or 0.0) / 1000.0


def find_argument_facts(
    store: SymbolicSessionStore, trajectory: SessionTrajectory, tool_call_id: str
) -> list[ArgumentFactLink]:
    """The links of one tool call (see the module docstring), one per source fact.
    Empty when the tool call is unknown, has no fact in the store or no string argument."""
    steps = trajectory.steps
    step = next((s for s in steps if s.get("kind") == "tool" and s["call_id"] == tool_call_id), None)
    if step is None:
        return []
    found = store.for_node(tool_call_id, "facts")
    wanted = string_arguments(getattr(found[0].element, "tool_arguments", None)) if found else {}
    if not wanted:
        return []
    own_fact_id = found[0].id

    owner = step.get("parent_id") or ""
    start = step.get("start_time") or 0.0
    requesters = [s for s in steps if s.get("kind") == "llm" and (s.get("parent_id") or "") == owner and _end(s) <= start]
    # What the requesting LLM call could see: the results finished before it started (before the tool call
    # itself when no LLM call is known).
    requester = max(requesters, key=_end, default=None)
    cut = (requester.get("start_time") or 0.0) if requester else start
    sources = []  # (tool call step, its fact), earliest first
    for s in sorted((s for s in steps if s.get("kind") == "tool"), key=lambda s: s.get("start_time") or 0.0):
        if s["call_id"] == tool_call_id or (s.get("parent_id") or "") != owner or _end(s) > cut:
            continue
        stored = store.for_node(s["call_id"], "facts")
        if stored:
            sources.append(stored[0].element)

    by_fact: dict[str, dict[str, str]] = {}  # source fact id -> {argument name: value}
    for name, value in wanted.items():
        for fact in sources:
            if value not in (getattr(fact, "verbatim_source", None) or ""):
                continue
            if value in string_arguments(getattr(fact, "tool_arguments", None)).values():
                continue  # echo: the source was called with that value
            by_fact.setdefault(fact.id, {})[name] = value
            break
    return [ArgumentFactLink(fact_id=own_fact_id, source_id=f, arguments=a) for f, a in by_fact.items()]


class ArgumentFactLinker(SemanticRelationExtractor):
    """Links the arguments of a tool call to the earlier results they were read from, see the module
    docstring. No LLM: ``last_usage`` stays zero.

    ``extract(store, elements, trajectory)`` handles the ``Fact`` elements of ``elements`` that are the result
    of a tool call with arguments (others are ignored), and records ``supported_by`` (the fact -> each source
    fact, attribute ``arguments``) in the store. It returns the links found (also the ones already recorded).
    Without a ``trajectory`` the agent call of the tool call is unknown and nothing is linked. Idempotent.

    The sources are facts of earlier tool calls: the pipeline runs it on the facts of a ToolCall node when the
    node is processed (nodes come in start order), so they are in the store already.
    """

    def extract(  # type: ignore[override]
        self,
        store: SymbolicSessionStore,
        elements: Sequence[SymbolicStateElement],
        trajectory: SessionTrajectory | None = None,
    ) -> list[ArgumentFactLink]:
        self.last_usage = LLMUsage()
        links: list[ArgumentFactLink] = []
        if trajectory is None:
            return links
        for element in elements:
            if not isinstance(element, Fact) or not element.tool_arguments or store.get(element.id) is None:
                continue
            for link in find_argument_facts(store, trajectory, element.grounded_in):
                store.link(SUPPORTED_BY, link.fact_id, link.source_id, arguments=link.arguments)
                links.append(link)
        if links:
            logger.info("supported_by: %d fact -> fact links (arguments of tool calls)", len(links))
        return links
