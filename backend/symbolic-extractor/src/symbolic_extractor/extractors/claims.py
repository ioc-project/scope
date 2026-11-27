"""Claim extraction. Definitions follow ``cognitive-ontology.ttl`` (mas:Claim).

The ``Claim`` / ``ClaimItem`` objects are defined in ``symbolic_extractor.models``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from symbolic_extractor.extractors.base import ExtractionPrompt
from symbolic_extractor.models import Claim, ClaimItem, KGNode
from symbolic_extractor.tool_calls import ToolCallRequest, requested_tool_calls

logger = logging.getLogger(__name__)

# Truncate very long texts so a single node cannot blow the context window.
MAX_CHARS = 12_000


SYSTEM_PROMPT = """\
You extract CLAIMS from the output of one LLM call in a multi-agent system trajectory.

A claim is a proposition, or a statement of stance, that the agent makes in the output text.
You record what the agent says, NOT whether it is true.

For each claim return:
- claim_text: the claim as one short, self-contained sentence. Stay faithful to the agent's \
wording and meaning, but resolve pronouns and references ("it", "this option") so the claim \
can be understood on its own. Do not add information the output does not contain.
- claim_force: the act the agent performs.
    * "assertive": states something about the world, the task or its own work.
    * "committing": states an action or choice the agent intends or adopts \
("I'll adopt v2", "we'll use microservices").
    * "directive": instructs or advises someone to act ("you must use v2", "visit in October").
- claim_kind: the sort of content (what decides how the claim is verified later).
    * "factual": a source could confirm or refute it ("Hotel Alfama has availability Friday to Sunday").
    * "evaluative": a judgment with no checkable core ("this is the best option").
    * "recommendation": advises what to do, or what suits whom or when.
  Required when claim_force is "assertive". For "directive", set it only for advisory \
directives (recommendation) and use null for plain instructions. Always null for "committing".
- verbatim_source: the part of the OUTPUT OF THE LLM that expresses the claim, copied \
exactly, character for character. It will be searched for as an exact string in other \
agents' inputs to follow the claim, so it must be long enough to be distinctive: give the \
FULL sentence that contains the claim, or several consecutive sentences when the claim spans \
them. Never give just a few words or a fragment of a sentence. Do NOT paraphrase, fix, \
translate, shorten with "..." or change punctuation, capitalisation, markdown or numbers. \
The span must be contiguous and, when the text has line breaks, stay within one line or \
paragraph (list items: the whole item). Several claims may share the same excerpt (for \
example when one sentence holds two claims). Always provide it; when the output is short or \
unstructured (for example a routing command), quote the whole output.
- tool_call_ids: only for a "committing" claim that announces tool calls (see the rules below), \
the ids of the calls it announces, copied from the section "TOOL CALLS REQUESTED BY THE LLM". \
Otherwise [].

Rules:
- Extract only from the section "OUTPUT OF THE LLM". The input section is context to \
understand the output; never extract claims from it.
- One claim per proposition. Split compound sentences into separate claims; never repeat a claim.
- Skip greetings, filler, questions and meta-talk about the format of the answer.
- When the output text says what the agent is about to do with a tool (it announces, plans or \
explains a tool call, for example "Let me look up the hotel prices"), extract it as a \
"committing" claim: claim_text names the tool action and what it is for, and tool_call_ids \
lists the call(s) of the section "TOOL CALLS REQUESTED BY THE LLM" it announces. Use only ids \
listed in that section, and use the name and arguments there to decide which calls the text is \
about. When that section is present, the text of the output is what the agent says right before \
making those calls, so a sentence about doing something the calls perform (even stated in \
general terms, such as "I will check the prices" for calls to a price lookup tool) announces them: \
fill tool_call_ids, with every call it covers (several calls may share one announcement), and \
leave it empty only when no listed call fits the sentence. Text that only restates earlier \
results is not an announcement. That section is context: never extract a claim from it, and \
never take a verbatim_source from it.
- Example. Section: "- id call_a1: get_weather({\\"city\\": \\"Oslo\\"})" and "- id call_a2: \
get_weather({\\"city\\": \\"Bergen\\"})"; output text: "Next, I will check the forecast for the \
two cities." gives {"claim_text": "I will check the weather forecast for Oslo and Bergen.", \
"claim_force": "committing", "claim_kind": null, "verbatim_source": "Next, I will check the \
forecast for the two cities.", "tool_call_ids": ["call_a1", "call_a2"]}.
- Skip text that merely quotes or copies the input without the agent standing behind it.
- If the output contains no claim, return an empty list.

Reply with a JSON object only. Every claim has all five fields, including tool_call_ids ([] when \
the claim announces no tool call):
{"claims": [{"claim_text": "...", "claim_force": "assertive|committing|directive", \
"claim_kind": "factual|evaluative|recommendation|null", "verbatim_source": "...", \
"tool_call_ids": []}]}
"""


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _text_parts(output: str) -> list[str]:
    """Text of the message parts when ``output`` is an OTel-style JSON message list."""
    try:
        messages = json.loads(output)
    except (TypeError, ValueError):
        return []
    if not isinstance(messages, list):
        return []
    return [
        part["content"]
        for message in messages
        if isinstance(message, dict)
        for part in message.get("parts") or []
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("content"), str)
    ]


def _final_answer(text: str) -> str | None:
    """The ``final_answer`` text when ``text`` is a JSON object (a router envelope such as
    ``{"next": "FINISH", "final_answer": "..."}``) holding one."""
    try:
        answer = json.loads(text)["final_answer"]
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return answer if isinstance(answer, str) and answer.strip() else None


def readable_output(output: str) -> str:
    """The text an agent produced, without the JSON wrapping the trace adds around it: the
    text of the message parts of an OTel message list, and, when that text is a router
    envelope with a ``final_answer``, only that answer. Anything else is returned unchanged."""
    parts = _text_parts(output)
    text = "\n".join(p for p in parts if p.strip()) if parts else output
    return _final_answer(text) or text


def is_verbatim(snippet: str, output: str) -> bool:
    """Whether ``snippet`` occurs in ``output``, either in the raw string or in its readable
    text (the raw form has JSON escapes). Whitespace runs are collapsed on both sides, so
    line breaks / indentation do not cause misses."""
    needle = _normalize_ws(snippet)
    if not needle:
        return False
    texts = (output, *_text_parts(output), readable_output(output))
    return any(needle in _normalize_ws(text) for text in texts)


#: Shortest snippet (in letters and digits) that may be recovered by alignment: a shorter one
#: could match several places of the output by accident.
MIN_RECOVER_CHARS = 15


def _alnum_index(text: str) -> tuple[str, list[int]]:
    """``text`` reduced to its lower-cased letters and digits, and the position in ``text`` of each."""
    positions = [i for i, ch in enumerate(text) if ch.isalnum()]
    return "".join(text[i].lower() for i in positions), positions


def recover_verbatim(snippet: str, output: str) -> str | None:
    """The exact excerpt of ``output`` that ``snippet`` was meant to copy, or None.

    LLMs often "clean" what they copy: markdown (``**bold**``, list markers, ``###``) or line
    breaks are dropped, so the snippet is no longer a substring of the output and the claim could
    not be followed across calls. When ``snippet`` is verbatim it is returned as it is. Otherwise it
    is looked for in the readable text of the output comparing letters and digits only (case and
    punctuation ignored), and the original substring found there is returned, with its markup.
    Snippets with fewer than ``MIN_RECOVER_CHARS`` letters and digits are not recovered.
    """
    if is_verbatim(snippet, output):
        return snippet.strip()
    needle, _ = _alnum_index(snippet)
    if len(needle) < MIN_RECOVER_CHARS:
        return None
    text = readable_output(output)
    haystack, positions = _alnum_index(text)
    start = haystack.find(needle)
    if start < 0:
        return None
    excerpt = text[positions[start] : positions[start + len(needle) - 1] + 1]
    return excerpt if is_verbatim(excerpt, output) else None


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n[... truncated ...]"


def _first_text(node: dict, *keys: str) -> str:
    for key in keys:
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _tool_calls_section(calls: list[ToolCallRequest]) -> str:
    """The prompt section listing the tool calls the LLM requested ('' when there are none)."""
    if not calls:
        return ""
    lines = [f"- id {c.id}: {c.name}({json.dumps(c.arguments, ensure_ascii=False)})" for c in calls]
    return (
        "\n=== TOOL CALLS REQUESTED BY THE LLM (context only: no claims from here, never a verbatim_source) ===\n"
        + _clip("\n".join(lines))
        + "\n"
    )


class ClaimExtractionPrompt(ExtractionPrompt[ClaimItem, Claim]):
    """Claims are generated by an LLMCall (mas:generatedBy), so only LLMCall nodes apply."""

    element_key = "claims"
    node_types = frozenset({"LLMCall"})
    item_model = ClaimItem
    system_prompt = SYSTEM_PROMPT

    def render(self, node: KGNode) -> str | None:
        output = _first_text(node.node, "completion", "outputContent")
        if not output.strip():
            return None
        calls = requested_tool_calls(output)
        if calls and not _text_parts(output):
            # Only tool calls, no text: the LLM said nothing a claim could be made of.
            return None
        input_text = _first_text(node.node, "prompt", "inputContent")
        model = _first_text(node.node, "modelName", "llmName")
        return (
            f"LLM call id: {node.node_id or 'unknown'}\n"
            f"Model: {model or 'unknown'}\n\n"
            f"=== INPUT GIVEN TO THE LLM (context only) ===\n{_clip(input_text) or '(empty)'}\n\n"
            f"=== OUTPUT OF THE LLM (extract claims from this) ===\n{_clip(readable_output(output))}\n"
            f"{_tool_calls_section(calls)}"
        )

    def build_element(self, item: ClaimItem, node: KGNode) -> Claim:
        # Deterministic id: re-running on the same session yields the same claim ids,
        # which keeps a later write to the KG idempotent.
        claim_id = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|{item.claim_force}|{item.claim_text}")
        )
        data = item.model_dump()
        output = _first_text(node.node, "completion", "outputContent")
        # Only the ids the LLM call really requested: the link to a tool call cannot be invented.
        requested = {c.id for c in requested_tool_calls(output)}
        data["tool_call_ids"] = [i for i in dict.fromkeys(item.tool_call_ids) if i in requested]
        snippet = data["verbatim_source"]
        if snippet is not None:
            exact = recover_verbatim(snippet, output)
            if exact is not None:
                # Verbatim as given, or the original excerpt the LLM copied with its formatting removed.
                data["verbatim_source"] = exact
            else:
                # A snippet that is not in the output cannot be used to follow the claim.
                logger.warning(
                    "verbatim_source not found in the output of LLMCall %s, dropping it: %r",
                    node.node_id,
                    snippet[:120],
                )
                data["verbatim_source"] = None
        return Claim(**data, id=claim_id, generated_by=node.node_id)
