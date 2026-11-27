"""Does the final answer satisfy the goal? ``fulfils`` (claim -> goal) and the status of the goal.

The claims of the final answer of a session (the ones with an ``answers`` relation to the goal, see
``SymbolicExtractor._link_answers``) say what the system delivered. ``GoalClaimRelationExtractor``
asks an LLM whether those claims, taken together, meet the success criteria of the goal. The set is
judged as a whole, in one request per goal: a criterion is met by what some claims say, so a single
claim cannot be judged alone.

When the set satisfies the goal, the LLM names the claims that carry the satisfaction and each of
them gets a ``fulfils`` relation to the goal in the store. When it does not, nothing is linked.

The same request also gives the status of the goal, with the values of the ontology
(``mas:GoalStatusShape``): ``achieved`` when the goal is satisfied; ``abandoned`` when the claims
say that it is dropped or cannot be met; ``set`` when it is not met but not given up. The extractor
replaces the goal in the store with the new status, which overrides the one the goal extractor
guessed from the final response alone. The verdict is also in the returned ``GoalEvaluation``.

It must only run once the claims of the final answer are extracted: before that the set is empty or
incomplete. ``SymbolicExtractor`` takes care of it (``_evaluate_goals``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel

from symbolic_extractor.llm import LLMClient, usage_of
from symbolic_extractor.models import Claim, Goal, GoalStatus, SymbolicStateElement
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import ANSWERS, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

#: Store relation recorded by ``GoalClaimRelationExtractor.extract``: claim -> the goal it fulfils
#: (``mas:fulfils`` of the ontology).
FULFILS = "fulfils"

SYSTEM_PROMPT = """\
You are given the GOAL of a multi-agent system (what the user wants, with its success criteria) \
and numbered ANSWER CLAIMS (C1, C2, ...): the statements of the final answer that the system \
gave. Decide whether the claims, taken together, satisfy the goal: every success criterion must \
be met by what the claims say.

Judge only from the claims. Do not use outside knowledge and do not judge whether the claims are \
true. A criterion that no claim addresses is not met. A criterion that a claim only partly \
covers, or that a claim contradicts, is not met either.

Reply with a JSON object only: {"satisfied": true, "status": "achieved", "fulfils": ["C1", \
"C3"], "reasoning": "one sentence"}.
- "satisfied" is true only when every criterion is met.
- "status" is the status of the goal after this answer: "achieved" when "satisfied" is true; \
"abandoned" when the claims state that the goal is dropped or cannot be met; "set" in every other \
case (the goal is not met, but the answer does not give it up).
- "fulfils" lists the labels of the claims the criteria are met with, and must be [] when \
"satisfied" is false.
Do not answer with anything else."""


def retry_reminder() -> str:
    """Added to the request that retries a reply that was not usable."""
    return (
        '\n\nYour previous reply was not valid. Reply with a JSON object with the keys "satisfied" '
        '(true or false), "status" ("achieved", "abandoned" or "set"), "fulfils" (a list of claim '
        'labels, never empty when "satisfied" is true) and "reasoning", for example '
        '{"satisfied": false, "status": "set", "fulfils": [], "reasoning": "..."}.'
    )


class GoalEvaluation(BaseModel):
    """The verdict on one goal: whether the claims of the final answer satisfy it."""

    goal_id: str
    #: Ids of the claims that were judged: the claims that ``answers`` the goal.
    claim_ids: list[str]
    satisfied: bool
    #: Ids of the claims that carry the satisfaction (``[]`` when not satisfied).
    fulfils: list[str] = []
    #: Status of the goal after the judgement (a value of ``mas:GoalStatusShape``): ``achieved`` when
    #: satisfied, else ``abandoned`` or ``set``.
    status: GoalStatus = "set"
    #: Status the goal had before (the goal extractor's, from the final response alone).
    previous_status: GoalStatus | None = None
    reasoning: str = ""


def render_prompt(goal: Goal, claims: Sequence[Claim]) -> str:
    criteria = "\n".join(f"- {c}" for c in goal.success_criteria)
    listed = "\n".join(f"C{i}. {c.claim_text}" for i, c in enumerate(claims, start=1))
    return (
        f"=== GOAL ===\n{goal.goal_text}\n\n=== SUCCESS CRITERIA ===\n{criteria}\n\n"
        f"=== ANSWER CLAIMS ===\n{listed}\n"
    )


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def parse_evaluation(
    data: dict, claims: Sequence[Claim]
) -> tuple[bool, list[Claim], GoalStatus, str]:
    """``(satisfied, the claims named in "fulfils", status of the goal, reasoning)`` of a reply.
    Labels that are not claims are ignored (logged), a label given twice counts once. A reply that
    is not satisfied names no claim, whatever it lists. The status is ``achieved`` when satisfied
    (whatever the reply says); otherwise ``abandoned`` if the reply says so, else ``set`` (a missing
    or other value is logged and read as ``set``: the goal is not met). Raises ``ValueError`` when
    "satisfied" is not a boolean, or when it is true and no claim is named: the reply is not usable."""
    satisfied = _as_bool(data.get("satisfied"))
    if satisfied is None:
        raise ValueError(f"no boolean 'satisfied' in the reply: {str(data)[:200]}")
    reasoning = str(data.get("reasoning") or "").strip()
    said = str(data.get("status") or "").strip().lower()
    if not satisfied:
        if said not in ("set", "abandoned"):
            logger.warning(
                "Status %r of a goal that is not satisfied, read as 'set'", data.get("status")
            )
        return False, [], "abandoned" if said == "abandoned" else "set", reasoning
    if said != "achieved":
        logger.warning(
            "Status %r of a goal that is satisfied, read as 'achieved'", data.get("status")
        )
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
    if not found:
        raise ValueError(f"'satisfied' is true but no claim is named: {str(data)[:200]}")
    return True, found, "achieved", reasoning


class GoalClaimRelationExtractor(SemanticRelationExtractor):
    """Judges whether the claims of the final answer satisfy the goal, see the module docstring.

    ``extract(store, elements)`` handles the goals of ``elements`` (other elements are ignored): for
    each, the claims that ``answers`` it in the store are judged in one request. A goal that has no
    such claim is skipped (nothing to judge). It returns one ``GoalEvaluation`` per goal judged,
    records ``fulfils`` (claim -> goal) for the claims of a satisfied one, and replaces the goal in
    the store with its new ``status``.
    """

    def __init__(self, llm: LLMClient) -> None:
        super().__init__()
        self._llm = llm

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> list[GoalEvaluation]:
        """The evaluations, with the ``fulfils`` relations and the new goal statuses also recorded in
        ``store``. ``last_usage`` is the total usage. Goals that are not in the store are ignored.
        Raises ``ValueError`` when a reply is unusable twice (the evaluations of the goals already
        done are kept in the store)."""
        self.last_usage = LLMUsage()
        evaluations: list[GoalEvaluation] = []
        for element in elements:
            stored = store.get(element.id)
            if not isinstance(element, Goal) or stored is None:
                continue
            claims = self.answer_claims(store, element)
            if not claims:
                continue
            evaluation = self.evaluate(element, claims)
            for claim_id in evaluation.fulfils:
                store.link(FULFILS, claim_id, element.id)
            if isinstance(stored.element, Goal) and stored.element.status != evaluation.status:
                store.add(
                    "goals",
                    stored.element.model_copy(update={"status": evaluation.status}),
                    stored.node_id,
                )
            evaluations.append(evaluation)
        return evaluations

    @staticmethod
    def answer_claims(store: SymbolicSessionStore, goal: Goal) -> list[Claim]:
        """The claims of the store that ``answers`` ``goal``, in the order they were extracted."""
        return [
            s.element
            for s in store.by_kind("claims")
            if isinstance(s.element, Claim) and goal.id in store.related(ANSWERS, s.id)
        ]

    def evaluate(self, goal: Goal, claims: Sequence[Claim]) -> GoalEvaluation:
        """Whether ``claims`` satisfy ``goal``: one request, and one more when the reply is not
        usable (not JSON at all, the client raises ``ValueError``, or JSON that ``parse_evaluation``
        rejects). The usage of every request that returned is added to ``last_usage``. API errors
        are not caught. Raises ``ValueError`` when the second reply is unusable too."""
        prompt = render_prompt(goal, claims)
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
                satisfied, found, status, reasoning = parse_evaluation(reply.data, claims)
            except ValueError:
                if attempt == 2:
                    raise
                logger.warning("Unusable reply, asking again: %.200s", str(reply.data))
                continue
            return GoalEvaluation(
                goal_id=goal.id,
                claim_ids=[c.id for c in claims],
                satisfied=satisfied,
                fulfils=[c.id for c in found],
                status=status,
                previous_status=goal.status,
                reasoning=reasoning,
            )
        raise AssertionError("unreachable")
