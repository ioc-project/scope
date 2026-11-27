"""Standing tasks of an agent, extracted from its system prompt (one LLM request per agent).

A system prompt describes a role: who the agent is, which tools it has, and the duties it carries
out every time it is called ("report the findings with these checks", "route to each agent at
least once"). Those duties are ``SystemTask`` elements. They are not the ``Task`` of an agent call:
nobody assigns them in one call, they have no status, and they are shared by every call of the
agent (the pipeline links them to each of its ``AgentCall`` nodes, see
``SymbolicExtractor._link_system_tasks``).

``SystemTaskExtractor`` runs on the LLM call from which ``AgentSystemPromptExtractor`` has just read
the system prompt of an agent (it must come after it in the list of extractors): that is the only
call it does anything on, so the LLM is asked once per agent and session, and later calls of the
agent reuse the tasks without a request. A prompt that holds no duty gives no task, and is not asked
again.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import ValidationError

from symbolic_extractor.extractors.claims import _clip
from symbolic_extractor.llm import LLMClient
from symbolic_extractor.models import AgentSystemPrompt, KGNode, SystemTask, SystemTaskItem
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You extract the STANDING TASKS of an agent from its system prompt, in a multi-agent system.

A system prompt describes the role of an agent. A standing task is a duty the prompt gives the \
agent every time it is called: a piece of work it must carry out, with the deliverable expected \
from it. Test: does the prompt tell the agent to do something and to hand back something?
Example: "Check the logs of the service and report the likely cause with the evidence" -> task \
"Report the likely cause of the problem in the service, with the evidence from its logs".

Not a task: the persona or identity ("You are an itinerary specialist"), the list of tools and how \
to call them, the tone and style, general background, safety boilerplate. They are context: use \
them only to write a task self-contained, never as a task of their own.

The role statement usually says the kind of work the agent does ("... helps users with data-layer \
tasks: investigating query performance, diagnosing connection issues ..."). That work is the main \
task of the agent: write it as a task, even when the prompt is mostly about how to behave. \
Working rules (be concise, state uncertainty, use the skills, ask before risky actions, call \
tools in parallel) are not tasks: when they constrain the deliverable of a task they become its \
acceptance criteria, otherwise they are dropped.

For each task return:
- task_text: the duty and the deliverable expected, as one short, self-contained sentence written \
as an assignment. Stay faithful to the prompt and add nothing it does not say.
- acceptance_criteria: the conditions the prompt states for the deliverable to count as done \
(checks to make, required content, format, limits, rules to respect). Only what the prompt \
states: an empty list when it states none. Never invent criteria.

Rules:
- One task per distinct duty. A role usually has one to three; merge the steps of the same \
procedure, and the kinds of work listed in one sentence, into one task instead of one task per \
step. Never repeat a task.
- If the prompt gives no duty (only an identity, or tools), return an empty list.
- The header may say that the prompt is cut off: it is then only the beginning, extract from what \
is there and do not guess the rest.

Reply with a JSON object only:
{"tasks": [{"task_text": "...", "acceptance_criteria": ["..."]}]}
"""


def render_system_task_prompt(prompt: AgentSystemPrompt) -> str:
    """The user prompt that asks for the standing tasks in ``prompt``."""
    return (
        f"Agent: {prompt.agent_id}\n"
        + ("System prompt looks cut off: yes\n" if prompt.truncated else "")
        + "\n=== SYSTEM PROMPT OF THE AGENT (extract the standing tasks from this) ===\n"
        + f"{_clip(prompt.system_prompt)}\n"
    )


def agent_system_tasks(store: SymbolicSessionStore, agent_id: str) -> list[SystemTask]:
    """The standing tasks stored for ``agent_id`` (empty when none)."""
    return [
        s.element
        for s in store.by_kind("system_tasks")
        if isinstance(s.element, SystemTask) and s.element.agent_id == agent_id
    ]


class SystemTaskExtractor:
    """The standing tasks of an agent, from the system prompt just read from an LLM call.

    Same interface as ``ElementExtractor``. It needs the ``AgentSystemPromptExtractor`` to run first
    on the same node (the extractors of a node run in order): it looks in the store for the system
    prompt read from this very call, so it does nothing on any other call, and nothing at all when
    there is no such extractor or no readable prompt (webex).
    """

    element_key = "system_tasks"
    node_types = frozenset({"LLMCall"})

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        #: Usage of the last ``extract`` call (zero when no request was made).
        self.last_usage = LLMUsage()
        #: Cumulative usage of this extractor.
        self.usage = LLMUsage()

    def applies_to(self, node: KGNode) -> bool:
        return node.node_type in self.node_types

    def extract(self, node: KGNode, store: SymbolicSessionStore | None = None) -> list[SystemTask]:
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
        # for "no tasks" (the agent would never be asked again).
        reply = self._llm.complete_json(SYSTEM_PROMPT, render_system_task_prompt(prompt))
        self.last_usage = reply.usage
        self.usage = self.usage + reply.usage

        raw_items = reply.data.get("tasks", [])
        if not isinstance(raw_items, list):
            logger.warning(
                "LLM reply for the system prompt of %s: 'tasks' is not a list, ignoring",
                prompt.agent_id,
            )
            return []
        tasks: list[SystemTask] = []
        for raw in raw_items:
            try:
                item = SystemTaskItem.model_validate(raw)
            except ValidationError as exc:
                logger.warning(
                    "Dropping invalid system task of %s: %s",
                    prompt.agent_id,
                    exc.errors(include_url=False),
                )
                continue
            # Deterministic id: re-running on the same session yields the same ids.
            task_id = str(
                uuid.uuid5(uuid.NAMESPACE_OID, f"{prompt.id}|system_task|{item.task_text}")
            )
            tasks.append(
                SystemTask(
                    **item.model_dump(),
                    id=task_id,
                    agent_id=prompt.agent_id,
                    system_prompt_id=prompt.id,
                    read_from=node.node_id,
                    truncated=prompt.truncated,
                )
            )
        return tasks
