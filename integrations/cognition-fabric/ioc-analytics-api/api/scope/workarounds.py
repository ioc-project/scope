"""Known deviations from the MAS ontology and their workarounds.

Conventions
-----------
This codebase uses two distinct workaround categories and keeps them at
different architectural boundaries:

1. Query workarounds
    These compensate for KG shapes that do not yet follow the ontology.
    They live in oxp-api query builders and are executed only by the
    provider boundary through ontology/workaround query pairs.

2. Metric workarounds
    These compensate for legacy field names or anomalous payloads after the KG
    nodes have already been fetched. They live in MCE inside dedicated
    ``*_workaround.py`` modules and must operate only on the fetched context.
    Metric workarounds must not issue their own Neo4j queries.

Catalogue
---------

WK-01  Missing structural relationships
       Ontology : Session -[:hasMASCalls]-> MASCall -[:hasAgentCalls]->
                  AgentCall -[:invokesLLM]-> LLMCall
                  AgentCall -[:invokesTool]-> ToolCall
       Reality  : AgentCall / LLMCall / ToolCall nodes carry a flat
                  ``{sessionId}`` property but are NOT connected through
                  the Session → MASCall → AgentCall hierarchy.
       Affects  : fetch_session_query, list_agents_query, list_llm_calls_query
       Fix      : Update ``ingest_multi_layer_trajectory.py`` to create MASCall
                  nodes and the hasMASCalls / hasAgentCalls / invokesLLM /
                  invokesTool relationships.

WK-02  Session node missing timing properties
       Ontology : Session node should carry ``startTime`` and ``duration``.
       Reality  : Before 2026-03-19 the Session node was created without
            timing info. Equivalent timing signals still exist through
            ``hasInitialState`` / ``hasFinalState`` state timestamps and
            Transition timestamps + durations.
       Affects  : fetch_session_timing_query
       Fix      : Ingestion step 4b (added 2026-03-19) now back-fills
                  ``Session.startTime`` and ``Session.duration``.  WK-02
            fallback remains active for sessions ingested before that date.

WK-03  ToolCall.inputParams instead of canonical toolArguments
       Ontology : ToolCall nodes should use ``toolArguments`` (camelCase).
       Reality  : Older nodes store the same data in ``inputParams``.
    Affects  : ToolUtilizationAccuracy metric workaround in MCE.
    Fix      : Correct norm / ingestion so raw KG nodes follow the ontology.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class KGWorkaroundsConfig:
    """Configuration for known deviations from the MAS ontology.

    Parameters
    ----------
    workarounds_first:
        *False* (default — **ontology-first**): the provider runs the
        ontology-compliant query first, falling back to the workaround query
        only when the ontology query returns empty results.  This is the
        semantically correct behaviour and will automatically become the
        dominant path once the ingestion pipeline is fully migrated.

        *True* (**workaround-first**): the provider skips the ontology probe
        and runs the workaround query immediately.  Use this setting when the
        graph is known to not yet follow the ontology (current production data
        as of 2026-03-19).  Controlled by the env var
        ``MCE_WORKAROUNDS_FIRST=1``.

    This flag governs query workarounds only. Metric-local workarounds remain
    inside MCE and do not open new KG queries.
    """

    workarounds_first: bool = False

    @classmethod
    def from_env(cls) -> "KGWorkaroundsConfig":
        """Build config from the ``MCE_WORKAROUNDS_FIRST`` environment variable."""
        wf = os.getenv("MCE_WORKAROUNDS_FIRST", "").strip().lower()
        return cls(workarounds_first=wf in ("1", "true", "yes"))
