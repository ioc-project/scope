"""OXPKGProvider — Neo4j-backed KGProvider + DataProvider.

Handles all knowledge-graph read operations: generic node queries,
session / agent / LLM-call listings, and execution-span fetching for
the MCE computation engine.

All Cypher is centralised in ``scope.query_builders.kg``.  No raw
query strings live in this file.

Query execution uses an ontology-first / workaround-fallback strategy
governed by :class:`~scope.workarounds.KGWorkaroundsConfig`.  See
``scope/workarounds.py`` for the full workaround catalogue.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import Any, Callable

from scope.connectors.base import Connector
from scope.interfaces import DataProvider, KGProvider
from scope.interfaces.models import QueryOptions, RetrievalRequest, RetrievalScope
from scope.query_builders import kg as kg_qb
from scope.query_builders import ui as ui_qb
from scope.workarounds import KGWorkaroundsConfig

logger = logging.getLogger(__name__)

# Valid Neo4j node label: starts with letter or underscore, alphanumeric + underscore only.
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_connector(
    db: Connector | None,
    connector: Connector | None,
    *,
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> Connector:
    """Return the supplied connector, or build one from environment variables."""
    resolved = db or connector
    if resolved is None:
        from scope.connectors.neo4j import Neo4JConnector
        from scope.core.config import resolve_neo4j_auth

        resolved_host = (
            host or os.getenv("NEO4J_HOST") or os.getenv("KG_DB_HOST") or "localhost"
        )
        if ":" in resolved_host and not resolved_host.startswith("bolt://"):
            hostname, _, port_str = resolved_host.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                hostname, port = resolved_host, 7687
        else:
            hostname, port = resolved_host, 7687
        default_username, default_password = resolve_neo4j_auth()
        resolved = Neo4JConnector(
            host=hostname,
            port=port,
            username=(username or os.getenv("KG_DB_USER") or default_username),
            password=(password or os.getenv("KG_DB_PASSWORD") or default_password),
            database=database
            or os.getenv("NEO4J_DATABASE")
            or os.getenv("KG_DB_NAME")
            or "neo4j",
        )
    resolved.ensure_connected()
    return resolved


class OXPKGProvider(KGProvider, DataProvider):
    """Neo4j-backed KGProvider + DataProvider.

    *KGProvider* — generic node queries, session / agent / LLM-call listings.
    *DataProvider* — execution-span fetching (``fetch``, ``resolve_session_id``)
    used by the MCE computation engine.

    Query execution is controlled by :class:`~scope.workarounds.KGWorkaroundsConfig`:
    ontology-first (default) or workaround-first (set ``MCE_WORKAROUNDS_FIRST=1``).
    """

    def __init__(
        self,
        db: Connector | None = None,
        *,
        connector: Connector | None = None,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        workarounds: KGWorkaroundsConfig | None = None,
    ) -> None:
        """Accept ``db=`` (canonical) or ``connector=`` (compat alias).

        Parameters
        ----------
        workarounds:
            Controls ontology-first vs workaround-first query execution.
            Defaults to :meth:`KGWorkaroundsConfig.from_env`.
        """
        self._db = _resolve_connector(
            db,
            connector,
            host=host,
            username=username,
            password=password,
            database=database,
        )
        self._workarounds = (
            workarounds if workarounds is not None else KGWorkaroundsConfig.from_env()
        )

    # ── Query execution helper ────────────────────────────────────────────

    def _execute_pair(
        self,
        ontology: tuple[str, dict],
        workaround: tuple[str, dict],
        *,
        has_results: Callable[[list], bool] | None = None,
        ctx: str = "",
    ) -> list | None:
        """Execute an (ontology, workaround) query pair.

        Order is governed by ``self._workarounds.workarounds_first``:
        ``False`` (default) tries the ontology query first;
        ``True`` tries the workaround first.
        """
        _has = has_results if has_results is not None else bool
        if self._workarounds.workarounds_first:
            primary, primary_label = workaround, "workaround"
            fallback, fallback_label = ontology, "ontology"
        else:
            primary, primary_label = ontology, "ontology"
            fallback, fallback_label = workaround, "workaround"

        try:
            rows = self._db.execute(*primary)
            if _has(rows):
                logger.debug("[%s] %s query succeeded", ctx, primary_label)
                return rows
            logger.debug(
                "[%s] %s query returned empty, trying %s",
                ctx,
                primary_label,
                fallback_label,
            )
        except Exception as exc:
            logger.debug(
                "[%s] %s query raised: %s — trying %s",
                ctx,
                primary_label,
                exc,
                fallback_label,
            )

        try:
            return self._db.execute(*fallback)
        except Exception as exc:
            logger.warning("[%s] %s fallback also failed: %s", ctx, fallback_label, exc)
            return None

    # ── KGProvider ────────────────────────────────────────────────────────

    def query_nodes(
        self,
        entity_type: str,
        options: QueryOptions | None = None,
    ) -> list[dict[str, Any]]:
        """Query KG nodes by label with SQL-like filters, projection, and ordering.

        ``entity_type`` must be a valid Neo4j label (alphanumeric / underscore)
        or ``"*"`` to match all nodes.  Invalid labels raise ``ValueError``
        rather than silently injecting Cypher.
        """
        if entity_type and entity_type != "*" and not _LABEL_RE.match(entity_type):
            raise ValueError(f"Invalid entity_type label: {entity_type!r}")

        options = options or QueryOptions()
        label_part = f":{entity_type}" if entity_type and entity_type != "*" else ""
        clauses = [f"MATCH (n{label_part})"]
        params: dict[str, Any] = {}
        where_parts: list[str] = []

        if options.filters:
            for k, v in options.filters.items():
                pk = f"filter_{k}"
                where_parts.append(
                    f"n.{k} IN ${pk}" if isinstance(v, list) else f"n.{k} = ${pk}"
                )
                params[pk] = v

        if options.where_clause:
            # NOTE: where_clause is an internal escape-hatch for trusted callers only.
            # It is not exposed over HTTP and must never receive user-supplied strings.
            wc = options.where_clause.strip()
            if wc.lower().startswith("where"):
                wc = wc[5:].strip()
            where_parts.append(f"({wc})")

        if where_parts:
            clauses.append("WHERE " + " AND ".join(where_parts))

        if options.columns and options.columns != ["*"]:
            ret_parts = [
                col
                if ("(" in col or " AS " in col.upper() or "." in col)
                else f"n.{col} AS {col}"
                for col in options.columns
            ]
            clauses.append("RETURN " + ", ".join(ret_parts))
        else:
            clauses.append("RETURN n")

        if options.order_by:
            sort_parts = []
            for field in options.order_by:
                desc = field.startswith("-")
                f_clean = field.lstrip("-")
                if "." not in f_clean:
                    f_clean = f"n.{f_clean}"
                sort_parts.append(f"{f_clean} {'DESC' if desc else 'ASC'}")
            clauses.append("ORDER BY " + ", ".join(sort_parts))

        if options.limit:
            clauses.append(f"LIMIT {int(options.limit)}")

        try:
            return self._db.execute("\n".join(clauses), params) or []
        except Exception as exc:
            logger.error("query_nodes(%s) failed: %s", entity_type, exc)
            return []

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        """List sessions ordered by most recent first."""
        query, params = kg_qb.list_sessions_query(limit)
        try:
            return self._db.execute(query, params) or []
        except Exception as exc:
            logger.error("list_sessions failed: %s", exc)
            return []

    def list_agents(self, session_id: str) -> list[dict[str, Any]]:
        """List AgentCall nodes for a session (ontology-first, WK-01 fallback)."""
        rows = self._execute_pair(
            *kg_qb.list_agents_query(session_id), ctx="list_agents"
        )
        return rows or []

    def list_llm_calls(self, session_id: str) -> list[dict[str, Any]]:
        """List LLMCall nodes for a session (ontology-first, WK-01 fallback)."""
        rows = self._execute_pair(
            *kg_qb.list_llm_calls_query(session_id), ctx="list_llm_calls"
        )
        return rows or []

    # ── DataProvider ──────────────────────────────────────────────────────

    def resolve_session_id(self, resource_id: str) -> str | None:
        """Resolve any resource ID to its parent Session ID."""
        for query, params in kg_qb.resolve_session_id_queries(resource_id):
            try:
                rows = self._db.execute(query, params)
                if rows and rows[0].get("sid"):
                    return rows[0]["sid"]
            except Exception as exc:
                logger.debug("resolve_session_id query failed: %s", exc)
        return None

    def retrieve(self, request: RetrievalRequest) -> dict[str, Any]:
        """Execute a faceted retrieval request.

        This V1 implementation supports declarative session scoping, ordered
        AgentCall sequence retrieval, basic client-side filtering/ordering, and
        simple aggregates such as counts.
        """
        session_ids = self._resolve_scope_session_ids(request)
        contexts: dict[str, dict[str, Any]] = {}

        for session_id in session_ids:
            context: dict[str, Any] = {"session_id": session_id}

            for node in request.nodes:
                alias = node.alias or self._context_key_for_entity(node.entity_type)
                records = self._retrieve_node_records(session_id, node, request)
                if node.limit is not None:
                    records = records[: node.limit]

                if self._context_key_for_entity(node.entity_type) == "session":
                    context[alias] = records[0] if records else {}
                else:
                    context[alias] = records

            self._apply_aggregates(context, request)
            contexts[session_id] = context

        return {
            "session_ids": session_ids,
            "contexts": contexts,
        }

    def fetch(self, resource_id: str, requirements: Any) -> dict[str, Any]:
        """Fetch execution spans from Neo4j for metric computation.

        Returns a dict with keys: session_id, session, agent_spans, llm_spans,
        tool_spans, conversation, conversation_data, input_text, output_text.

        Uses the ontology-first / workaround-fallback strategy from
        :class:`~scope.workarounds.KGWorkaroundsConfig` for each sub-query.
        """
        try:
            retrieval_request = requirements.retrieval
        except AttributeError:
            retrieval_request = None
        if retrieval_request is not None:
            request = self._anchor_request(retrieval_request, resource_id)
            retrieved = self.retrieve(request)
            contexts = retrieved.get("contexts", {})
            session_ids = retrieved.get("session_ids", [])
            session_id = (
                resource_id
                if resource_id in contexts
                else (session_ids[0] if session_ids else None)
            )
            return contexts.get(session_id, {}) if session_id is not None else {}

        # 1. Main record (session + spans)
        rows = self._execute_pair(
            *kg_qb.fetch_session_query(resource_id),
            has_results=lambda r: (
                bool(r) and bool(r[0].get("llm_calls") or r[0].get("tool_calls"))
            ),
            ctx="fetch_session",
        )
        record = rows[0] if rows else None

        if record is None:
            logger.warning("Session %s not found in Neo4j", resource_id)
            return {}

        session_node = record.get("s") or {}
        session_props = (
            dict(session_node.items())
            if hasattr(session_node, "items")
            else dict(session_node)
        )

        # 2. Session timing (WK-02)
        timing_rows = self._execute_pair(
            *kg_qb.fetch_session_timing_query(resource_id), ctx="fetch_timing"
        )
        if timing_rows:
            t = timing_rows[0]
            if t.get("startTime") is not None:
                session_props.setdefault("startTime", t["startTime"])
            if t.get("duration") is not None:
                session_props.setdefault("duration", t["duration"])

        # 3. Conversation data (Transitions / States)
        conversation_data: dict[str, Any] = {}
        conv_rows = self._execute_pair(
            *kg_qb.fetch_conversation_query(resource_id), ctx="fetch_conversation"
        )
        if conv_rows:
            if conv_rows[0].get("transId") is not None:
                # Ontology path: Transition → State rows
                inputs = [r["input"] for r in conv_rows if r.get("input")]
                outputs = [r["output"] for r in conv_rows if r.get("output")]
                conversation_data = {
                    "query": inputs[0] if inputs else "",
                    "response": outputs[-1] if outputs else "",
                    "conversation": "\n\n".join(
                        f"[{r.get('input_stateId')}\u2192{r.get('output_stateId')}] "
                        f"{(r.get('input') or '')[:200]} \u2192 {(r.get('output') or '')[:200]}"
                        for r in conv_rows
                        if r.get("input") or r.get("output")
                    ),
                }
            else:
                # Workaround path: flat State rows
                conversation_data = {
                    "query": conv_rows[0].get("content", ""),
                    "response": conv_rows[-1].get("content", ""),
                    "conversation": "\n\n".join(
                        f"[{r.get('stateId')}] {r.get('content', '')}"
                        for r in conv_rows
                        if r.get("content")
                    ),
                }

        def _as_dict(n: Any) -> dict[str, Any]:
            return dict(n.items()) if hasattr(n, "items") else dict(n)

        tool_call_transitions = self._fetch_tool_call_transition_rows(resource_id)
        agent_spans = [
            self._normalize_agent_call(_as_dict(n))
            for n in (record.get("agent_calls") or [])
            if n
        ]
        llm_spans = [
            self._normalize_llm_call(_as_dict(n))
            for n in (record.get("llm_calls") or [])
            if n
        ]
        tool_spans = [
            self._normalize_tool_call(
                _as_dict(n),
                tool_call_transition=tool_call_transitions.get(
                    str(_as_dict(n).get("executionId", ""))
                ),
            )
            for n in (record.get("tool_calls") or [])
            if n
        ]

        input_text = conversation_data.get("query", "")
        output_text = conversation_data.get("response", "")
        if not input_text or not output_text:
            fallback_input, fallback_output = self._derive_session_query_response(
                session_props,
                llm_spans,
            )
            input_text = input_text or fallback_input
            output_text = output_text or fallback_output
            if input_text and not conversation_data.get("query"):
                conversation_data["query"] = input_text
            if output_text and not conversation_data.get("response"):
                conversation_data["response"] = output_text

        return {
            "session_id": resource_id,
            "session": session_props,
            "agent_spans": agent_spans,
            "llm_spans": llm_spans,
            "tool_spans": tool_spans,
            "conversation": conversation_data.get("conversation", ""),
            "conversation_data": conversation_data,
            "input_text": input_text,
            "output_text": output_text,
        }

    def _resolve_scope_session_ids(self, request: RetrievalRequest) -> list[str]:
        session_ids: list[str] = []

        for session_id in request.scope.session_ids:
            if session_id not in session_ids:
                session_ids.append(session_id)

        for resource_id in request.scope.resource_ids:
            session_id = self.resolve_session_id(resource_id)
            if session_id and session_id not in session_ids:
                session_ids.append(session_id)

        if request.scope.start_time is not None or request.scope.end_time is not None:
            query, params = kg_qb.list_sessions_in_interval_query(
                request.scope.start_time,
                request.scope.end_time,
                request.limit,
            )
            rows = self._db.execute(query, params) or []
            for row in rows:
                session_id = row.get("session_id")
                if session_id and session_id not in session_ids:
                    session_ids.append(session_id)

        return session_ids

    def _anchor_request(
        self, request: RetrievalRequest, resource_id: str
    ) -> RetrievalRequest:
        if request.scope.session_ids or request.scope.resource_ids:
            return request

        session_id = self.resolve_session_id(resource_id) or resource_id
        return replace(
            request,
            scope=RetrievalScope(session_ids=[session_id]),
        )

    @staticmethod
    def _context_key_for_entity(entity_type: str) -> str:
        mapping = {
            "mas:AgentCall": "agent_calls",
            "AgentCall": "agent_calls",
            "mas:LLMCall": "llm_calls",
            "LLMCall": "llm_calls",
            "mas:ToolCall": "tool_calls",
            "ToolCall": "tool_calls",
            "mas:MASCall": "mas_calls",
            "MASCall": "mas_calls",
            "mas:Session": "session",
            "Session": "session",
        }
        return mapping.get(entity_type, entity_type.split(":")[-1].lower())

    def _retrieve_node_records(
        self, session_id: str, node: Any, request: RetrievalRequest
    ) -> list[dict[str, Any]]:
        rows = (
            self._execute_pair(
                *kg_qb.fetch_traversed_nodes_query(
                    session_id,
                    node.entity_type,
                    request.edges,
                    order_by=node.order_by or request.order_by,
                ),
                has_results=bool,
                ctx=f"retrieve_{self._context_key_for_entity(node.entity_type)}",
            )
            or []
        )

        tool_call_transitions: dict[str, dict[str, Any]] = {}
        if self._context_key_for_entity(node.entity_type) == "tool_calls":
            tool_call_transitions = self._fetch_tool_call_transition_rows(session_id)

        records = [
            self._normalize_entity(
                node.entity_type,
                self._as_dict(row["node"]),
                tool_call_transition=tool_call_transitions.get(
                    str(self._as_dict(row["node"]).get("executionId", ""))
                ),
            )
            for row in rows
            if row.get("node")
        ]
        records = self._apply_filters(records, node.filters)
        records = self._sort_records(
            records,
            node.order_by
            or request.order_by
            or self._default_order_for_entity(node.entity_type),
        )
        if node.fields:
            records = [self._project_fields(record, node.fields) for record in records]
        return records

    @staticmethod
    def _project_fields(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        return {field: record.get(field) for field in fields}

    @staticmethod
    def _default_order_for_entity(entity_type: str) -> list[str]:
        key = entity_type.split(":")[-1]
        if key in {"AgentCall", "LLMCall", "ToolCall", "MASCall"}:
            return ["startTime", "start_time", "timestamp", "executionId"]
        return []

    def _normalize_entity(
        self,
        entity_type: str,
        node: dict[str, Any],
        *,
        tool_call_transition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = entity_type.split(":")[-1]
        if key == "ToolCall":
            return self._normalize_tool_call(
                node, tool_call_transition=tool_call_transition
            )
        if key == "LLMCall":
            return self._normalize_llm_call(node)
        if key == "AgentCall":
            return self._normalize_agent_call(node)
        return node

    def _fetch_tool_call_transition_rows(
        self, session_id: str
    ) -> dict[str, dict[str, Any]]:
        query, params = ui_qb.execution_hierarchy_tool_call_query(session_id=session_id)
        try:
            rows = self._db.execute(query, params) or []
        except Exception as exc:
            logger.debug(
                "tool call transition enrichment failed for %s: %s", session_id, exc
            )
            return {}

        indexed: dict[str, dict[str, Any]] = {}
        for row in rows:
            execution_id = row.get("executionId")
            if not execution_id:
                continue
            key = str(execution_id)
            current = indexed.get(key)
            if current is None or (
                not current.get("outputContent")
                and row.get("outputContent") is not None
            ):
                indexed[key] = self._as_dict(row)
        return indexed

    @staticmethod
    def _apply_filters(
        records: list[dict[str, Any]], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not filters:
            return records

        filtered: list[dict[str, Any]] = []
        for record in records:
            keep = True
            for key, expected in filters.items():
                value = record.get(key)
                if isinstance(expected, list):
                    if value not in expected:
                        keep = False
                        break
                elif value != expected:
                    keep = False
                    break
            if keep:
                filtered.append(record)
        return filtered

    @staticmethod
    def _sort_records(
        records: list[dict[str, Any]], order_by: list[str]
    ) -> list[dict[str, Any]]:
        if not order_by:
            return records

        sorted_records = list(records)
        for field in reversed(order_by):
            descending = field.startswith("-")
            field_name = field.lstrip("-")
            sorted_records.sort(
                key=lambda record: (
                    record.get(field_name) is None,
                    record.get(field_name),
                ),
                reverse=descending,
            )
        return sorted_records

    def _apply_aggregates(
        self, context: dict[str, Any], request: RetrievalRequest
    ) -> None:
        for aggregate in request.aggregates:
            alias = aggregate.alias or aggregate.function
            if aggregate.function != "count" or not aggregate.entity_type:
                raise NotImplementedError(
                    f"Unsupported aggregate facet: {aggregate.function!r}"
                )

            key = self._context_key_for_entity(aggregate.entity_type)
            records = context.get(key, [])
            if aggregate.group_by:
                grouped: dict[Any, int] = {}
                for record in records:
                    group_key = tuple(record.get(field) for field in aggregate.group_by)
                    normalized_key = group_key[0] if len(group_key) == 1 else group_key
                    grouped[normalized_key] = grouped.get(normalized_key, 0) + 1
                context[alias] = grouped
            else:
                context[alias] = len(records)

    @staticmethod
    def _as_dict(node: Any) -> dict[str, Any]:
        return dict(node.items()) if hasattr(node, "items") else dict(node)

    @staticmethod
    def _derive_session_query_response(
        session: dict[str, Any],
        llm_spans: list[dict[str, Any]],
    ) -> tuple[str, str]:
        def _pick_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
            for key in keys:
                value = mapping.get(key)
                if value not in (None, ""):
                    return str(value)
            return ""

        query = _pick_first(
            session,
            ("input_text", "input_query", "query", "prompt"),
        )
        response = _pick_first(
            session,
            ("output_text", "final_response", "response", "completion"),
        )

        if not query:
            for llm_call in llm_spans:
                query = _pick_first(llm_call, ("prompt", "inputContent", "input_text"))
                if query:
                    break

        if not response:
            for llm_call in reversed(llm_spans):
                response = _pick_first(
                    llm_call,
                    ("completion", "outputContent", "output_text"),
                )
                if response:
                    break

        return query, response

    # ── normalizers ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_tool_call(
        node: dict[str, Any],
        *,
        tool_call_transition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = dict(node)
        transition = tool_call_transition or {}

        if (
            normalized.get("toolArguments") is None
            and normalized.get("inputParams") is not None
        ):
            normalized["toolArguments"] = normalized["inputParams"]

        output_content = normalized.get("outputContent")
        tool_output = normalized.get("toolOutput")
        transition_output = transition.get("outputContent")
        if output_content is None and transition_output is not None:
            normalized["outputContent"] = transition_output
            output_content = transition_output
        if tool_output is None and transition_output is not None:
            normalized["toolOutput"] = transition_output
            tool_output = transition_output
        if normalized.get("outputContent") is None and tool_output is not None:
            normalized["outputContent"] = tool_output
        if normalized.get("toolOutput") is None and output_content is not None:
            normalized["toolOutput"] = output_content

        if (
            normalized.get("toolName") is None
            and transition.get("toolName") is not None
        ):
            normalized["toolName"] = transition["toolName"]
        if (
            normalized.get("inputParams") is None
            and transition.get("inputParams") is not None
        ):
            normalized["inputParams"] = transition["inputParams"]
        if (
            normalized.get("inputContent") is None
            and transition.get("inputContent") is not None
        ):
            normalized["inputContent"] = transition["inputContent"]
        if "contains_error" not in normalized:
            status = (
                normalized.get("status")
                or normalized.get("status_code")
                or normalized.get("statusCode")
            )
            normalized["contains_error"] = bool(
                (isinstance(status, str) and status.upper() == "ERROR")
                or normalized.get("error") not in (None, "", False)
            )

        return normalized

    @staticmethod
    def _normalize_llm_call(node: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(node)
        if (
            normalized.get("prompt") is None
            and normalized.get("inputContent") is not None
        ):
            normalized["prompt"] = normalized["inputContent"]
        if (
            normalized.get("completion") is None
            and normalized.get("outputContent") is not None
        ):
            normalized["completion"] = normalized["outputContent"]
        if "contains_error" not in normalized:
            status = (
                normalized.get("status")
                or normalized.get("status_code")
                or normalized.get("statusCode")
            )
            normalized["contains_error"] = bool(
                (isinstance(status, str) and status.upper() == "ERROR")
                or normalized.get("error") not in (None, "", False)
            )
        return normalized

    @staticmethod
    def _normalize_agent_call(node: dict[str, Any]) -> dict[str, Any]:
        return node
