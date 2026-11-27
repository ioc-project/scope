"""Which claims fulfil a task? ``fulfils`` (claim -> task).

The claims ``produced_by`` a task (see ``SymbolicExtractor._link_produced_by``) are what the agent
that owns the task said while it worked on it. ``TaskClaimRelationExtractor`` asks an LLM which of
them fulfil the task: which state the deliverable the task asks for, or what an acceptance
criterion requires. The others are the agent's process (searching, announcing, restating the
request) or content outside the deliverable.

It works per LLM call: the claims of one LLM call (one node) are judged against each task they are
``produced_by``, in one request per task. An agent call has several LLM calls, so a task is judged
once for each of them, and each time only on the claims of that call: the deliverable may be
spread over several calls, the judge only sees a part of it.

Each claim named by the LLM gets a ``fulfils_task`` relation to the task in the store. The ontology
property is ``mas:fulfils`` for both goals and tasks; the store keeps them apart (``fulfils`` is
Claim -> Goal, see ``GoalClaimRelationExtractor``). A request where no claim fulfils the task is
still a verdict: ``TaskEvaluation.fulfils`` is empty.

Ordering: the ``produced_by`` link of a claim exists only when the task of the agent call is in
the store, i.e. the AgentCall node must be processed before the LLM calls it owns. See
``SymbolicExtractor._extract_node_relations``, which runs this extractor.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel

from symbolic_extractor.llm import LLMClient, usage_of
from symbolic_extractor.models import Claim, SymbolicStateElement, Task
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import PRODUCED_BY, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Store relation recorded by ``TaskClaimRelationExtractor.extract``: claim -> the task it fulfils
#: (``mas:fulfils`` of the ontology, with a Task as target).
FULFILS_TASK = "fulfils_task"

SYSTEM_PROMPT = """\
You are given a TASK assigned to an agent of a multi-agent system (the work to be done, with the \
acceptance criteria when it has some) and numbered CLAIMS (C1, C2, ...): the statements the agent \
made while it worked on the task. They are only some of the claims the agent made for the task \
(those of one step of its work), the task may be fulfilled by claims you do not see. Decide which \
of the claims you are given fulfil the task.

A claim fulfils the task when it states part of the deliverable the task asks for, or something \
that an acceptance criterion requires. The deliverable can be spread over several claims: name \
all of them.

A claim does NOT fulfil the task when it is only about the agent's own process (it is searching, \
checking, about to answer), repeats the request, comments on the work without delivering any of \
it, or is about something the task did not ask for.

Judge only from the texts. Do not use outside knowledge and do not judge whether the claims are \
true.

Reply with a JSON object only: {"fulfils": ["C1", "C3"], "reasoning": "one sentence"}. "fulfils" \
lists the labels of the claims that fulfil the task, [] when none does. Do not answer with \
anything else."""


def retry_reminder() -> str:
    """Added to the request that retries a reply that was not usable."""
    return (
        '\n\nYour previous reply was not valid. Reply with a JSON object with the key "fulfils" whose '
        'value is a list of claim labels ([] when none), and "reasoning", for example '
        '{"fulfils": ["C1"], "reasoning": "..."}.'
    )


class TaskEvaluation(BaseModel):
    """The verdict on one task for a set of claims: which of them fulfil it."""

    task_id: str
    #: Ids of the claims that were judged: the ones given to ``extract`` that are ``produced_by`` the task.
    claim_ids: list[str]
    #: Ids of the claims that fulfil the task (``[]`` when none does).
    fulfils: list[str] = []
    reasoning: str = ""


def render_prompt(task: Task, claims: Sequence[Claim]) -> str:
    criteria = (
        "\n".join(f"- {c}" for c in task.acceptance_criteria)
        if task.acceptance_criteria
        else "(none stated)"
    )
    listed = "\n".join(f"C{i}. {c.claim_text}" for i, c in enumerate(claims, start=1))
    return (
        f"=== TASK ===\n{task.task_text}\n\n=== ACCEPTANCE CRITERIA ===\n{criteria}\n\n"
        f"=== CLAIMS OF THE AGENT ===\n{listed}\n"
    )


def parse_evaluation(data: dict, claims: Sequence[Claim]) -> tuple[list[Claim], str]:
    """The claims the reply says fulfil the task, and the reasoning. Labels that are not claims are
    ignored (logged), a label given twice counts once. Raises ``ValueError`` when the reply has no
    list of labels under ``fulfils``, which is a failed request, not "no claim fulfils the task"."""
    labels = data.get("fulfils")
    if not isinstance(labels, list):
        raise ValueError(f"no 'fulfils' list in the reply: {str(data)[:200]}")
    by_label = {f"C{i}": c for i, c in enumerate(claims, start=1)}
    found: list[Claim] = []
    for raw in labels:
        label = str(raw).strip().upper()
        claim = by_label.get(f"C{label}" if label.isdigit() else label)
        if claim is None:
            logger.warning("Claim %r of the reply is not in the list, ignored", raw)
        elif all(claim.id != other.id for other in found):
            found.append(claim)
    return found, str(data.get("reasoning") or "").strip()


class TaskClaimRelationExtractor(SemanticRelationExtractor):
    """Judges which claims produced by a task fulfil it, see the module docstring.

    ``extract(store, elements)`` handles the tasks and claims of ``elements`` (other elements are
    ignored): for each task, the claims of ``elements`` that are ``produced_by`` it in the store are
    judged in one request. A task with no such claim is skipped (nothing to judge). It returns one
    ``TaskEvaluation`` per task judged and records ``fulfils_task`` (claim -> task) for the claims
    that fulfil it.
    """

    def __init__(self, llm: LLMClient) -> None:
        super().__init__()
        self._llm = llm

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> list[TaskEvaluation]:
        """The evaluations, with the ``fulfils_task`` relations also recorded in ``store``.
        ``last_usage`` is the total usage. Tasks that are not in the store are ignored. Raises
        ``ValueError`` when a reply is unusable twice (the evaluations of the tasks already done are
        kept in the store)."""
        self.last_usage = LLMUsage()
        evaluations: list[TaskEvaluation] = []
        given = [e for e in elements if isinstance(e, Claim) and store.get(e.id) is not None]
        for element in elements:
            if not isinstance(element, Task) or store.get(element.id) is None:
                continue
            claims = [c for c in given if element.id in store.related(PRODUCED_BY, c.id)]
            if not claims:
                continue
            evaluation = self.evaluate(element, claims)
            for claim_id in evaluation.fulfils:
                store.link(FULFILS_TASK, claim_id, element.id)
            evaluations.append(evaluation)
        return evaluations

    def evaluate(self, task: Task, claims: Sequence[Claim]) -> TaskEvaluation:
        """Which of ``claims`` fulfil ``task``: one request, and one more when the reply is not
        usable (not JSON at all, the client raises ``ValueError``, or JSON that ``parse_evaluation``
        rejects). The usage of every request that returned is added to ``last_usage``. API errors
        are not caught. Raises ``ValueError`` when the second reply is unusable too."""
        prompt = render_prompt(task, claims)
        for attempt in (1, 2):
            try:
                reply = self._llm.complete_json(
                    SYSTEM_PROMPT, prompt if attempt == 1 else prompt + retry_reminder()
                )
            except ValueError as exc:
                self.last_usage = self.last_usage + usage_of(exc)
                if attempt == 2:
                    raise
                logger.warning("Reply is not valid JSON, asking again: %.200s", exc)
                continue
            self.last_usage = self.last_usage + reply.usage
            try:
                found, reasoning = parse_evaluation(reply.data, claims)
            except ValueError:
                if attempt == 2:
                    raise
                logger.warning("Unusable reply, asking again: %.200s", str(reply.data))
                continue
            return TaskEvaluation(
                task_id=task.id,
                claim_ids=[c.id for c in claims],
                fulfils=[c.id for c in found],
                reasoning=reasoning,
            )
        raise AssertionError("unreachable")
