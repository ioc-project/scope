"""Fact extraction. Definitions follow ``cognitive-ontology.ttl`` (mas:Fact).

A Fact is a proposition grounded in provenance external to agent generation. Only the
``tool_result`` source is handled: every ToolCall that returned something yields one Fact
holding its output. No LLM is involved, so the Facts cost nothing and are deterministic.

The Fact also records the tool and the arguments it was called with (``tool_name``, ``tool_arguments``):
a bare output such as ``5`` means nothing without them. Which claim announced the call is not known here
(it is a relation between nodes): it is the ``announces`` relation, see ``SymbolicExtractor._link_announces``.

For now ``fact_text`` and ``verbatim_source`` are both the tool output. They are kept as two
fields because ``fact_text`` is meant to become a distilled proposition later, while
``verbatim_source`` stays the exact output that agents may later quote in their inputs.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from symbolic_extractor.models import Fact, KGNode
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)


def tool_output_text(node: dict) -> str:
    """The output of a ToolCall node as the text an agent would have received.

    The KG stores outputs JSON-encoded, so a string result is wrapped in quotes with escapes
    (``"\\"Fare: $75\\\\n\\""``); it is decoded back to the plain text, which is what appears
    in the inputs of later LLM calls. Any other output (an object, a number, plain text that
    is not JSON) is kept as stored. Returns an empty string when there is no output.
    """
    for key in ("toolOutput", "outputContent"):
        value = node.get(key)
        if value in (None, ""):
            continue
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        try:
            decoded = json.loads(raw)
        except ValueError:
            return raw.strip()
        return (decoded if isinstance(decoded, str) else raw).strip()
    return ""


def tool_name(node: dict) -> str | None:
    """Name of the tool a ToolCall node ran: ``toolName``, else the one in a span name such as
    ``Tool call (calculate) 8d701e6a``. None when the node does not say."""
    name = node.get("toolName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    match = re.match(r"Tool call \((.+?)\)", str(node.get("name") or ""))
    return match.group(1) if match else None


def tool_arguments(node: dict) -> dict[str, Any]:
    """What a ToolCall node was called with. The KG stores ``{"args": [...], "kwargs": {...}}``: the
    result is the keyword arguments, and the positional ones, if there are any, under ``"args"``. Empty
    when the tool took no argument or the node has none (or none that can be read)."""
    for key in ("toolArguments", "inputParams", "inputContent"):
        value = node.get(key)
        if value in (None, ""):
            continue
        try:
            data = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        if "kwargs" not in data and "args" not in data:
            return data
        kwargs = data.get("kwargs")
        arguments = dict(kwargs) if isinstance(kwargs, dict) else {}
        if data.get("args"):
            arguments["args"] = data["args"]
        return arguments
    return {}


class ToolResultFactExtractor:
    """Facts from ToolCall outputs, without an LLM.

    Same interface as ``ElementExtractor`` (``element_key``, ``applies_to``, ``extract``,
    ``last_usage``), so it plugs into ``SymbolicExtractor`` next to the LLM extractors.
    """

    element_key = "facts"
    node_types = frozenset({"ToolCall"})

    def __init__(self) -> None:
        #: No LLM is called: always zero. Present to match ``ElementExtractor``.
        self.last_usage = LLMUsage()
        self.usage = LLMUsage()

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    def extract(self, node: KGNode, store: SymbolicSessionStore | None = None) -> list[Fact]:
        text = tool_output_text(node.node)
        if not text:
            return []
        if not node.node_id:
            logger.warning("ToolCall without an id, no fact extracted")
            return []
        # Deterministic id: re-running on the same session yields the same fact ids.
        fact_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|fact|{text}"))
        return [
            Fact(
                id=fact_id,
                fact_text=text,
                fact_source_type="tool_result",
                verbatim_source=text,
                grounded_in=node.node_id,
                tool_name=tool_name(node.node),
                tool_arguments=tool_arguments(node.node),
            )
        ]
