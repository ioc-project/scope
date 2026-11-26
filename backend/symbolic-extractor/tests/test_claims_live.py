"""Live tests: call the real LLM configured in ``.env``. Run with ``pytest --live``."""

from __future__ import annotations

import pytest

from symbolic_extractor.extractors import ClaimExtractionPrompt, ElementExtractor
from symbolic_extractor.llm import OpenAIChatClient
from symbolic_extractor.models import KGNode

pytestmark = pytest.mark.live

LLM_NODE = KGNode(
    node_type="LLMCall",
    node={
        "id": "live-llm-1",
        "modelName": "test-model",
        "prompt": "Find me a hotel in Lisbon for the weekend and tell me what you will do.",
        "completion": (
            "Hotel Alfama has availability from Friday to Sunday for EUR 280 in total. "
            "I think it is the best option for a short stay. "
            "I'll book it now. "
            "You should visit Lisbon in October."
        ),
    },
)


@pytest.fixture(scope="module")
def llm() -> OpenAIChatClient:
    client = OpenAIChatClient.from_env()
    if not client._client.api_key:
        pytest.skip("no API key found (OPENAI_API_KEY / LLM_API_KEY) in the environment or .env")
    return client


def test_claim_extraction_with_real_llm(llm: OpenAIChatClient) -> None:
    claims = ElementExtractor(llm, ClaimExtractionPrompt()).extract(LLM_NODE)

    # Output is non-deterministic, so only check the contract, not the exact wording.
    assert claims, "the real LLM returned no valid claim for an output full of claims"
    assert all(c.generated_by == "live-llm-1" for c in claims)
    assert all(c.claim_text.strip() for c in claims)
    assert all(c.claim_kind is None for c in claims if c.claim_force == "committing")
    assert all(c.claim_kind is not None for c in claims if c.claim_force == "assertive")

    # The text clearly contains an assertive claim and a committing one.
    forces = {c.claim_force for c in claims}
    assert "assertive" in forces
    assert "committing" in forces
