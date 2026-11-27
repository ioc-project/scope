import json

import pytest

from symbolic_extractor import SessionData, SymbolicExtractor
from symbolic_extractor.extractors import ClaimExtractionPrompt, ElementExtractor
from symbolic_extractor.extractors.claims import is_verbatim, readable_output, recover_verbatim
from symbolic_extractor.llm import LLMReply, parse_json_object
from symbolic_extractor.models import KGNode
from symbolic_extractor.usage import LLMUsage, TokenPricing


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return LLMReply(
            data=self.reply,
            usage=LLMUsage(calls=1, prompt_tokens=100, completion_tokens=20, cost_usd=0.001),
        )


LLM_NODE = KGNode(
    node_type="LLMCall",
    node={
        "id": "l1",
        "completion": "Hotel Alfama is available. I'll book it.",
        "prompt": "Find a hotel",
    },
)

REPLY = {
    "claims": [
        {
            "claim_text": "Hotel Alfama is available.",
            "claim_force": "assertive",
            "claim_kind": "factual",
        },
        {
            "claim_text": "The agent will book Hotel Alfama.",
            "claim_force": "committing",
            "claim_kind": "factual",
        },
    ]
}


def test_claims_are_extracted_and_anchored_to_the_llm_call():
    extractor = ElementExtractor(FakeLLM(REPLY), ClaimExtractionPrompt())
    claims = extractor.extract(LLM_NODE)

    assert [c.claim_force for c in claims] == ["assertive", "committing"]
    assert all(c.generated_by == "l1" for c in claims)
    # mas:ClaimKindShape: committing claims carry no kind
    assert claims[1].claim_kind is None


def test_claim_ids_are_deterministic():
    extractor = ElementExtractor(FakeLLM(REPLY), ClaimExtractionPrompt())
    assert [c.id for c in extractor.extract(LLM_NODE)] == [
        c.id for c in extractor.extract(LLM_NODE)
    ]


def test_invalid_items_are_dropped():
    reply = {
        "claims": [
            {"claim_text": "ok", "claim_force": "assertive", "claim_kind": "factual"},
            {"claim_text": "assertive without kind", "claim_force": "assertive"},
            {"claim_text": "bad force", "claim_force": "wonder"},
        ]
    }
    claims = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(LLM_NODE)
    assert [c.claim_text for c in claims] == ["ok"]


def test_verbatim_source_is_kept_only_when_it_is_in_the_output():
    reply = {
        "claims": [
            {
                "claim_text": "Hotel Alfama is available.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "verbatim_source": "Hotel Alfama is available.",
            },
            {
                "claim_text": "The agent will book it.",
                "claim_force": "committing",
                "verbatim_source": "I will reserve it for you.",  # paraphrase, not in output
            },
            {"claim_text": "No source", "claim_force": "directive"},
        ]
    }
    claims = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(LLM_NODE)

    assert [c.verbatim_source for c in claims] == ["Hotel Alfama is available.", None, None]
    assert len(claims) == 3  # an unverifiable snippet does not drop the claim


def test_verbatim_source_matches_decoded_text_of_otel_messages():
    completion = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "text",
                        "content": 'The "Express Train" takes **5 hours**.\nNo transfers.',
                    }
                ],
            }
        ]
    )
    node = KGNode(node_type="LLMCall", node={"id": "l3", "completion": completion})
    reply = {
        "claims": [
            {
                "claim_text": "The Express Train takes 5 hours.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                # unescaped quotes: only present in the decoded text, not in the raw JSON string
                "verbatim_source": 'The "Express Train" takes **5 hours**.',
            }
        ]
    }
    (claim,) = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(node)
    assert claim.verbatim_source == 'The "Express Train" takes **5 hours**.'


def _otel(text: str) -> str:
    return json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": text}]}])


ITINERARY = "### Itinerary\nDay 1: Lisbon, total $300.\nOverall total: $1,055-1,625."
ENVELOPE = _otel(json.dumps({"next": "FINISH", "final_answer": ITINERARY}))


def test_readable_output_decodes_messages_and_unwraps_the_final_answer():
    assert readable_output("plain text") == "plain text"
    assert readable_output(_otel("Hello\nthere")) == "Hello\nthere"
    assert readable_output(ENVELOPE) == ITINERARY
    assert readable_output(json.dumps({"next": "FINISH", "final_answer": ITINERARY})) == ITINERARY
    # a routing command without an answer stays as it is
    command = json.dumps({"next": "hotel_agent"})
    assert readable_output(_otel(command)) == command


def test_prompt_sends_the_unwrapped_final_answer_not_the_json_envelope():
    node = KGNode(node_type="LLMCall", node={"id": "l4", "completion": ENVELOPE, "prompt": "plan"})
    prompt = ClaimExtractionPrompt().render(node)

    assert "Overall total: $1,055-1,625." in prompt
    assert '"next"' not in prompt
    assert "\\n" not in prompt


def test_verbatim_source_taken_from_the_unwrapped_final_answer_is_kept():
    node = KGNode(node_type="LLMCall", node={"id": "l5", "completion": ENVELOPE})
    reply = {
        "claims": [
            {
                "claim_text": "The overall total is $1,055-1,625.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "verbatim_source": "Overall total: $1,055-1,625.",
            }
        ]
    }
    (claim,) = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(node)
    assert claim.verbatim_source == "Overall total: $1,055-1,625."


def test_is_verbatim_ignores_whitespace_runs_but_not_wording():
    assert is_verbatim("a  b", "x a\nb y")
    assert not is_verbatim("a c", "x a b y")
    assert not is_verbatim("  ", "anything")


MARKDOWN_ANSWER = (
    "#### **Day 1-2: Celestia**\n**Attractions:**\n1. **Crystal Tower Observatory**: Panoramic views ($25/person).\n"
    "2. **Starlight Gardens**: Bioluminescent gardens (Free).\n\n**Costs:**\n- Total for 2 days: $145-205/person."
)


def test_recover_verbatim_returns_the_original_text_with_its_formatting():
    cleaned = "Attractions: 1. Crystal Tower Observatory: Panoramic views ($25/person)."
    excerpt = recover_verbatim(cleaned, MARKDOWN_ANSWER)

    assert (
        excerpt == "Attractions:**\n1. **Crystal Tower Observatory**: Panoramic views ($25/person"
    )
    assert is_verbatim(excerpt, MARKDOWN_ANSWER)
    # case and punctuation do not matter, wording does
    assert recover_verbatim("total for 2 days $145-205 per person", MARKDOWN_ANSWER) is None
    assert (
        recover_verbatim("TOTAL FOR 2 DAYS: $145-205/PERSON", MARKDOWN_ANSWER)
        == "Total for 2 days: $145-205/person"
    )


def test_recover_verbatim_keeps_an_exact_snippet_and_refuses_short_or_unknown_ones():
    assert recover_verbatim(" Day 1-2: Celestia ", MARKDOWN_ANSWER) == "Day 1-2: Celestia"
    assert (
        recover_verbatim("Costs", MARKDOWN_ANSWER) == "Costs"
    )  # exact, so kept whatever its length
    assert (
        recover_verbatim("**Cost** s", "Costs: a lot") is None
    )  # too short to recover by alignment
    assert recover_verbatim("Visit the Eiffel Tower in spring", MARKDOWN_ANSWER) is None


def test_verbatim_source_cleaned_of_markdown_by_the_llm_is_recovered():
    node = KGNode(node_type="LLMCall", node={"id": "l6", "completion": _otel(MARKDOWN_ANSWER)})
    reply = {
        "claims": [
            {
                "claim_text": "The crystal tower observatory costs $25 per person.",
                "claim_force": "assertive",
                "claim_kind": "factual",
                "verbatim_source": "1. Crystal Tower Observatory: Panoramic views ($25/person).",
            }
        ]
    }
    (claim,) = ElementExtractor(FakeLLM(reply), ClaimExtractionPrompt()).extract(node)
    assert claim.verbatim_source == "1. **Crystal Tower Observatory**: Panoramic views ($25/person"


def test_node_without_output_skips_llm_call():
    llm = FakeLLM(REPLY)
    node = KGNode(node_type="LLMCall", node={"id": "l2", "completion": "  "})
    assert ElementExtractor(llm, ClaimExtractionPrompt()).extract(node) == []
    assert llm.calls == []


def test_claims_only_apply_to_llm_calls():
    extractor = ElementExtractor(FakeLLM(REPLY), ClaimExtractionPrompt())
    assert extractor.applies_to(LLM_NODE)
    assert not extractor.applies_to(KGNode(node_type="ToolCall", node={"id": "t1"}))


def test_pipeline_sorts_by_time_and_keeps_original_node():
    payload = {
        "session_id": "s1",
        "session": {"sessionId": "s1"},
        "agent_spans": [{"id": "a1", "startTime": "2026-01-01T10:00:00"}],
        "llm_spans": [
            {"id": "l-late", "startTime": "2026-01-01T10:00:02", "completion": "X is true."},
            {"id": "l-early", "startTime": "2026-01-01T10:00:01", "completion": "Y is true."},
        ],
        "tool_spans": [{"id": "t-notime"}],
    }
    llm = FakeLLM(
        {"claims": [{"claim_text": "c", "claim_force": "assertive", "claim_kind": "factual"}]}
    )
    pipeline = SymbolicExtractor([ElementExtractor(llm, ClaimExtractionPrompt())])

    result = pipeline.process(SessionData.from_payload(payload))

    assert [n.node["id"] for n in result.nodes] == ["a1", "l-early", "l-late", "t-notime"]
    by_id = {n.node["id"]: n for n in result.nodes}
    assert by_id["l-early"].node["completion"] == "Y is true."  # original node preserved
    assert len(by_id["l-early"].symbolic_state_elements["claims"]) == 1
    assert by_id["a1"].symbolic_state_elements == {}  # extractor does not apply to AgentCall
    assert by_id["t-notime"].symbolic_state_elements == {}

    # usage: 2 LLM calls, attributed per node and summed in the result
    assert by_id["l-early"].llm_usage["claims"].total_tokens == 120
    assert by_id["a1"].llm_usage == {}
    assert result.llm_usage.calls == 2
    assert result.llm_usage.total_tokens == 240
    assert result.llm_usage.cost_usd == pytest.approx(0.002)
    assert result.llm_usage_by_element["claims"] == result.llm_usage


def test_skipped_node_has_no_usage():
    extractor = ElementExtractor(FakeLLM(REPLY), ClaimExtractionPrompt())
    extractor.extract(LLM_NODE)
    extractor.extract(KGNode(node_type="LLMCall", node={"id": "l2", "completion": " "}))
    assert extractor.last_usage.calls == 0
    assert extractor.usage.calls == 1  # cumulative usage is unchanged by the skipped node


def test_usage_sum_skips_unknown_costs():
    known = LLMUsage(calls=1, prompt_tokens=1, completion_tokens=1, cost_usd=0.5)
    unknown = LLMUsage(calls=1, prompt_tokens=2, completion_tokens=2)
    assert (known + unknown).cost_usd == 0.5
    assert (unknown + unknown).cost_usd is None


def test_token_pricing_is_per_million_tokens():
    assert TokenPricing(input_per_1m=2.0, output_per_1m=10.0).cost(
        500_000, 100_000
    ) == pytest.approx(2.0)


def test_nodes_are_sorted_numerically_by_epoch_start_time():
    # The KG stores startTime as a float epoch. "999.5" > "1000.5" as strings, not as numbers.
    data = SessionData(
        session_id="s1",
        llm_spans=[
            {"id": "b", "startTime": 1000.5},
            {"id": "a", "startTime": 999.5},
            {"id": "none"},
        ],
    )
    assert [n.node["id"] for n in data.nodes()] == ["a", "b", "none"]


def test_parse_json_object_handles_code_fence():
    assert parse_json_object('```json\n{"claims": []}\n```') == {"claims": []}
    with pytest.raises(ValueError):
        parse_json_object("[1]")


def test_parse_json_object_recovers_from_stray_prefix():
    # Reply shape seen from a model behind the LiteLLM proxy: a stray `{"` before the object.
    reply = '{"{\n  "claims": [\n    {"claim_text": "x", "claim_force": "assertive"}\n  ]\n}'
    assert parse_json_object(reply) == {"claims": [{"claim_text": "x", "claim_force": "assertive"}]}
    assert parse_json_object('Here you go: {"claims": []} Hope it helps') == {"claims": []}


def test_parse_json_object_without_any_object_raises():
    with pytest.raises(ValueError):
        parse_json_object("no json here")
