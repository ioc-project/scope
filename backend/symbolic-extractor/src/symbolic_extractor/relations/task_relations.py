"""Relations from a task to what it serves: ``implements`` (task -> task) and ``achieves``
(task -> goal).

A task T1 owned by an agent call implements the task of the closest agent call before it that has
a task (the one that handed the work over, passing over calls without task): T1 is the part of that
assignment its agent has to do. The order is not decided here: it is the ``preceded_by`` relation
between tasks, recorded beforehand by ``SymbolicExtractor._link_preceding_tasks``. For each task T1:

* the predecessor has a task identical to T1 (``same_as``): nothing is asked, ``same_as`` already
  says it, and no ``implements`` is added;
* the predecessor has other tasks: an LLM says which of them T1 implements (none, one or several);
* there is no task before it, the root being reached without one: T1 is at the top level, and the
  LLM says whether it achieves the goal of the session (``achieves``). The assumption is that the
  work of a top-level agent call serves the goal.

The relations are recorded in the store as ``implements`` (task -> task) and ``achieves`` (task ->
goal). One request per task, none when there is nothing to compare it with.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from symbolic_extractor.llm import LLMClient, usage_of
from symbolic_extractor.models import Goal, SymbolicStateElement, Task
from symbolic_extractor.relations.claim_claim_relations import SAME_AS
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import PRECEDED_BY, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Store relation recorded by ``TaskRelationExtractor.extract``: task -> the task it implements.
IMPLEMENTS = "implements"
#: Store relation recorded by ``TaskRelationExtractor.extract``: task -> the goal it achieves.
ACHIEVES = "achieves"

_PROMPT_TEMPLATE = """\
You are given a TASK assigned to an agent of a multi-agent system, and numbered CANDIDATES \
(C1, C2, ...): @CANDIDATES@. Decide which candidates the TASK @VERB@.

The TASK @VERB@ a candidate when carrying it out is a step towards fulfilling that \
candidate: the candidate is the wider assignment, or the end state that is wanted, and the TASK \
is the part of it that this agent has to do (a sub-step, a check, an investigation, a piece of \
the deliverable). Judge from the texts only; do not use outside knowledge.

A TASK that gathers the information, analysis or assessment that the candidate needs in order \
to be fulfilled is a step towards it, so it @VERB@ it, even if it is only one of several \
steps and does not complete it alone.

The TASK does NOT @BARE@ a candidate when it only shares the subject with it, when it is the \
wider one of the two (the wrong direction), or when doing it would not move the candidate \
forward.

Reply with a JSON object only: {"@VERB@": ["C2"], "reasoning": "one sentence"}. \
"@VERB@" lists the labels of the candidates the TASK @VERB@, [] when there is none. \
Do not answer with anything else."""


def _prompt(verb: str, bare: str, candidates: str) -> str:
    return (
        _PROMPT_TEMPLATE.replace("@VERB@", verb).replace("@BARE@", bare).replace("@CANDIDATES@", candidates)
    )


#: System prompt to judge ``implements``: the candidates are tasks.
SYSTEM_PROMPT = _prompt(IMPLEMENTS, "implement", "the tasks of the agent that handed the work over to it")
#: System prompt to judge ``achieves``: the candidate is the goal of the session.
GOAL_SYSTEM_PROMPT = _prompt(ACHIEVES, "achieve", "the goal of the whole session")


def retry_reminder(relation: str) -> str:
    """Added to the request that retries a reply that had no usable list under the key ``relation``."""
    return (
        f'\n\nYour previous reply was not valid. Reply with a JSON object with the key "{relation}" '
        f'whose value is a list of labels ([] when none), for example {{"{relation}": []}}.'
    )


Candidate = Task | Goal


class TaskRelation(BaseModel):
    """A task that implements a task of the preceding agent call, or achieves the goal."""

    task_id: str
    target_id: str
    #: What ``target_id`` is: a ``task`` (of the preceding agent call) or the session ``goal``.
    target: Literal["task", "goal"]
    #: ``implements`` for a task target, ``achieves`` for the goal.
    relation: Literal["implements", "achieves"] = "implements"
    reasoning: str = ""


def _relation_of(candidates: Sequence[Candidate]) -> str:
    """The relation judged against ``candidates``: ``achieves`` for the goal, else ``implements``."""
    return ACHIEVES if candidates and isinstance(candidates[0], Goal) else IMPLEMENTS

def _describe(element: Candidate) -> str:
    if isinstance(element, Goal):
        text, label, criteria = element.goal_text, "Success criteria", element.success_criteria
    else:
        text, label, criteria = element.task_text, "Acceptance criteria", element.acceptance_criteria
    return f"{text} ({label}: {'; '.join(criteria)})" if criteria else text


def render_prompt(task: Task, candidates: Sequence[Candidate]) -> str:
    if candidates and isinstance(candidates[0], Goal):
        header = "CANDIDATES: THE GOAL OF THE SESSION (what the user wants)"
    else:
        header = "CANDIDATES: TASKS OF THE AGENT THAT HANDED THE WORK OVER"
    listed = "\n".join(f"C{i}. {_describe(c)}" for i, c in enumerate(candidates, start=1))
    return f"=== TASK ===\n{_describe(task)}\n\n=== {header} ===\n{listed}\n"


def parse_relation(data: dict, candidates: Sequence[Candidate]) -> tuple[list[Candidate], str]:
    """The candidates the reply says the task implements (or achieves, for the goal), and the
    reasoning. Labels that are not candidates are ignored (logged), a label given twice counts
    once. Raises ``ValueError`` when the reply has no list of labels under the key of the
    relation, which is a failed request, not "relates to nothing"."""
    key = _relation_of(candidates)
    labels = data.get(key)
    if not isinstance(labels, list):
        raise ValueError(f"no '{key}' list in the reply: {str(data)[:200]}")
    by_label = {f"C{i}": c for i, c in enumerate(candidates, start=1)}
    found: list[Candidate] = []
    for raw in labels:
        label = str(raw).strip().upper()
        candidate = by_label.get(f"C{label}" if label.isdigit() else label)
        if candidate is None:
            logger.warning("Candidate %r of the reply is not in the list, ignored", raw)
        elif all(candidate.id != other.id for other in found):
            found.append(candidate)
    return found, str(data.get("reasoning") or "").strip()


class TaskRelationExtractor(SemanticRelationExtractor):
    """Finds the ``implements`` (task -> task) and ``achieves`` (task -> goal) relations of tasks,
    see the module docstring.

    ``extract(store, elements)`` handles the tasks of ``elements`` (other elements are ignored). It
    needs the store to hold the goal and the tasks with their ``preceded_by`` links (the earlier
    tasks of the trajectory); without ``preceded_by`` every task looks like a top-level one.
    """

    def __init__(self, llm: LLMClient) -> None:
        super().__init__()
        self._llm = llm

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> list[TaskRelation]:
        """The relations found, also recorded in ``store``. ``last_usage`` is the total usage.
        Tasks that are not in the store are ignored. Raises ``ValueError`` when a reply has no
        list of labels (the relations of the tasks already done are kept)."""
        self.last_usage = LLMUsage()
        relations: list[TaskRelation] = []
        for element in elements:
            if not isinstance(element, Task) or store.get(element.id) is None:
                continue
            candidates = self.candidates(store, element)
            if not candidates:
                continue
            found = self.relate(element, candidates)
            for relation in found:
                store.link(relation.relation, relation.task_id, relation.target_id)
            relations.extend(found)
        return relations

    @staticmethod
    def candidates(store: SymbolicSessionStore, task: Task) -> list[Candidate]:
        """What ``task`` is compared with: the tasks it is ``preceded_by``, or the goal(s) when it has
        none (no predecessor, or no task in it). Empty when nothing has to be asked: one of those
        tasks is identical to ``task`` (``same_as``), or there is no goal."""
        earlier = [
            stored.element
            for other_id in store.related(PRECEDED_BY, task.id)
            if (stored := store.get(other_id)) is not None and isinstance(stored.element, Task)
        ]
        if earlier:
            same = set(store.related(SAME_AS, task.id))
            return [] if any(t.id in same for t in earlier) else earlier
        return [s.element for s in store.by_kind("goals") if isinstance(s.element, Goal)]

    def relate(self, task: Task, candidates: Sequence[Candidate]) -> list[TaskRelation]:
        """The candidates ``task`` implements (or achieves, when the candidate is the goal): one
        request, and one more when the reply is not usable (not JSON at all, the client raises
        ``ValueError``, or JSON without a list under the key of the relation). The usage of every
        request that returned is added to ``last_usage``. API errors are not caught. Raises
        ``ValueError`` when the second reply is unusable too."""
        relation = _relation_of(candidates)
        system = GOAL_SYSTEM_PROMPT if relation == ACHIEVES else SYSTEM_PROMPT
        prompt = render_prompt(task, candidates)
        for attempt in (1, 2):
            try:
                reply = self._llm.complete_json(
                    system, prompt if attempt == 1 else prompt + retry_reminder(relation)
                )
            except ValueError as exc:
                self.last_usage = self.last_usage + usage_of(exc)
                if attempt == 2:
                    raise
                logger.warning("Reply is not valid JSON, asking again: %.200s", exc)
                continue
            self.last_usage = self.last_usage + reply.usage
            try:
                found, reasoning = parse_relation(reply.data, candidates)
            except ValueError:
                if attempt == 2:
                    raise
                logger.warning("Reply without a %s list, asking again: %.200s", relation, str(reply.data))
                continue
            return [
                TaskRelation(
                    task_id=task.id,
                    target_id=candidate.id,
                    target="goal" if isinstance(candidate, Goal) else "task",
                    relation=relation,
                    reasoning=reasoning,
                )
                for candidate in found
            ]
        raise AssertionError("unreachable")
