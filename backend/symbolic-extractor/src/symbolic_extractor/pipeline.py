"""The extraction pipeline: which extractors and relation extractors run, in which order.

Shared by the CLI (``__main__``) and the queue worker, so both extract the same way.
"""

from __future__ import annotations

from symbolic_extractor.comparison import (
    DEFAULT_SAME_CLAIM_THRESHOLD,
    DEFAULT_SIMILARITY_THRESHOLD,
)
from symbolic_extractor.embeddings import TextEmbedder
from symbolic_extractor.extractor import SymbolicExtractor
from symbolic_extractor.extractors import (
    AgentSystemPromptExtractor,
    ClaimExtractionPrompt,
    DelegationClaimExtractor,
    ElementExtractor,
    GoalExtractionPrompt,
    SystemCapabilityExtractor,
    SystemConstraintExtractor,
    SystemTaskExtractor,
    TaskExtractionPrompt,
    ToolResultFactExtractor,
    UserConstraintPrompt,
)
from symbolic_extractor.llm import OpenAIChatClient
from symbolic_extractor.relations import (
    ArgumentFactLinker,
    ArgumentOriginExtractor,
    CapabilityTaskRelationExtractor,
    ClaimRelationExtractor,
    FactClaimLinker,
    GoalClaimRelationExtractor,
    TaskClaimRelationExtractor,
    TaskRelationExtractor,
)


def build_pipeline(
    llm: OpenAIChatClient,
    *,
    embedder: TextEmbedder | None = None,
    same_claim_threshold: float = DEFAULT_SAME_CLAIM_THRESHOLD,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    match_input_facts: bool = True,
) -> SymbolicExtractor:
    """A ``SymbolicExtractor`` with every extractor and relation extractor. Without an ``embedder``,
    ``same_claims`` is not computed."""
    extractors = [
        ElementExtractor(llm, ClaimExtractionPrompt()),
        ToolResultFactExtractor(),
        # The system prompt of each agent, read once from the input of its first LLM call (no LLM).
        AgentSystemPromptExtractor(),
        # The standing tasks of each agent: one LLM request on the system prompt just read above
        # (must come right after it); the pipeline links them to every agent call of the agent.
        SystemTaskExtractor(llm),
        # The constraints (rules) of each agent: its own LLM request on the same system prompt, once per
        # agent (must come after AgentSystemPromptExtractor); linked to the tasks of the agent's calls.
        SystemConstraintExtractor(llm),
        # The capabilities (what the agent is able to do) of each agent: again its own LLM request on the
        # same system prompt, once per agent (must come after AgentSystemPromptExtractor); linked to every
        # agent call of the agent.
        SystemCapabilityExtractor(llm),
        # One goal per session, read from the Conversation node (user request + final response).
        ElementExtractor(llm, GoalExtractionPrompt()),
        # The constraints the user states in that same request: a separate LLM request from the goal's;
        # linked to the goal.
        ElementExtractor(llm, UserConstraintPrompt()),
        # One task per assignment, read from the input of each AgentCall (markers and routing
        # turns are skipped without an LLM request).
        ElementExtractor(llm, TaskExtractionPrompt()),
        # The claims of the brief an agent call received from the agent that delegated to it
        # (needs the trajectory to find the caller; calls with no brief make no LLM request).
        DelegationClaimExtractor(llm),
    ]
    return SymbolicExtractor(
        extractors,
        embedder=embedder,
        same_claim_threshold=same_claim_threshold,
        similarity_threshold=similarity_threshold,
        match_input_facts=match_input_facts,
        # Always on, in both the per-session and the live mode: supported_by (claim -> fact),
        # conflicts_with / same_as / derived_from (claim -> input claims), implements (task ->
        # task of an earlier agent call) and achieves (task -> goal). GoalClaimRelationExtractor
        # judges, once the claims of the final answer are extracted, whether they satisfy the goal:
        # fulfils (claim -> goal). TaskClaimRelationExtractor judges, for each task, which of the
        # claims produced by it (produced_by, deterministic) fulfil it: fulfils (claim -> task).
        relation_extractors=[
            FactClaimLinker(llm),
            ClaimRelationExtractor(llm),
            TaskRelationExtractor(llm),
            GoalClaimRelationExtractor(llm),
            TaskClaimRelationExtractor(llm),
            # supports (capability -> task): which capabilities of its agent support each task and system task,
            # judged once per task as soon as the capabilities of its agent are known.
            CapabilityTaskRelationExtractor(llm),
            # supported_by (fact -> claim): which claims its agent had made so far the arguments of each tool
            # call come from, judged once per tool call that has arguments.
            ArgumentOriginExtractor(llm),
            # supported_by (fact -> fact): which earlier tool results of its agent call each tool result was
            # obtained with (a string argument found in them); text matching and timestamps, no LLM.
            ArgumentFactLinker(),
        ],
    )
