"""System prompt of each agent, read from the input of its LLM calls. No LLM is involved.

The system prompt is not a node of the KG: the normalisation folds it into the input of every
LLM call (``gen_ai.system_instructions``, see ``scope/backend/norm`` ``handlers/chat.py``), and the
``Agent`` nodes have an empty ``description``. So it is read back from the call input. Three
shapes of input exist in the saved sessions:

* **OTel** (trip planner): ``<system parts JSON>\\n\\n<messages JSON>``; the parts have no ``role``
  (``[{"type": "text", "content": "..."}]``). Every agent has one system prompt, the same in all
  its calls.
* **OpenAI messages** (SRE): ``[{"role": "system", "content": "..."}, {"role": "user", ...}]``.
  The KG keeps only the first 4000 characters of the input, so it is usually not valid JSON: the
  system message is decoded from the part that is there, and ``truncated`` says whether its string
  was cut (the role statement comes first, the generic part is what gets lost).
* **A reference** (webex, ``llm-input:<agent>:<uuid>``): the content was never recorded. Nothing
  can be read, and nothing is extracted.

Anything else gives no system prompt. The agent of an LLM call comes from the trajectory
(``SessionTrajectory.agent_name``); one system prompt is extracted per agent, from the first LLM
call that has a readable one.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from symbolic_extractor.models import AgentSystemPrompt, KGNode, SessionTrajectory
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.symbolic_state import _message_texts
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Start of an OpenAI-style input whose first message is the system message; the match ends just
#: before the opening quote of its content.
_OPENAI_SYSTEM_START = re.compile(r'\s*\[\s*\{\s*"role"\s*:\s*"system"\s*,\s*"content"\s*:\s*(?=")')
#: A cut string can end with an incomplete escape (``\``, ``\u12``): at most this many characters
#: are dropped to make it decodable.
_MAX_DANGLING_ESCAPE = 6


def _json_values(raw: str) -> list | None:
    """The JSON values glued together in ``raw`` (separated by whitespace), None when some part is
    not valid JSON (an input cut by the KG, or not JSON at all)."""
    decoder = json.JSONDecoder()
    values: list = []
    position = 0
    while True:
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position >= len(raw):
            return values
        try:
            value, position = decoder.raw_decode(raw, position)
        except ValueError:
            return None
        values.append(value)


def _system_texts(values: list) -> list[str]:
    """The system instructions of decoded input values: the entries of a message array without a
    role (the OTel system parts) and the messages whose role is ``system``. Exactly what
    ``symbolic_state.input_texts`` leaves out of the conversation."""
    texts: list[str] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for message in value:
            if isinstance(message, dict) and message.get("role", "system") == "system":
                texts.extend(_message_texts(message))
    return texts


def _cut_system_message(raw: str) -> tuple[str, bool] | None:
    """The system message of an OpenAI-style input that is not valid JSON as a whole (the KG cut
    it), read from the string that is there: ``(text, truncated)``, None when the input does not
    start with a system message or its content is empty."""
    match = _OPENAI_SYSTEM_START.match(raw)
    if match is None:
        return None
    start = match.end()
    try:  # the string is closed: the system message is complete, whatever follows was cut
        text, _ = json.JSONDecoder(strict=False).raw_decode(raw, start)
        return (text, False) if isinstance(text, str) and text.strip() else None
    except ValueError:
        pass
    body = raw[start + 1 :]
    for dropped in range(_MAX_DANGLING_ESCAPE + 1):
        try:
            text = json.loads(f'"{body[: len(body) - dropped]}"', strict=False)
        except ValueError:
            continue
        return (text, True) if text.strip() else None
    return None


def split_system_prompt(raw: str | None) -> tuple[str, bool] | None:
    """The system prompt in the input of an LLM call: ``(text, truncated)``, or None when the input
    has none that can be read (no system message, a reference such as ``llm-input:<agent>:<uuid>``,
    an unknown format). See the module docstring for the shapes handled. ``truncated`` is only ever
    true for an OpenAI-style input that the KG cut inside the system message."""
    raw = (raw or "").strip()
    if not raw:
        return None
    values = _json_values(raw)
    if values is not None:
        text = "\n".join(t for t in _system_texts(values) if t.strip())
        return (text, False) if text else None
    return _cut_system_message(raw)


def agent_system_prompt(store: SymbolicSessionStore, agent_id: str) -> AgentSystemPrompt | None:
    """The system prompt stored for ``agent_id`` (None when it has none yet)."""
    for stored in store.by_kind("system_prompts"):
        element = stored.element
        if isinstance(element, AgentSystemPrompt) and element.agent_id == agent_id:
            return element
    return None


class AgentSystemPromptExtractor:
    """The system prompt of each agent, from the input of its LLM calls, without an LLM.

    Same interface as ``ElementExtractor`` (``element_key``, ``applies_to``, ``extract``,
    ``last_usage``), plus ``uses_trajectory``: ``SymbolicExtractor`` passes it the trajectory, which
    is the only place that says which agent made an LLM call.

    The prompt is extracted once per agent: an LLM call of an agent that already has one in the
    store gives nothing, and an LLM call whose input has no readable system prompt (or whose agent is
    unknown) gives nothing either, so the next call of the agent is tried.
    """

    element_key = "system_prompts"
    node_types = frozenset({"LLMCall"})
    uses_trajectory = True

    def __init__(self) -> None:
        #: No LLM is called: always zero. Present to match ``ElementExtractor``.
        self.last_usage = LLMUsage()
        self.usage = LLMUsage()

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    def extract(
        self,
        node: KGNode,
        store: SymbolicSessionStore | None = None,
        trajectory: SessionTrajectory | None = None,
    ) -> list[AgentSystemPrompt]:
        if not node.node_id:
            return []
        agent = trajectory.agent_name(node.node_id) if trajectory is not None else None
        if not agent:
            logger.debug(
                "Agent of LLM call %s unknown (no trajectory step): no system prompt", node.node_id
            )
            return []
        if store is not None and agent_system_prompt(store, agent) is not None:
            return []
        found = split_system_prompt(
            str(node.node.get("prompt") or node.node.get("inputContent") or "")
        )
        if found is None:
            return []
        text, truncated = found
        # Deterministic id: re-running on the same session yields the same one.
        prompt_id = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|system_prompt|{agent}|{text}")
        )
        return [
            AgentSystemPrompt(
                id=prompt_id,
                agent_id=agent,
                system_prompt=text,
                truncated=truncated,
                read_from=node.node_id,
            )
        ]
