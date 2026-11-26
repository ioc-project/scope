import pytest

from symbolic_extractor.extractors import Claim, Fact
from symbolic_extractor.llm import TextReply
from symbolic_extractor.relations.fact_claim_links import (
    BATCH_TEXT_SYSTEM_PROMPT,
    SUPPORTED_BY,
    SYSTEM_PROMPT,
    TEXT_SYSTEM_PROMPT,
    BatchFactClaimLinker,
    FactClaimLinker,
    FactClaimPair,
    parse_batch_answers,
    parse_yes_no,
    render_batch_prompt,
    render_prompt,
)
from symbolic_extractor.relations.semantic_relations import SemanticRelationExtractor
from symbolic_extractor.store import SymbolicSessionStore
from symbolic_extractor.usage import LLMUsage


class SequenceLLM:
    """Returns the given replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_text(self, system, user):
        self.calls.append((system, user))
        return TextReply(
            text=self.replies[len(self.calls) - 1],
            usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=2, cost_usd=0.001),
        )


FACT = "Celestia to Luminos (Direct): 425 km, 5 hours"
CLAIM = "The direct train from Celestia to Luminos takes 5 hours."


@pytest.mark.parametrize(
    "text, expected",
    [
        ("YES", True),
        ("NO", False),
        ("yes", True),
        (" No \n", False),
        ("Yes.", True),
        ('"NO"', False),
        ("**YES**", True),
        ("Yes, the fact states it.", True),
        ("No - the fact does not say.", False),
        ("", None),
        ("maybe", None),
        ("Not sure", None),
        ("Yesterday", None),
        ("Nothing", None),
    ],
)
def test_parse_yes_no(text, expected):
    assert parse_yes_no(text) is expected


def test_answer_is_a_yes_no():
    assert FactClaimLinker(SequenceLLM("YES")).is_derived(FACT, CLAIM)[0] is True
    assert FactClaimLinker(SequenceLLM("NO")).is_derived(FACT, CLAIM)[0] is False


def test_prompt_holds_the_fact_and_the_claim():
    llm = SequenceLLM("YES")
    FactClaimLinker(llm).is_derived(FACT, CLAIM)

    _, user = llm.calls[0]
    assert user == render_prompt(FACT, CLAIM)
    assert FACT in user and CLAIM in user


def test_text_subject_uses_the_text_prompts_and_labels():
    llm = SequenceLLM("YES")
    FactClaimLinker(llm, subject="text").is_derived(FACT, CLAIM)
    system, user = llm.calls[0]
    assert system == TEXT_SYSTEM_PROMPT and system != SYSTEM_PROMPT
    assert user == render_prompt(FACT, CLAIM, "text") and "=== TEXT ===" in user and "=== CLAIM ===" not in user

    batch = SequenceLLM("P1: YES")
    pair = FactClaimPair(id="a", fact_text=FACT, claim_text=CLAIM)
    BatchFactClaimLinker(batch, subject="text").is_derived([pair])
    system, user = batch.calls[0]
    assert system == BATCH_TEXT_SYSTEM_PROMPT
    assert user == render_batch_prompt([pair], "text") and "\nTEXT:\n" in user


def test_claim_is_the_default_subject():
    llm = SequenceLLM("YES")
    FactClaimLinker(llm).is_derived(FACT, CLAIM)
    assert llm.calls[0][0] == SYSTEM_PROMPT and "=== CLAIM ===" in llm.calls[0][1]


def test_invalid_reply_is_retried_and_usage_is_summed():
    llm = SequenceLLM("maybe", "NO")
    derived, usage = FactClaimLinker(llm).is_derived(FACT, CLAIM)

    assert derived is False
    assert len(llm.calls) == 2
    assert usage.calls == 2 and usage.prompt_tokens == 200
    assert usage.cost_usd == pytest.approx(0.002)


def test_gives_up_after_the_attempts():
    llm = SequenceLLM("maybe", "")
    with pytest.raises(ValueError, match="No valid answer"):
        FactClaimLinker(llm).is_derived(FACT, CLAIM)
    assert len(llm.calls) == 2


def test_link_carries_the_ids_of_the_fact_and_the_claim():
    fact = Fact(id="f1", fact_text=FACT, verbatim_source=FACT, grounded_in="t1")
    claim = Claim(id="c1", claim_text=CLAIM, claim_force="assertive", claim_kind="factual")

    link, usage = FactClaimLinker(SequenceLLM("YES")).link(fact, claim)

    assert (link.fact_id, link.claim_id, link.backed) == ("f1", "c1", True)
    assert usage.calls == 1


def test_extract_judges_every_fact_claim_pair_and_records_the_backed_ones_in_the_store():
    fact = Fact(id="f1", fact_text=FACT, verbatim_source=FACT, grounded_in="t1")
    c1 = Claim(id="c1", claim_text=CLAIM, claim_force="assertive", claim_kind="factual")
    c2 = Claim(id="c2", claim_text="It is raining.", claim_force="assertive", claim_kind="factual")
    store = SymbolicSessionStore("s1")
    llm = SequenceLLM("YES", "NO")
    linker = FactClaimLinker(llm)

    assert isinstance(linker, SemanticRelationExtractor)
    links = linker.extract(store, [c1, fact, c2])

    assert [(l.fact_id, l.claim_id, l.backed) for l in links] == [("f1", "c1", True), ("f1", "c2", False)]
    assert store.related(SUPPORTED_BY, "c1") == ["f1"]
    assert store.related(SUPPORTED_BY, "c2") == []
    assert linker.last_usage.calls == 2 and linker.last_usage.cost_usd == pytest.approx(0.002)


def test_extract_without_a_fact_or_a_claim_does_nothing():
    claim = Claim(id="c1", claim_text=CLAIM, claim_force="assertive", claim_kind="factual")
    llm = SequenceLLM()
    linker = FactClaimLinker(llm)
    assert linker.extract(SymbolicSessionStore("s1"), [claim]) == []
    assert linker.extract(SymbolicSessionStore("s1"), []) == []
    assert llm.calls == [] and linker.last_usage.calls == 0


def test_the_base_extractor_finds_no_relation():
    claim = Claim(id="c1", claim_text=CLAIM, claim_force="assertive", claim_kind="factual")
    store = SymbolicSessionStore("s1")
    assert SemanticRelationExtractor().extract(store, [claim]) == []
    assert store.related(SUPPORTED_BY, "c1") == []


# --- Batch ---------------------------------------------------------------------------------

PAIRS = [
    FactClaimPair(id="id-a", fact_text="Fare: $75", claim_text="The fare is $75."),
    FactClaimPair(id="id-b", fact_text="Fare: $75", claim_text="The fare is $95."),
    FactClaimPair(id="id-c", fact_text="Train: 5 hours", claim_text="The train takes 5 hours."),
]


@pytest.mark.parametrize(
    "text, count, expected",
    [
        ("P1: YES\nP2: NO\nP3: YES", 3, {1: True, 2: False, 3: True}),
        ("p1: yes\np2: no", 2, {1: True, 2: False}),
        ("1: YES\n2. NO", 2, {1: True, 2: False}),
        ("**P1**: **YES**\n**P2**: **NO**", 2, {1: True, 2: False}),
        ("P1: YES, P2: NO, P3: YES", 3, {1: True, 2: False, 3: True}),
        ("P3: NO\nP1: YES", 3, {1: True, 3: False}),  # any order, partial
        ("P1: YES\nP1: YES", 1, {1: True}),  # the same answer twice
        ("P1: YES\nP1: NO", 1, {}),  # contradictory: not known
        ("P1: YES\nP7: NO\nP0: NO", 1, {1: True}),  # labels out of range
        ("P1: Not sure\nP2: maybe", 2, {}),
        ("P10: NO\nP1: YES", 10, {1: True, 10: False}),
        ("", 3, {}),
    ],
)
def test_parse_batch_answers(text, count, expected):
    assert parse_batch_answers(text, count) == expected


def test_batch_prompt_labels_the_pairs_and_holds_every_fact_and_claim():
    llm = SequenceLLM("P1: YES\nP2: NO\nP3: YES")
    BatchFactClaimLinker(llm).is_derived(PAIRS)

    system, user = llm.calls[0]
    assert user == render_batch_prompt(PAIRS)
    for label, pair in zip(("P1", "P2", "P3"), PAIRS):
        assert f"=== PAIR {label} ===" in user
        assert pair.claim_text in user
    assert "id-a" not in user  # the real ids are not sent, only the short labels
    assert "one line per pair" in system


def test_batch_answers_come_back_by_pair_id_in_input_order():
    llm = SequenceLLM("P3: YES\nP1: YES\nP2: NO")  # the model answers out of order
    result = BatchFactClaimLinker(llm).is_derived(PAIRS)

    assert list(result.answers.items()) == [("id-a", True), ("id-b", False), ("id-c", True)]
    assert result.missing == []
    assert len(llm.calls) == 1  # one request for the three pairs
    assert result.usage.calls == 1 and result.usage.prompt_tokens == 100


def test_batch_asks_again_only_for_the_unanswered_pairs():
    llm = SequenceLLM("P1: YES\nP3: NO", "P1: NO")  # P2 was skipped
    result = BatchFactClaimLinker(llm).is_derived(PAIRS)

    assert result.answers == {"id-a": True, "id-b": False, "id-c": False}
    assert result.missing == []
    second = llm.calls[1][1]  # the retry holds the unanswered pair only, relabelled P1
    assert "=== PAIR P1 ===" in second and PAIRS[1].claim_text in second
    assert "P2" not in second and PAIRS[0].claim_text not in second
    assert result.usage.calls == 2 and result.usage.cost_usd == pytest.approx(0.002)


def test_batch_reports_the_pairs_that_stay_unanswered_without_raising():
    llm = SequenceLLM("P1: YES\nP2: maybe", "I cannot tell")
    result = BatchFactClaimLinker(llm).is_derived(PAIRS)

    assert result.answers == {"id-a": True}
    assert result.missing == ["id-b", "id-c"]
    assert len(llm.calls) == 2  # ATTEMPTS requests at most


def test_batch_rejects_duplicate_ids_and_skips_the_request_for_no_pairs():
    llm = SequenceLLM()
    with pytest.raises(ValueError, match="unique"):
        BatchFactClaimLinker(llm).is_derived([PAIRS[0], PAIRS[0]])

    result = BatchFactClaimLinker(llm).is_derived([])
    assert result.answers == {} and result.missing == [] and llm.calls == []


def test_batch_link_returns_the_links_of_the_answered_pairs():
    fact = Fact(id="f1", fact_text="Fare: $75", verbatim_source="Fare: $75", grounded_in="t1")
    c1 = Claim(id="c1", claim_text="The fare is $75.", claim_force="assertive", claim_kind="factual")
    c2 = Claim(id="c2", claim_text="The fare is $95.", claim_force="assertive", claim_kind="factual")
    c3 = Claim(id="c3", claim_text="Fares rise in July.", claim_force="assertive", claim_kind="factual")
    llm = SequenceLLM("P1: YES\nP2: NO", "nothing useful")  # P3 never gets an answer

    links, result = BatchFactClaimLinker(llm).link([(fact, c1), (fact, c2), (fact, c3)])

    assert [(l.fact_id, l.claim_id, l.backed) for l in links] == [("f1", "c1", True), ("f1", "c2", False)]
    assert result.missing == ["f1|c3"]
