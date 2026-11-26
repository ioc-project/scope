"""Goal extraction. Definitions follow ``cognitive-ontology.ttl`` (mas:Goal).

The ``Goal`` / ``GoalItem`` objects are defined in ``symbolic_extractor.models``.

A goal is a desired end state with the criteria its combined outcome is judged against. It has no
assignee: it is what the user asked the system for, so it is read from the ``Conversation`` node
of the session (see ``SessionData.conversation_node``): its ``query`` (the initial State of the
Session) defines the goal and its ``response`` (the final State) sets the status. A job delegated
from one agent to another is a task (see ``tasks.py``), not a goal.

TODO: multi-turn sessions. One goal is extracted per session; when the traces hold several user
interactions, each turn will be its own ``Conversation`` node and so its own goal.
TODO: once the tasks are linked to the goal, derive the status from theirs ("decomposed",
"achieved", ...) instead of the final response only.
"""

from __future__ import annotations

import json
import uuid

from symbolic_extractor.extractors.base import ExtractionPrompt
from symbolic_extractor.extractors.claims import _clip, _first_text
from symbolic_extractor.models import CONVERSATION_NODE_TYPE, Goal, GoalItem, KGNode

SYSTEM_PROMPT = """\
You extract the GOAL of a session of a multi-agent system, from the request the user sent to \
the system and from the response the system finally returned.

A goal is a desired end state together with its success criteria. It has no assignee: it is \
the standard against which the combined outcome of the whole system is judged, not a job given \
to one agent. Test: can the whole outcome be checked yes/no against it?
Example: "A complete weekend itinerary for 2 in Lisbon, including transport and lodging, total \
cost <= EUR 800."

The request of the user is, by definition, what the system is asked to achieve, so it holds a \
goal. A question or an information request is a goal too: the desired end state is that the \
user gets a correct and complete answer to it.
Example: "Is the ferry to Porto Alto cheaper than the bus?" -> goal_text "The user knows \
whether the ferry to Porto Alto is cheaper than the bus."; criteria: "Gives the ferry price", \
"Gives the bus price", "States which one is cheaper"; inferred: true.

For each goal return:
- goal_text: the desired end state as one short, self-contained sentence. Stay faithful to the \
wording and meaning of the request and do not add information it does not contain.
- success_criteria: a list of short, checkable conditions that the outcome must satisfy for the \
goal to count as met (requirements, constraints, budgets, deadlines, format). Use the ones \
stated in the request. At least one: when the request states none, derive them from the goal \
(for a question, the answer must be correct and complete) and set "inferred" to true.
- rationale: optional, one sentence for a human reader on why this is the goal or how the \
criteria were read. null when there is nothing to add.
- inferred: true when the goal itself or its criteria are not stated in the request but deduced \
from it, false when the request states them.
- status, judged from the response only:
    * "set": the default. Use it when the response is empty, or is only a routing or technical \
message (for example a command such as {"goto": "__end__"}) that says nothing about the result.
    * "achieved": the response answers the request and meets the success criteria.
    * "abandoned": the response states that the goal is dropped or cannot be met.
  Do not use "decomposed".

Rules:
- Extract only from the section "REQUEST OF THE USER". The response section is only there to set \
the status; never extract a goal from it.
- The request may start with a technical prefix such as "input:<agent>:"; ignore it.
- One goal per desired end state; never repeat a goal. Usually there is exactly one.
- Return an empty list only when the request is empty or has no meaning (for example a bare \
technical marker such as "agent-resume-2"). A question or a task for the system is never empty: \
extract its goal, do not judge whether the request is worth pursuing.

Reply with a JSON object only:
{"goals": [{"goal_text": "...", "success_criteria": ["..."], "rationale": "...|null", \
"inferred": true|false, "status": "set|achieved|abandoned"}]}
"""


def _user_request(raw: str) -> str:
    """The text of the human message(s) when ``raw`` is a LangGraph-style call input
    (``{"args": [{"messages": [{"type": "HumanMessage", "content": "..."}]}]}``), else ``raw``
    unchanged: any other shape is passed to the LLM as it is."""
    try:
        data = json.loads(raw)
        texts = [
            message["content"].strip()
            for arg in data["args"]
            for message in arg["messages"]
            if message["type"] == "HumanMessage" and isinstance(message["content"], str)
        ]
    except (TypeError, ValueError, KeyError, AttributeError):
        return raw
    return "\n".join(t for t in texts if t) or raw


def _final_answer(raw: str) -> str:
    """The ``final_answer`` text when ``raw`` is a JSON object holding one, else ``raw`` unchanged."""
    try:
        answer = json.loads(raw)["final_answer"]
    except (TypeError, ValueError, KeyError):
        return raw
    return answer if isinstance(answer, str) and answer.strip() else raw


class GoalExtractionPrompt(ExtractionPrompt[GoalItem, Goal]):
    """The goal is what the user asked for, so only the ``Conversation`` node applies."""

    element_key = "goals"
    node_types = frozenset({CONVERSATION_NODE_TYPE})
    item_model = GoalItem
    system_prompt = SYSTEM_PROMPT

    def render(self, node: KGNode) -> str | None:
        query = _first_text(node.node, "query")
        if not query.strip():
            return None
        request = _user_request(query)
        response = _final_answer(_first_text(node.node, "response"))
        return (
            f"=== REQUEST OF THE USER (extract goals from this) ===\n{_clip(request)}\n\n"
            f"=== RESPONSE OF THE SYSTEM (only to set the status) ===\n{_clip(response) or '(empty)'}\n"
        )

    def build_element(self, item: GoalItem, node: KGNode) -> Goal:
        # Deterministic id: re-running on the same session yields the same goal ids.
        goal_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{node.node_id}|goal|{item.goal_text}"))
        return Goal(**item.model_dump(), id=goal_id)
