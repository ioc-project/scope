"""Reads session data from the Knowledge Graph via the SCOPE API library."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from dotenv import find_dotenv, load_dotenv

from symbolic_extractor.models import SessionData, SessionTrajectory

logger = logging.getLogger(__name__)


class SessionNotFoundError(LookupError):
    """Raised when the KG holds no data for the requested session."""


#: Handoffs between the AgentCalls of a session, as the normalisation (``norm``) wrote them. When an
#: agent span names its predecessor (``ioa_observe.handoff.source.span_ids``) and the predecessor's
#: final State differs from the successor's initial State, ``norm`` adds a ``ProcessingCall`` (name
#: "handoff bridge (...)", ``Processing`` named ``handoff``) between the two States; with no such
#: attribute it chains sibling calls by start time (``handoff_fallback``). When the two States are
#: identical they are merged into one, so the handoff is a State shared by the two calls. There is
#: no AgentCall -> AgentCall edge: the link is read through the States. The bridges hang from the
#: MASCall, so the SCOPE API's session query (``hasAgentCall -> hasProcessingCall``) does not see them.
HANDOFFS_QUERY = """
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(:State)<-[:hasInitialState]-(p:ProcessingCall)
MATCH (p)-[:executesProcessing]->(pr:Processing)
MATCH (p)-[:hasFinalState]->(:State)<-[:hasInitialState]-(b:AgentCall)
WHERE a <> b AND pr.name STARTS WITH 'handoff'
RETURN a.id AS source, b.id AS target, pr.name AS kind
UNION
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(:State)<-[:hasInitialState]-(b:AgentCall)
WHERE a <> b
RETURN a.id AS source, b.id AS target, 'shared_state' AS kind
"""


def fetch_handoffs(db: Any, session_id: str) -> list[dict[str, Any]]:
    """The handoffs between the AgentCalls of ``session_id`` in the KG (``HANDOFFS_QUERY``), as
    ``{"from_call", "to_call", "kind"}``, each pair once (the first kind found), sorted for a stable
    output. ``db`` is a SCOPE connector (``execute(query, params)``)."""
    rows = db.execute(HANDOFFS_QUERY, {"session_id": session_id}) or []
    found: dict[tuple[str, str], str] = {}
    for row in sorted(rows, key=lambda r: (r["source"], r["target"], r["kind"])):
        found.setdefault((row["source"], row["target"]), row["kind"])
    return [{"from_call": a, "to_call": b, "kind": kind} for (a, b), kind in found.items()]


#: The AgentCall that produced the final answer of a session. The Session's ``hasFinalState`` State
#: is the very same node as the ``hasFinalState`` of one AgentCall (and of a ProcessingCall and the
#: MASCall), so the AgentCall is found through that shared State: no timestamp or text matching.
#: Verified on every saved session: exactly one AgentCall shares it. Timestamps would give the wrong
#: call (a call can start or end after the one that delivers the answer, e.g. a resume turn).
FINAL_AGENT_CALL_QUERY = """
MATCH (:Session {sessionId: $session_id})-[:hasFinalState]->(fs:State)
MATCH (a:AgentCall {sessionId: $session_id})-[:hasFinalState]->(fs)
RETURN DISTINCT a.id AS id
"""


def fetch_final_agent_call(db: Any, session_id: str) -> str | None:
    """The id of the AgentCall whose final State is the final State of ``session_id``
    (``FINAL_AGENT_CALL_QUERY``), None when the KG has none (the session has no final State yet, or
    no AgentCall shares it). With several (not seen in practice) the first by id is returned, for a
    stable output. ``db`` is a SCOPE connector (``execute(query, params)``)."""
    rows = db.execute(FINAL_AGENT_CALL_QUERY, {"session_id": session_id}) or []
    ids = sorted({row["id"] for row in rows if row.get("id")})
    return ids[0] if ids else None


#: The MAS (multi-agent system) a session belongs to: ``(:MASCall)-[:executesMAS]-(:MAS)``. The edge
#: is matched in either direction, only the label and the name matter.
# Note that this is used only for function tests logging, not in the extraction itself.
MAS_NAME_QUERY = """
MATCH (m:MAS)-[:executesMAS]-(:MASCall {sessionId: $session_id})
RETURN DISTINCT m.name AS name
"""


def fetch_mas_name(db: Any, session_id: str) -> str | None:
    """The name of the MAS that ran ``session_id`` (``MAS_NAME_QUERY``), None when the KG has none.
    ``db`` is a SCOPE connector (``execute(query, params)``)."""
    rows = db.execute(MAS_NAME_QUERY, {"session_id": session_id}) or []
    names = sorted({row["name"] for row in rows if row.get("name")})
    return names[0] if names else None


class SessionProvider(Protocol):
    """Subset of ``scope.providers.kg.SCOPEKGProvider`` used by the reader.

    TODO: ``LocalClient`` (scope.client.local) is the preferred entry point to the
    SCOPE API, but it has no method returning the full session payload (Session
    node, Agent/LLM/Tool calls with names and input/output content, and the
    session conversation). ``LocalClient.get_state_by_session_id`` only returns
    the ``State`` node. For now we use ``SCOPEKGProvider.fetch`` directly.
    Once we know exactly which data the extraction needs, revise this and either
    move to existing ``LocalClient`` methods or extend ``LocalClient`` (e.g. in
    ``scope/client/_kg.py``) with a dedicated method, then drop this dependency.
    """

    def fetch(self, resource_id: str, requirements: Any) -> dict[str, Any]: ...


class KGSessionReader:
    """Retrieves everything the KG knows about one session."""

    def __init__(
        self,
        provider: SessionProvider,
        trajectory_fetcher: Callable[[str], dict[str, Any]] | None = None,
        handoff_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
        final_call_fetcher: Callable[[str], str | None] | None = None,
    ) -> None:
        self._provider = provider
        self._trajectory_fetcher = trajectory_fetcher
        self._handoff_fetcher = handoff_fetcher
        self._final_call_fetcher = final_call_fetcher

    @classmethod
    def from_env(cls) -> KGSessionReader:
        """Build a reader on the SCOPE API's shared Neo4j connector.

        Connection settings come from the API library's own configuration
        (``NEO4J_HOST`` / ``NEO4J_URI``, ``NEO4J_PORT``, ``NEO4J_USERNAME``,
        ``NEO4J_PASSWORD``, ``NEO4J_DATABASE``).
        """
        # The SCOPE API reads NEO4J_* from the process environment at import time and
        # does not load .env files itself, so load ours before importing it.
        # Variables already exported in the shell take precedence.
        load_dotenv(find_dotenv(usecwd=True))

        from scope.client.dal import get_session_trajectory
        from scope.dependencies import get_neo4j_connector
        from scope.providers import SCOPEKGProvider

        db = get_neo4j_connector()
        # TODO: replace SCOPEKGProvider with LocalClient (or an extended version of it);
        # see the note on SessionProvider. ``get_session_trajectory`` is the DAL function
        # behind the REST route and the other workers (stateful-eval, cognitive-failure).
        return cls(
            SCOPEKGProvider(db=db),
            trajectory_fetcher=lambda session_id: get_session_trajectory(db, session_id),
            handoff_fetcher=lambda session_id: fetch_handoffs(db, session_id),
            final_call_fetcher=lambda session_id: fetch_final_agent_call(db, session_id),
        )

    def read(self, session_id: str) -> SessionData:
        # requirements=None -> the provider's default full-session fetch
        # (Session node + Agent/LLM/Tool calls + conversation).
        payload = self._provider.fetch(session_id, None)
        if not payload:
            raise SessionNotFoundError(f"No data found in the KG for session '{session_id}'")

        data = SessionData.from_payload(payload)
        logger.info(
            "Fetched session %s: %d agent calls, %d llm calls, %d tool calls",
            session_id,
            len(data.agent_spans),
            len(data.llm_spans),
            len(data.tool_spans),
        )
        return data

    @property
    def can_read_trajectory(self) -> bool:
        """Whether this reader was built with a trajectory fetcher (see ``read_trajectory``)."""
        return self._trajectory_fetcher is not None

    def read_trajectory(self, session_id: str) -> SessionTrajectory:
        """The session's calls in execution order, each with the AgentCall that owns it.

        This is how an LLMCall / ToolCall is tied to its AgentCall: ``parentSpanId`` on the
        call nodes points to OTel spans that are not nodes of the KG, while the graph edge
        ``AgentCall -[:hasLLMCall|hasToolCall]-> call`` is only visible through the trajectory.

        When the reader has a handoff fetcher (``from_env`` does), the handoffs between the agent
        calls that the normalisation wrote are added as ``handoffs``. A failing handoff query is
        logged and leaves ``handoffs`` at None: the order of the agent calls is then inferred from
        the trajectory alone (see ``symbolic_state.build_transitions``).

        When it has a final call fetcher (``from_env`` does), ``final_agent_call`` is the AgentCall
        that produced the final answer of the session (``fetch_final_agent_call``). A failing query
        is logged and leaves it None, like a session that has no final State yet.
        """
        if self._trajectory_fetcher is None:
            raise ValueError("This reader was built without a trajectory fetcher")
        trajectory = SessionTrajectory.model_validate(self._trajectory_fetcher(session_id))
        if self._handoff_fetcher is not None:
            try:
                trajectory.handoffs = self._handoff_fetcher(session_id)
            except Exception:
                logger.warning("Could not read the handoffs of session %s", session_id, exc_info=True)
            else:
                logger.info("Fetched %d handoffs of session %s", len(trajectory.handoffs), session_id)
        if self._final_call_fetcher is not None:
            try:
                trajectory.final_agent_call = self._final_call_fetcher(session_id)
            except Exception:
                logger.warning("Could not read the final agent call of session %s", session_id, exc_info=True)
            else:
                logger.info("Final agent call of session %s: %s", session_id, trajectory.final_agent_call)
        if not trajectory.steps:
            logger.warning("The KG has no trajectory steps for session %s", session_id)
        else:
            logger.info("Fetched trajectory of session %s: %d steps", session_id, len(trajectory.steps))
        return trajectory
