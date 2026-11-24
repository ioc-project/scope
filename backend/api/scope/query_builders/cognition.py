"""Query builders for cognition authorization operations."""

from __future__ import annotations

from typing import Any

from scope.interfaces.models import CEFinding
from scope.models.cognition_store import StoreFilter


def authorization_request_probe_query(
    *,
    mas_id: str,
    source_agent_id: str,
    destination_agent_id: str,
    trace_id: str,
) -> tuple[str, dict[str, Any]]:
    """Build a minimal authorization query used to validate DB reachability.

    The query currently echoes the request payload through the database engine.
    It can be replaced later with a real policy lookup while keeping the same
    call contract in the client layer.
    """
    query = """
    SELECT :mas_id AS mas_id,
           :source_agent_id AS source_agent_id,
           :destination_agent_id AS destination_agent_id,
           :trace_id AS trace_id
    """
    return query, {
        "mas_id": mas_id,
        "source_agent_id": source_agent_id,
        "destination_agent_id": destination_agent_id,
        "trace_id": trace_id,
    }


def ensure_findings_table_command() -> str:
    """Build DDL to ensure the findings table exists in Postgres."""
    return """
    CREATE TABLE IF NOT EXISTS ce_findings (
        finding_id TEXT PRIMARY KEY,
        mas_id TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL,
        source_agent_id TEXT NOT NULL,
        destination_agent_id TEXT NOT NULL,
        action_class TEXT NOT NULL,
        direction TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL
    )
    """


def store_finding_command(*, finding: CEFinding) -> tuple[str, dict[str, Any]]:
    """Build a write command that upserts one CE finding row."""
    command = """
    INSERT INTO ce_findings (
        finding_id,
        mas_id,
        trace_id,
        session_id,
        outcome,
        reason,
        source_agent_id,
        destination_agent_id,
        action_class,
        direction,
        timestamp
    ) VALUES (
        :finding_id,
        :mas_id,
        :trace_id,
        :session_id,
        :outcome,
        :reason,
        :source_agent_id,
        :destination_agent_id,
        :action_class,
        :direction,
        :timestamp
    )
    ON CONFLICT (finding_id) DO UPDATE
    SET mas_id = EXCLUDED.mas_id,
        trace_id = EXCLUDED.trace_id,
        session_id = EXCLUDED.session_id,
        outcome = EXCLUDED.outcome,
        reason = EXCLUDED.reason,
        source_agent_id = EXCLUDED.source_agent_id,
        destination_agent_id = EXCLUDED.destination_agent_id,
        action_class = EXCLUDED.action_class,
        direction = EXCLUDED.direction,
        timestamp = EXCLUDED.timestamp
    """
    return command, {
        "finding_id": finding.finding_id,
        "mas_id": finding.mas_id,
        "trace_id": finding.trace_id,
        "session_id": finding.session_id,
        "outcome": finding.outcome,
        "reason": finding.reason,
        "source_agent_id": finding.source_agent_id,
        "destination_agent_id": finding.destination_agent_id,
        "action_class": finding.action_class,
        "direction": finding.direction,
        "timestamp": finding.timestamp,
    }


def get_prior_findings_query(
    *,
    mas_id: str,
    trace_id: str | None,
    session_id: str | None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """Build a query that fetches prior findings for one MAS."""
    query = """
    SELECT finding_id,
           mas_id,
           trace_id,
           session_id,
           outcome,
           reason,
           source_agent_id,
           destination_agent_id,
           action_class,
           direction,
           timestamp
    FROM ce_findings
    WHERE mas_id = :mas_id
      AND (:trace_id IS NULL OR trace_id = :trace_id)
      AND (:session_id IS NULL OR session_id = :session_id)
    ORDER BY timestamp DESC
    LIMIT :limit
    """
    return query, {
        "mas_id": mas_id,
        "trace_id": trace_id,
        "session_id": session_id,
        "limit": limit,
    }


CREATE_STORE_ITEMS = """
INSERT INTO cognition_store_items (ce_id, namespace, key, type, value, metadata)
VALUES (:ce_id, :namespace, :key, :type, CAST(:value AS JSONB), CAST(:metadata AS JSONB))
ON CONFLICT (namespace, key) DO UPDATE
SET ce_id = EXCLUDED.ce_id,
    type = EXCLUDED.type,
    value = EXCLUDED.value,
    metadata = EXCLUDED.metadata,
    timestamp = clock_timestamp()
RETURNING ce_id, namespace, key, type, value, metadata, timestamp
"""

STORE_ITEM = """
INSERT INTO cognition_store_items (ce_id, namespace, key, type, value, metadata)
VALUES (:ce_id, :namespace, :key, :type, CAST(:value AS JSONB), CAST(:metadata AS JSONB))
ON CONFLICT (namespace, key) DO UPDATE
SET ce_id = EXCLUDED.ce_id,
    type = EXCLUDED.type,
    value = EXCLUDED.value,
    metadata = EXCLUDED.metadata,
    timestamp = clock_timestamp()
RETURNING ce_id, namespace, key, type, value, metadata, timestamp
"""

STORE_SUBSCRIPTION = """
INSERT INTO cognition_store_subscriptions (id, namespace, type, filter, callback_url)
VALUES (:id, :namespace, :type, CAST(:filter AS JSONB), :callback_url)
RETURNING id, namespace, type, filter, callback_url
"""

GET_ITEM_COUNT = "SELECT COUNT(*) FROM cognition_store_items"

DELETE_SUBSCRIPTION = """
DELETE FROM cognition_store_subscriptions WHERE id = :id RETURNING id
"""


def get_subscriptions_query(
    namespace: str | None,
) -> tuple[str, dict[str, Any]]:
    query = """
    SELECT id, namespace, type, filter, callback_url
    FROM cognition_store_subscriptions
    """
    params = {}
    if namespace is not None:
        query += " WHERE namespace = :namespace"
        params["namespace"] = namespace
    return query + " ORDER BY insertion_id ASC", params


def get_store_items_query(filters: StoreFilter) -> tuple[str, dict[str, Any]]:
    conditions = []
    params = {}
    for index, (field, value) in enumerate(
        filters.model_dump(mode="json", by_alias=True, exclude_none=True).items()
    ):
        column = (
            f"metadata ->> '{field.removeprefix('metadata.')}'"
            if field.startswith("metadata.")
            else field
        )
        parameter = f"filter_{index}"
        conditions.append(f"{column} = :{parameter}")
        params[parameter] = value
    query = """
    SELECT ce_id, namespace, key, type, value, metadata, timestamp
    FROM cognition_store_items
    """
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    return query + " ORDER BY insertion_id ASC", params
