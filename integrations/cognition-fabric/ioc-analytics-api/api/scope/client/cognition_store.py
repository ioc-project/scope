"""Cognition client helpers used by API cognition endpoints."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from scope.connectors.base import Connector
from scope.connectors.sqlalchemy import SQLAlchemyConnector
from scope.core.exceptions import DatabaseError, NotFoundError
from scope.interfaces.models import CEFinding
from scope.interfaces.models import PriorFindingsResult
from scope.query_builders import cognition as cognition_queries
from scope.models.cognition_store import (
    StoreFilter,
    StoreItem,
    StoreItemRequest,
    Subscription,
    SubscriptionRequest,
)

logger = logging.getLogger(__name__)


def _ensure_findings_table(db: Connector) -> None:
    """Create the findings table if it does not already exist."""
    db.execute_command(cognition_queries.ensure_findings_table_command())


def _row_as_mapping(row: Any) -> dict[str, Any]:
    """Normalize row shapes from fake connectors and SQLAlchemy rows."""
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return dict(row)


def store_finding(db: Connector, finding: CEFinding) -> bool:
    """Store one CE finding in the cognition store backend."""
    if not finding.finding_id:
        finding.finding_id = str(uuid4())
    if not finding.timestamp:
        finding.timestamp = datetime.now(timezone.utc).isoformat()

    try:
        _ensure_findings_table(db)
    except Exception as exc:
        raise DatabaseError(
            f"Failed to ensure cognition findings table exists: {exc}"
        ) from exc

    command, params = cognition_queries.store_finding_command(finding=finding)
    try:
        db.execute_command(command, params)
    except Exception as exc:
        raise DatabaseError(f"Failed to store cognition finding: {exc}") from exc
    return True


def get_prior_findings(
    db: Connector,
    *,
    mas_id: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> PriorFindingsResult:
    """Fetch prior CE findings for a MAS, optionally scoped by trace/session."""
    try:
        _ensure_findings_table(db)
    except Exception as exc:
        raise DatabaseError(
            f"Failed to ensure cognition findings table exists: {exc}"
        ) from exc

    query, params = cognition_queries.get_prior_findings_query(
        mas_id=mas_id,
        trace_id=trace_id,
        session_id=session_id,
        limit=limit,
    )
    try:
        rows = db.execute(query, params) or []
    except Exception as exc:
        raise DatabaseError(f"Failed to fetch prior cognition findings: {exc}") from exc

    row_dicts = [_row_as_mapping(row) for row in rows]
    findings = [
        CEFinding(
            finding_id=str(row.get("finding_id") or ""),
            mas_id=str(row.get("mas_id") or ""),
            trace_id=str(row.get("trace_id") or ""),
            session_id=str(row.get("session_id") or ""),
            outcome=str(row.get("outcome") or ""),
            reason=str(row.get("reason") or ""),
            source_agent_id=str(row.get("source_agent_id") or ""),
            destination_agent_id=str(row.get("destination_agent_id") or ""),
            action_class=str(row.get("action_class") or ""),
            direction=str(row.get("direction") or ""),
            timestamp=str(row.get("timestamp") or ""),
        )
        for row in row_dicts
    ]
    return PriorFindingsResult(
        mas_id=mas_id,
        findings=findings,
        context_complete=len(findings) < limit,
    )


@contextmanager
def _store_session(db: SQLAlchemyConnector) -> Generator[Session]:
    try:
        with db.get_session() as session, session.begin():
            yield session
    except SQLAlchemyError as exc:
        raise DatabaseError("Cognition store database operation failed") from exc


def store_item(db: SQLAlchemyConnector, request: StoreItemRequest) -> StoreItem:
    params = request.model_dump(mode="json")
    params["value"] = json.dumps(params["value"], allow_nan=False)
    params["metadata"] = (
        json.dumps(params["metadata"]) if request.metadata is not None else None
    )
    with _store_session(db) as session:
        row = (
            session.execute(text(cognition_queries.STORE_ITEM), params).mappings().one()
        )
        item = StoreItem.model_validate(dict(row))
    return item


def get_store_items(db: SQLAlchemyConnector, filters: StoreFilter) -> list[StoreItem]:
    query, params = cognition_queries.get_store_items_query(filters)
    with _store_session(db) as session:
        rows = session.execute(text(query), params).mappings().all()
        return [StoreItem.model_validate(dict(row)) for row in rows]


def get_item_count(db: SQLAlchemyConnector) -> int:
    with _store_session(db) as session:
        return session.execute(text(cognition_queries.GET_ITEM_COUNT)).scalar_one()


def create_subscription(
    db: SQLAlchemyConnector, request: SubscriptionRequest
) -> Subscription:
    params = request.model_dump(mode="json")
    params["id"] = str(uuid4())
    params["filter"] = request.filter.model_dump_json(by_alias=True, exclude_none=True)
    with _store_session(db) as session:
        row = (
            session.execute(text(cognition_queries.STORE_SUBSCRIPTION), params)
            .mappings()
            .one()
        )
        return Subscription.model_validate(dict(row))


def get_subscriptions(
    db: SQLAlchemyConnector, namespace: str | None = None
) -> list[Subscription]:
    query, params = cognition_queries.get_subscriptions_query(namespace)
    with _store_session(db) as session:
        rows = session.execute(text(query), params).mappings().all()
        return [Subscription.model_validate(dict(row)) for row in rows]


def delete_subscription(db: SQLAlchemyConnector, subscription_id: UUID) -> None:
    with _store_session(db) as session:
        deleted = session.execute(
            text(cognition_queries.DELETE_SUBSCRIPTION),
            {"id": str(subscription_id)},
        ).scalar_one_or_none()
        if deleted is None:
            raise NotFoundError("Subscription not found")


def notify_subscriptions(item: StoreItem, subscriptions: list[Subscription]) -> None:
    """Deliver best-effort HTTP callbacks after the item transaction commits."""
    for subscription in subscriptions:
        if not subscription.matches(item):
            continue
        try:
            with requests.post(
                str(subscription.callback_url),
                json=item.model_dump(mode="json"),
                timeout=(3, 10),
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                if not 200 <= response.status_code < 300:
                    logger.error(
                        "Cognition subscription %s callback returned HTTP %s",
                        subscription.id,
                        response.status_code,
                    )
        except requests.RequestException:
            logger.exception(
                "Cognition subscription %s callback delivery failed", subscription.id
            )
