"""Capabilities of an agent, extracted from its system prompt (one LLM request per agent).

A capability is what an agent is able to do: the kind of work it is specialised in, and what its tools
and skills let it do. It is not what it is told to do (the standing tasks, ``SystemTask``) nor how it
must behave (the system ``Constraint``). The point is to check later that a task is within what the
agent can do: an agent that finds the ways to reach a place should not be asked to book a hotel.

``SystemCapabilityExtractor`` works like ``SystemTaskExtractor`` and ``SystemConstraintExtractor``: it
runs on the LLM call from which ``AgentSystemPromptExtractor`` has just read the system prompt of an
agent (it must come after it in the list of extractors), so the LLM is asked once per agent and
session, with its own request. The pipeline links the capabilities to every ``AgentCall`` of the agent
(``SymbolicExtractor._link_capabilities``). The system prompt is only an approximation of what the
agent can do (it may be cut by the KG, and an agent often can do more than its prompt says): what is
extracted is what the prompt shows.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import ValidationError

from symbolic_extractor.extractors.claims import _clip
from symbolic_extractor.llm import LLMClient
from symbolic_extractor.models import AgentSystemPrompt, AgentCapability, AgentCapabilityItem, KGNode
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You extract the CAPABILITIES of an agent from its system prompt, in a multi-agent system.

A capability is something the agent is able to do: the kind of work it is specialised in and the \
means it has to do it (tools, data sources, skills, knowledge domains). The capabilities will be \
used to check that a task given to the agent is something it can do, so they must say what kind of \
work the agent can take on, at the level of what someone would ask it.
Example: for an itinerary agent whose prompt lists a route-search tool and a timetable tool -> \
"Find the ways to reach a place (routes, transport options, travel times) with the route-search \
and timetable tools". It is not a capability to find hotels or estimate costs: the prompt does \
not give it that.

Not a capability:
- What the agent is told to deliver in each call (its duties and deliverables) or how it must \
behave (rules, limits, tone, safety): those are tasks and constraints, extracted elsewhere. Use \
the role description only to tell what kind of work the agent can do.
- Abilities every language-model agent has (understand a request, write text, reason, answer in \
the language of the user), unless that is the whole role of the agent ("routes the request to the \
right specialist", "summarises the findings of the others").
- What the agent is not allowed to do, or could do if it had other tools. Do not infer abilities the \
prompt does not show.

For each capability return:
- capability_text: what the agent is able to do, as one short, self-contained sentence that names \
the kind of work and, when the prompt says so, the tools or sources it uses. Stay faithful to the \
prompt and add nothing it does not say.

Rules:
- One capability per distinct kind of work. Tools and sources that serve the same kind of work are \
the means of one capability, not one capability each. A role usually has one to six.
- Never repeat a capability. Return an empty list when the prompt shows none.
- The header may say that the prompt is cut off: it is then only the beginning, extract from what \
is there and do not guess the rest.

Reply with a JSON object only:
{"capabilities": [{"capability_text": "..."}]}
"""


def render_capability_prompt(prompt: AgentSystemPrompt) -> str:
    """The user prompt that asks for the capabilities in ``prompt``."""
    return (
        f"Agent: {prompt.agent_id}\n"
        + ("System prompt looks cut off: yes\n" if prompt.truncated else "")
        + "\n=== SYSTEM PROMPT OF THE AGENT (extract the capabilities from this) ===\n"
        + f"{_clip(prompt.system_prompt)}\n"
    )


def agent_capabilities(store: SymbolicSessionStore, agent_id: str) -> list[AgentCapability]:
    """The capabilities stored for ``agent_id`` (empty when none)."""
    return [
        s.element
        for s in store.by_kind("capabilities")
        if isinstance(s.element, AgentCapability) and s.element.agent_id == agent_id
    ]


class SystemCapabilityExtractor:
    """The capabilities of an agent, from the system prompt just read from an LLM call.

    Same interface as ``ElementExtractor``. Like ``SystemTaskExtractor`` it needs the
    ``AgentSystemPromptExtractor`` to run first on the same node: it looks in the store for the system
    prompt read from this very call, so it does nothing on any other call (one request per agent and
    session), and nothing at all when there is no such extractor or no readable prompt (webex).
    """

    element_key = "capabilities"
    node_types = frozenset({"LLMCall"})

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        #: Usage of the last ``extract`` call (zero when no request was made).
        self.last_usage = LLMUsage()
        #: Cumulative usage of this extractor.
        self.usage = LLMUsage()

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    def extract(self, node: KGNode, store: SymbolicSessionStore | None = None) -> list[AgentCapability]:
        self.last_usage = LLMUsage()
        if store is None or not node.node_id:
            return []
        prompt = next(
            (
                s.element
                for s in store.for_node(node.node_id, "system_prompts")
                if isinstance(s.element, AgentSystemPrompt)
            ),
            None,
        )
        if prompt is None:
            return []

        # Transport / API errors are not caught: a failed LLM call must not be silently mistaken
        # for "no capabilities" (the agent would never be asked again).
        reply = self._llm.complete_json(SYSTEM_PROMPT, render_capability_prompt(prompt))
        self.last_usage = reply.usage
        self.usage = self.usage + reply.usage

        raw_items = reply.data.get("capabilities", [])
        if not isinstance(raw_items, list):
            logger.warning(
                "LLM reply for the system prompt of %s: 'capabilities' is not a list, ignoring", prompt.agent_id
            )
            return []
        capabilities: list[AgentCapability] = []
        for raw in raw_items:
            try:
                item = AgentCapabilityItem.model_validate(raw)
            except ValidationError as exc:
                logger.warning(
                    "Dropping invalid capability of %s: %s", prompt.agent_id, exc.errors(include_url=False)
                )
                continue
            # Deterministic id: re-running on the same session yields the same ids.
            capability_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{prompt.id}|capability|{item.capability_text}"))
            capabilities.append(
                AgentCapability(
                    **item.model_dump(),
                    id=capability_id,
                    agent_id=prompt.agent_id,
                    system_prompt_id=prompt.id,
                    read_from=node.node_id,
                    truncated=prompt.truncated,
                )
            )
        return capabilities
