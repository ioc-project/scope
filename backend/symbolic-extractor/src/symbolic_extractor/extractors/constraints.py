"""Constraint extraction (mas:Constraint). One class, ``Constraint``, two origins told apart by its
``origin`` attribute:

* ``user``: what the user forbids, requires or prefers in the request of the session ("avoid planes",
  "budget under 800 EUR"). ``UserConstraintPrompt`` reads it from the ``Conversation`` node, with its
  own LLM request, separate from the one that extracts the goal from the same text. The pipeline links
  these constraints to the goals of that node (``bounded_by``, see ``SymbolicExtractor._link_constraints``).
* ``system``: the rules the system prompt of an agent puts on its behaviour ("never act without
  approval", "state uncertainty explicitly"). ``SystemConstraintExtractor`` works like
  ``SystemTaskExtractor``: it runs on the LLM call from which ``AgentSystemPromptExtractor`` has just
  read the prompt of an agent (it must come after it in the list of extractors), so the LLM is asked once
  per agent and session, with its own request, separate from the one that extracts the standing tasks of
  the same prompt. The pipeline links them to the tasks of the agent calls of the agent.

A constraint is not a goal or a task: it does not say what to achieve or do, it bounds how. Test: does
it forbid, require or prefer something specific?
"""

from __future__ import annotations

import logging
import uuid

from pydantic import ValidationError

from symbolic_extractor.extractors.base import ExtractionPrompt
from symbolic_extractor.extractors.claims import _clip, _first_text
from symbolic_extractor.extractors.goals import _user_request
from symbolic_extractor.llm import LLMClient
from symbolic_extractor.models import (
    CONVERSATION_NODE_TYPE,
    AgentSystemPrompt,
    Constraint,
    ConstraintItem,
    KGNode,
)
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

_SHARED = """\
A constraint is a boundary on acceptable solutions, a selection preference over acceptable \
solutions, or a rule on behaviour. Test: does it forbid, require or prefer something specific?

For each constraint return:
- constraint_text: what it forbids, requires or prefers, as one short, self-contained sentence. \
Stay faithful to the wording and meaning of the text and add nothing it does not say.
- constraint_strength: "hard" when it must hold (a boundary, a prohibition, an obligation, a rule), \
"soft" when it is only a preference over acceptable solutions ("preferably", "if possible", \
"I would rather").

Rules:
- One constraint per distinct restriction. Split compound sentences when the parts are \
independent; never repeat a constraint.
- Return an empty list when the text states none. Do not invent constraints.
"""

USER_SYSTEM_PROMPT = (
    """\
You extract the CONSTRAINTS the user states in the request sent to a multi-agent system.

"""
    + _SHARED
    + """\
- Only what the user states about the solution: limits (budget, deadline, size, count), exclusions \
("avoid planes"), requirements (must include, must be in this format), rules on how the work is to \
be done, preferences between acceptable solutions.
- Not a constraint: the goal itself (what the user wants the system to achieve or find out), a \
question, background information, greetings.
Example: "Plan a weekend in Lisbon for 2, total cost under EUR 800, no flights, preferably near \
the river." -> "The total cost must stay under EUR 800." (hard), "The trip must not use flights." \
(hard), "The accommodation should preferably be near the river." (soft). The plan itself is the \
goal, not a constraint.
- The request may start with a technical prefix such as "input:<agent>:"; ignore it.

Reply with a JSON object only:
{"constraints": [{"constraint_text": "...", "constraint_strength": "hard|soft"}]}
"""
)

SYSTEM_SYSTEM_PROMPT = (
    """\
You extract the CONSTRAINTS an agent is given in its system prompt, in a multi-agent system.

"""
    + _SHARED
    + """\
- Only rules that restrict how the agent must behave or what it may deliver, whatever it is asked \
to do: prohibitions ("never delete data without approval"), obligations ("always state uncertainty \
explicitly", "ask before a risky action"), limits (budgets, formats, sizes), preferences ("prefer \
the cached result").
- Not a constraint: the duties and deliverables of the agent (its tasks: "investigate the incident \
and report the cause"), the persona or identity ("You are an itinerary specialist"), the list of \
tools and how to call them, general background. A rule is a constraint even when it comes inside a \
description of a duty, if it restricts how the duty is carried out.
- The header may say that the prompt is cut off: it is then only the beginning, extract from what \
is there and do not guess the rest.

Reply with a JSON object only:
{"constraints": [{"constraint_text": "...", "constraint_strength": "hard|soft"}]}
"""
)


class UserConstraintPrompt(ExtractionPrompt[ConstraintItem, Constraint]):
    """The constraints of the user, read from the ``Conversation`` node (the request of the session).
    Same node as the goal, but its own LLM request and prompt."""

    element_key = "constraints"
    node_types = frozenset({CONVERSATION_NODE_TYPE})
    item_model = ConstraintItem
    system_prompt = USER_SYSTEM_PROMPT

    def render(self, node: KGNode) -> str | None:
        query = _first_text(node.node, "query")
        if not query.strip():
            return None
        return f"=== REQUEST OF THE USER (extract the constraints from this) ===\n{_clip(_user_request(query))}\n"

    def build_element(self, item: ConstraintItem, node: KGNode) -> Constraint:
        # Deterministic id: re-running on the same session yields the same constraint ids.
        constraint_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|constraint|user|{item.constraint_text}"))
        return Constraint(**item.model_dump(), id=constraint_id, origin="user", read_from=node.node_id or "")


def render_system_constraint_prompt(prompt: AgentSystemPrompt) -> str:
    """The user prompt that asks for the constraints in ``prompt``."""
    return (
        f"Agent: {prompt.agent_id}\n"
        + ("System prompt looks cut off: yes\n" if prompt.truncated else "")
        + "\n=== SYSTEM PROMPT OF THE AGENT (extract the constraints from this) ===\n"
        + f"{_clip(prompt.system_prompt)}\n"
    )


def agent_constraints(store: SymbolicSessionStore, agent_id: str) -> list[Constraint]:
    """The system constraints stored for ``agent_id`` (empty when none)."""
    return [
        s.element
        for s in store.by_kind("constraints")
        if isinstance(s.element, Constraint) and s.element.origin == "system" and s.element.agent_id == agent_id
    ]


class SystemConstraintExtractor:
    """The constraints of an agent, from the system prompt just read from an LLM call.

    Same interface as ``ElementExtractor``. Like ``SystemTaskExtractor`` it needs the
    ``AgentSystemPromptExtractor`` to run first on the same node: it looks in the store for the system
    prompt read from this very call, so it does nothing on any other call (one request per agent and
    session), and nothing at all when there is no such extractor or no readable prompt (webex).
    """

    element_key = "constraints"
    node_types = frozenset({"LLMCall"})

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        #: Usage of the last ``extract`` call (zero when no request was made).
        self.last_usage = LLMUsage()
        #: Cumulative usage of this extractor.
        self.usage = LLMUsage()

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    def extract(self, node: KGNode, store: SymbolicSessionStore | None = None) -> list[Constraint]:
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
        # for "no constraints" (the agent would never be asked again).
        reply = self._llm.complete_json(SYSTEM_SYSTEM_PROMPT, render_system_constraint_prompt(prompt))
        self.last_usage = reply.usage
        self.usage = self.usage + reply.usage

        raw_items = reply.data.get("constraints", [])
        if not isinstance(raw_items, list):
            logger.warning(
                "LLM reply for the system prompt of %s: 'constraints' is not a list, ignoring", prompt.agent_id
            )
            return []
        constraints: list[Constraint] = []
        for raw in raw_items:
            try:
                item = ConstraintItem.model_validate(raw)
            except ValidationError as exc:
                logger.warning(
                    "Dropping invalid constraint of %s: %s", prompt.agent_id, exc.errors(include_url=False)
                )
                continue
            # Deterministic id: re-running on the same session yields the same ids.
            constraint_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{prompt.id}|constraint|system|{item.constraint_text}"))
            constraints.append(
                Constraint(
                    **item.model_dump(),
                    id=constraint_id,
                    origin="system",
                    read_from=node.node_id,
                    agent_id=prompt.agent_id,
                    system_prompt_id=prompt.id,
                    truncated=prompt.truncated,
                )
            )
        return constraints
