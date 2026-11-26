"""Link a task to the capabilities that support it: ``supports`` (AgentCapability -> Task).

An ``AgentCapability`` says what an agent is able to do (read from its system prompt). A task is the work the
agent is given. ``CapabilityTaskRelationExtractor`` asks an LLM which of the capabilities of the agent
support the task (the work the task asks for falls under what they describe) and records one ``supports``
link for each. That is all it does: a task may get one link, several, or none, and what a missing link
means (the agent was given something it cannot do, or it has no capability) is not decided here.

Two kinds of tasks are linked, each to the capabilities of its agent:

* a ``Task`` (assigned to an agent call): the capabilities of the agent of that call, found through
  ``has_capability`` (AgentCall -> AgentCapability, see ``SymbolicExtractor._link_capabilities``);
* a ``SystemTask`` (a standing task read from the system prompt): the capabilities of the same agent.
  It is shared by every call of the agent, so it is linked once, not once per call.

One request per task. A task whose agent has no capability is not submitted (nothing to link it to): it has
no link, like a task that no capability supports. See ``SymbolicExtractor._evaluate_capabilities``, which
runs this extractor as soon as the task and the capabilities of its agent are both in the store (the
capabilities are read from the first LLM call of the agent, after its AgentCall node).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel

from symbolic_extractor.llm import LLMClient, usage_of
from symbolic_extractor.models import AgentCapability, SymbolicStateElement, SystemTask, Task
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import HAS_CAPABILITY, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Store relation recorded by ``CapabilityTaskRelationExtractor.extract``: capability -> the task (or standing
#: task) it supports (``mas:supports`` of the ontology, not defined yet).
SUPPORTS = "supports"

SYSTEM_PROMPT = """\
You are given a TASK given to an agent of a multi-agent system (the work to be done, with the acceptance \
criteria when it has some) and numbered CAPABILITIES (C1, C2, ...): what that agent is able to do, \
according to its system prompt. Link the task to the capabilities that support it.

A capability supports the task when carrying out the task, or a part of it, is the kind of work that \
capability describes: the agent is able to do that work. A task can need several capabilities: name all \
of them.

A capability does NOT support the task when it only shares the subject or the place with it, or when it \
describes other work than what the task asks for. The task can ask for something that none of the \
capabilities covers (for example it asks for hotels while the agent is only able to find routes): then no \
capability supports it, reply []. When the task is only partly covered, name the capabilities that cover \
that part and say in the reasoning which part is not covered.

Judge only from the texts. Do not use outside knowledge, and do not judge whether the agent should have \
been given the task or whether it did it well.

Reply with a JSON object only: {"supports": ["C1"], "reasoning": "one sentence"}. "supports" lists the \
labels of the capabilities that support the task, [] when none does. Do not answer with anything else."""


def retry_reminder() -> str:
    """Added to the request that retries a reply that was not usable."""
    return (
        '\n\nYour previous reply was not valid. Reply with a JSON object with the key "supports" whose '
        'value is a list of capability labels ([] when none), and "reasoning", for example '
        '{"supports": ["C1"], "reasoning": "..."}.'
    )


class CapabilityEvaluation(BaseModel):
    """The verdict on one task: which capabilities of its agent support it."""

    #: Id of the ``Task`` or ``SystemTask`` that was judged.
    task_id: str
    #: Ids of the capabilities it was compared with (all those of its agent).
    capability_ids: list[str]
    #: Ids of the capabilities that support it (``[]`` when none does).
    supports: list[str] = []
    reasoning: str = ""

    @property
    def supported(self) -> bool:
        return bool(self.supports)


def render_prompt(task: Task | SystemTask, capabilities: Sequence[AgentCapability]) -> str:
    criteria = (
        "\n".join(f"- {c}" for c in task.acceptance_criteria) if task.acceptance_criteria else "(none stated)"
    )
    listed = "\n".join(f"C{i}. {c.capability_text}" for i, c in enumerate(capabilities, start=1))
    return (
        f"=== TASK ===\n{task.task_text}\n\n=== ACCEPTANCE CRITERIA ===\n{criteria}\n\n"
        f"=== CAPABILITIES OF THE AGENT ===\n{listed}\n"
    )


def parse_evaluation(data: dict, capabilities: Sequence[AgentCapability]) -> tuple[list[AgentCapability], str]:
    """The capabilities the reply says support the task, and the reasoning. Labels that are not capabilities
    are ignored (logged), a label given twice counts once. Raises ``ValueError`` when the reply has no list
    of labels under ``supports``, which is a failed request, not "no capability supports the task"."""
    labels = data.get("supports")
    if not isinstance(labels, list):
        raise ValueError(f"no 'supports' list in the reply: {str(data)[:200]}")
    by_label = {f"C{i}": c for i, c in enumerate(capabilities, start=1)}
    found: list[AgentCapability] = []
    for raw in labels:
        label = str(raw).strip().upper()
        capability = by_label.get(f"C{label}" if label.isdigit() else label)
        if capability is None:
            logger.warning("Capability %r of the reply is not in the list, ignored", raw)
        elif all(capability.id != other.id for other in found):
            found.append(capability)
    return found, str(data.get("reasoning") or "").strip()


class CapabilityTaskRelationExtractor(SemanticRelationExtractor):
    """Judges which capabilities of an agent support a task of that agent, see the module docstring.

    ``extract(store, elements)`` handles the ``Task`` and ``SystemTask`` elements of ``elements`` (others are
    ignored). It returns one ``CapabilityEvaluation`` per task judged and records ``supports`` (capability ->
    task) in the store for the capabilities that support it. The store must hold the capabilities and, for a
    ``Task``, the ``has_capability`` links of its agent call.
    """

    def __init__(self, llm: LLMClient) -> None:
        super().__init__()
        self._llm = llm

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> list[CapabilityEvaluation]:
        """The evaluations, with the ``supports`` relations also recorded in ``store``. ``last_usage`` is
        the total usage. Tasks that are not in the store, or whose agent has no capability, are ignored.
        Raises ``ValueError`` when a reply is unusable twice (the evaluations of the tasks already done
        are kept in the store)."""
        self.last_usage = LLMUsage()
        evaluations: list[CapabilityEvaluation] = []
        for element in elements:
            if not isinstance(element, (Task, SystemTask)) or store.get(element.id) is None:
                continue
            capabilities = self.candidates(store, element)
            if not capabilities:
                continue
            evaluation = self.evaluate(element, capabilities)
            for capability_id in evaluation.supports:
                store.link(SUPPORTS, capability_id, element.id)
            evaluations.append(evaluation)
        return evaluations

    @staticmethod
    def candidates(store: SymbolicSessionStore, task: Task | SystemTask) -> list[AgentCapability]:
        """The capabilities of the agent of ``task``: through ``has_capability`` of its agent call for a
        ``Task``, through ``agent_id`` for a ``SystemTask``. Empty when the agent has none (yet)."""
        if isinstance(task, SystemTask):
            return [s.element for s in store.by_kind("capabilities") if s.element.agent_id == task.agent_id]
        return [
            stored.element
            for capability_id in store.related(HAS_CAPABILITY, task.owned_by)
            if (stored := store.get(capability_id)) is not None and isinstance(stored.element, AgentCapability)
        ]

    def evaluate(self, task: Task | SystemTask, capabilities: Sequence[AgentCapability]) -> CapabilityEvaluation:
        """Which of ``capabilities`` support ``task``: one request, and one more when the reply is not
        usable (not JSON at all, the client raises ``ValueError``, or JSON that ``parse_evaluation``
        rejects). The usage of every request that returned is added to ``last_usage``. API errors are not
        caught. Raises ``ValueError`` when the second reply is unusable too."""
        prompt = render_prompt(task, capabilities)
        for attempt in (1, 2):
            try:
                reply = self._llm.complete_json(SYSTEM_PROMPT, prompt if attempt == 1 else prompt + retry_reminder())
            except ValueError as exc:
                self.last_usage = self.last_usage + usage_of(exc)
                if attempt == 2:
                    raise
                logger.warning("Reply is not valid JSON, asking again: %.200s", exc)
                continue
            self.last_usage = self.last_usage + reply.usage
            try:
                found, reasoning = parse_evaluation(reply.data, capabilities)
            except ValueError:
                if attempt == 2:
                    raise
                logger.warning("Unusable reply, asking again: %.200s", str(reply.data))
                continue
            return CapabilityEvaluation(
                task_id=task.id,
                capability_ids=[c.id for c in capabilities],
                supports=[c.id for c in found],
                reasoning=reasoning,
            )
        raise AssertionError("unreachable")
