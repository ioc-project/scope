"""Link a claim to a fact: is the claim supported by (derived from) the fact?

For one (fact, claim) pair an LLM answers a yes/no question: does the fact support the claim,
directly or with trivial reasoning? Anything else (the fact says nothing about it, contradicts
it, or is only related to it) is "no". The reply is plain text, YES or NO, no JSON.

``FactClaimLinker`` judges one pair per request. ``BatchFactClaimLinker`` judges a list of
pairs in a single request and returns the answer of each pair by its id.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from symbolic_extractor.llm import TextLLMClient
from symbolic_extractor.models import Claim, Fact, SymbolicStateElement
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import ANNOUNCES, SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage

logger = logging.getLogger(__name__)

ATTEMPTS = 2  # the reply must be YES or NO

#: Store relation recorded by ``FactClaimLinker.extract``: claim -> fact that supports it.
SUPPORTED_BY = "supported_by"

#: Added to every prompt: how to read a fact that comes with the tool, its arguments and the reason.
FACT_CONTEXT_NOTE = """\
The fact may start with the tool that was called, its arguments, and the reason the agent gave \
for calling it, followed by the output. The tool and its arguments say what the output answers \
(for example which values a bare number results from) and may be used to read it. The reason is \
the agent's own words: it is context only, never evidence. What the fact states is its output.
"""

SYSTEM_PROMPT = """\
You are given a FACT (the output of a tool call) and a CLAIM made by an agent. Decide whether \
the claim is derived from the fact: the fact states it, or it follows from the fact with at \
most trivial reasoning (a rewording, a sum, a comparison, a count), and every part of the \
claim is covered.

Use only the fact. Do not use outside knowledge, and do not judge whether the claim is true in \
general. Answer NO when the fact says nothing about the claim, contradicts it, or does not \
decide it (it is about the same subject but needs information the fact does not contain).

""" + FACT_CONTEXT_NOTE + """
Reply with a single word: YES or NO. Nothing else.
"""

#: What is compared with the fact: a ``"claim"`` (one extracted statement) or a ``"text"`` (a
#: longer passage, the source a claim is extracted from, with several statements). The
#: question differs, so each has its own prompts; the pair is labelled CLAIM or TEXT.
Subject = Literal["claim", "text"]

TEXT_SYSTEM_PROMPT = """\
You are given a FACT (the output of a tool call) and a TEXT written by an agent. The text may \
contain several statements, most of them about other things than the fact. Decide whether the \
text contains a statement that is derived from the fact: the fact states it, or it follows \
from the fact with at most trivial reasoning (a rewording, a sum, a comparison, a count), and \
every part of that statement is covered.

Use only the fact. Do not use outside knowledge, and do not judge whether the text is true in \
general. Statements of the text that the fact does not address are ignored. Answer NO when no \
statement of the text is derived from the fact: the fact says nothing about them, contradicts \
them, or does not decide them (they are about the same subject but need information the fact \
does not contain).

""" + FACT_CONTEXT_NOTE + """
Reply with a single word: YES or NO. Nothing else.
"""


class FactClaimLink(BaseModel):
    """A fact-to-claim link: whether ``claim_id`` is backed by ``fact_id``."""

    fact_id: str
    claim_id: str
    backed: bool


def describe_fact(fact: Fact, store: SymbolicSessionStore | None = None) -> str:
    """The text shown to the model for ``fact``: its output, preceded by what makes it readable when
    known: the tool, its arguments, and the claim(s) of the agent that announced the call (``announces``
    in ``store``: "I will calculate the total travel time"). A bare ``5`` then reads as the result of
    ``calculate({"expression": "3 + 2"})``. A fact with none of it is just its text.
    """
    lines: list[str] = []
    if fact.tool_name:
        lines.append(f"Tool: {fact.tool_name}")
    if fact.tool_arguments:
        lines.append(f"Arguments: {json.dumps(fact.tool_arguments, ensure_ascii=False)}")
    if store is not None:
        for claim_id in store.related_from(ANNOUNCES, fact.grounded_in):
            announcing = store.get(claim_id)
            if announcing is not None and isinstance(announcing.element, Claim):
                lines.append(f"Reason given by the agent: {announcing.element.claim_text}")
    if not lines:
        return fact.fact_text
    return "\n".join([*lines, "Output:", fact.fact_text])


def render_prompt(fact_text: str, claim_text: str, subject: Subject = "claim") -> str:
    return f"=== FACT ===\n{fact_text}\n\n=== {subject.upper()} ===\n{claim_text}\n"


def parse_yes_no(text: str) -> bool | None:
    """True for YES, False for NO, None when the reply is neither.

    The reply should be the bare word; case, surrounding whitespace, quotes, markdown and a
    final period are ignored, and so is anything after the first word ("Yes, because ...").
    """
    match = re.match(r"[\s\W_]*(yes|no)\b", text, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).lower() == "yes"


class FactClaimLinker(SemanticRelationExtractor):
    """Infers whether a claim is derived from a fact, with one LLM request per pair.

    ``subject="text"`` compares the fact with a longer passage instead of a claim (is some
    statement of the passage derived from the fact?); the argument is then named ``claim_text``
    all the same.

    As a ``SemanticRelationExtractor``, ``extract(store, elements)`` judges every (fact, claim)
    pair of ``elements`` and records the ``supported_by`` relation (claim -> fact) in the store
    for the ones that are backed.
    """

    def __init__(self, llm: TextLLMClient, subject: Subject = "claim") -> None:
        super().__init__()
        self._llm = llm
        self._subject = subject
        self._system = TEXT_SYSTEM_PROMPT if subject == "text" else SYSTEM_PROMPT

    def extract(
        self, store: SymbolicSessionStore, elements: Sequence[SymbolicStateElement]
    ) -> list[FactClaimLink]:
        """One ``FactClaimLink`` per (fact, claim) pair of ``elements`` (every fact against
        every claim, one request each); other element types are ignored.

        The backed pairs are also recorded in ``store`` as ``supported_by`` (claim -> fact).
        ``last_usage`` is the total usage of the call. Raises ``ValueError`` when a pair gets
        no valid answer.
        """
        facts = [e for e in elements if isinstance(e, Fact)]
        claims = [e for e in elements if isinstance(e, Claim)]
        self.last_usage = LLMUsage()
        links: list[FactClaimLink] = []
        for fact in facts:
            for claim in claims:
                link, usage = self.link(fact, claim, store)
                self.last_usage = self.last_usage + usage
                links.append(link)
                if link.backed:
                    store.link(SUPPORTED_BY, claim.id, fact.id)
        return links

    def is_derived(self, fact_text: str, claim_text: str) -> tuple[bool, LLMUsage]:
        """Whether the claim is derived from the fact, retried when the reply is not YES/NO.
        Raises ``ValueError`` when no attempt gives a valid reply."""
        usage = LLMUsage()
        prompt = render_prompt(fact_text, claim_text, self._subject)
        problem = ""
        for attempt in range(1, ATTEMPTS + 1):
            reply = self._llm.complete_text(self._system, prompt)
            usage = usage + reply.usage
            answer = parse_yes_no(reply.text)
            if answer is not None:
                return answer, usage
            problem = f"expected YES or NO, got {reply.text[:100]!r}"
            logger.warning("Attempt %d/%d rejected: %s", attempt, ATTEMPTS, problem)
        raise ValueError(f"No valid answer from the LLM: {problem}")

    def link(
        self, fact: Fact, claim: Claim, store: SymbolicSessionStore | None = None
    ) -> tuple[FactClaimLink, LLMUsage]:
        """The link between an extracted fact and an extracted claim. With a ``store`` the fact is shown
        with the claim(s) that announced its tool call (see ``describe_fact``)."""
        backed, usage = self.is_derived(describe_fact(fact, store), claim.claim_text)
        return FactClaimLink(fact_id=fact.id, claim_id=claim.id, backed=backed), usage


# --- Batch: many (fact, claim) pairs in one request -------------------------------------

BATCH_SYSTEM_PROMPT = """\
You are given several numbered PAIRS. Each pair has a FACT (the output of a tool call) and a \
CLAIM made by an agent. For each pair, decide whether the claim is derived from the fact: the \
fact states it, or it follows from the fact with at most trivial reasoning (a rewording, a \
sum, a comparison, a count), and every part of the claim is covered.

Judge every pair on its own, using only the fact of that pair: the facts of the other pairs \
are not evidence for it. Do not use outside knowledge, and do not judge whether the claim is \
true in general. Answer NO when the fact says nothing about the claim, contradicts it, or does \
not decide it (it is about the same subject but needs information the fact does not contain).

""" + FACT_CONTEXT_NOTE + """
Reply with exactly one line per pair, in order, in the form "P1: YES" or "P2: NO", using the \
number of the pair. Nothing else: no explanation, no other text.
"""

BATCH_TEXT_SYSTEM_PROMPT = """\
You are given several numbered PAIRS. Each pair has a FACT (the output of a tool call) and a \
TEXT written by an agent. A text may contain several statements, most of them about other \
things than the fact. For each pair, decide whether the text contains a statement that is \
derived from the fact: the fact states it, or it follows from the fact with at most trivial \
reasoning (a rewording, a sum, a comparison, a count), and every part of that statement is \
covered.

Judge every pair on its own, using only the fact of that pair: the facts of the other pairs \
are not evidence for it. Do not use outside knowledge, and do not judge whether the text is \
true in general. Statements of the text that the fact does not address are ignored. Answer NO \
when no statement of the text is derived from the fact: the fact says nothing about them, \
contradicts them, or does not decide them (they are about the same subject but need \
information the fact does not contain).

""" + FACT_CONTEXT_NOTE + """
Reply with exactly one line per pair, in order, in the form "P1: YES" or "P2: NO", using the \
number of the pair. Nothing else: no explanation, no other text.
"""

#: ``P3: YES``, ``3. no``, ``**P3**: **YES**``... The label, then the answer, with only
#: punctuation or spaces between them.
_ANSWER_LINE = re.compile(r"(?<![\w.])P?(\d+)[\s\W_]*?\b(yes|no)\b", flags=re.IGNORECASE)


class FactClaimPair(BaseModel):
    """One (fact, claim) pair to check. ``id`` identifies it in the result."""

    id: str
    fact_text: str
    claim_text: str


class BatchResult(BaseModel):
    """Answers of a batch: ``answers[pair id]`` is True when the claim is derived from the fact."""

    answers: dict[str, bool] = Field(default_factory=dict)
    #: Ids of the pairs that got no valid answer after the retries.
    missing: list[str] = Field(default_factory=list)
    usage: LLMUsage = Field(default_factory=LLMUsage)


def render_batch_prompt(pairs: Sequence[FactClaimPair], subject: Subject = "claim") -> str:
    """The pairs, labelled P1, P2, ... (short labels are easier to copy back than real ids)."""
    return "\n".join(
        f"=== PAIR P{i} ===\nFACT:\n{p.fact_text}\n\n{subject.upper()}:\n{p.claim_text}\n"
        for i, p in enumerate(pairs, start=1)
    )


def parse_batch_answers(text: str, count: int) -> dict[int, bool]:
    """``{pair number (from 1): derived}`` for the pairs answered in ``text``.

    A label outside 1..``count`` is ignored. A pair answered twice with different answers is
    left out (it is not known which one is meant); the same answer twice is fine.
    """
    found: dict[int, set[bool]] = {}
    for label, word in _ANSWER_LINE.findall(text):
        number = int(label)
        if 1 <= number <= count:
            found.setdefault(number, set()).add(word.lower() == "yes")
    return {number: next(iter(answers)) for number, answers in found.items() if len(answers) == 1}


class BatchFactClaimLinker:
    """Infers whether claims are derived from facts, with one LLM request for a list of pairs.

    The pairs are labelled P1..Pn in the prompt and the model answers one line per pair
    (``P1: YES``), which is easier to keep stable than JSON. Pairs with no valid answer are
    asked again, alone, up to ``ATTEMPTS`` requests in total; whatever is still unanswered is
    reported in ``BatchResult.missing`` instead of raising, so the answered ones are kept.
    ``subject="text"`` compares the facts with longer passages instead of claims (see
    ``FactClaimLinker``).
    """

    def __init__(self, llm: TextLLMClient, subject: Subject = "claim") -> None:
        self._llm = llm
        self._subject = subject
        self._system = BATCH_TEXT_SYSTEM_PROMPT if subject == "text" else BATCH_SYSTEM_PROMPT

    def is_derived(self, pairs: Sequence[FactClaimPair]) -> BatchResult:
        """The answer of every pair, by pair id. Raises ``ValueError`` on duplicate ids."""
        ids = [p.id for p in pairs]
        if len(set(ids)) != len(ids):
            raise ValueError("pair ids must be unique")

        result = BatchResult()
        pending = list(pairs)
        for attempt in range(1, ATTEMPTS + 1):
            if not pending:
                break
            reply = self._llm.complete_text(self._system, render_batch_prompt(pending, self._subject))
            result.usage = result.usage + reply.usage
            answered = parse_batch_answers(reply.text, len(pending))
            for number, derived in answered.items():
                result.answers[pending[number - 1].id] = derived
            pending = [p for i, p in enumerate(pending, start=1) if i not in answered]
            if pending:
                logger.warning(
                    "Attempt %d/%d: %d of the pairs have no valid answer", attempt, ATTEMPTS, len(pending)
                )
        result.missing = [p.id for p in pending]
        result.answers = {i: result.answers[i] for i in ids if i in result.answers}  # input order
        return result

    def link(
        self, items: Sequence[tuple[Fact, Claim]], store: SymbolicSessionStore | None = None
    ) -> tuple[list[FactClaimLink], BatchResult]:
        """Links for the answered (fact, claim) pairs, in order, and the batch result. With a ``store``
        the facts are shown with the claim(s) that announced their tool call (see ``describe_fact``).

        The pair id used in the result is ``"<fact id>|<claim id>"``; a pair that got no valid
        answer has no link and is in ``BatchResult.missing``.
        """
        pairs = [
            FactClaimPair(
                id=f"{fact.id}|{claim.id}", fact_text=describe_fact(fact, store), claim_text=claim.claim_text
            )
            for fact, claim in items
        ]
        result = self.is_derived(pairs)
        links = [
            FactClaimLink(fact_id=fact.id, claim_id=claim.id, backed=result.answers[pair.id])
            for (fact, claim), pair in zip(items, pairs)
            if pair.id in result.answers
        ]
        return links, result
