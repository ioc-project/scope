# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Cognition store endpoints."""

from __future__ import annotations

import logging
from enum import Enum
from importlib.metadata import version
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from scope.client import cognition_store
from scope.client.cognition_store import get_prior_findings, store_finding
from scope.connectors.base import Connector
from scope.connectors.sqlalchemy import SQLAlchemyConnector
from scope.dependencies import get_postgres_db
from scope.interfaces.models import CEFinding
from scope.models.cognition_store import (
    CognitionStoreInfo,
    StoreFilter,
    StoreItem,
    StoreItemRequest,
    Subscription,
    SubscriptionQuery,
    SubscriptionRequest,
)

logger = logging.getLogger(__name__)
_started_at = monotonic()
_version = version("ioc-scope-api")
PostgresDB = Annotated[SQLAlchemyConnector, Depends(get_postgres_db)]


class ActionClass(str, Enum):
    """Type of action being evaluated."""

    ACTION_CLASS_UNSPECIFIED = "ACTION_CLASS_UNSPECIFIED"
    ACTION_CLASS_MCP_TOOL_CALL = "ACTION_CLASS_MCP_TOOL_CALL"
    ACTION_CLASS_A2A_DELEGATION = "ACTION_CLASS_A2A_DELEGATION"
    ACTION_CLASS_AGENT_RESPONSE = "ACTION_CLASS_AGENT_RESPONSE"


class Direction(str, Enum):
    """Direction of the action being inspected."""

    DIRECTION_UNSPECIFIED = "DIRECTION_UNSPECIFIED"
    DIRECTION_REQUEST = "DIRECTION_REQUEST"
    DIRECTION_RESPONSE = "DIRECTION_RESPONSE"


class CEOutcome(str, Enum):
    """Decision outcome for one CE finding."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    MODIFY = "MODIFY"


class CEFindingRequest(BaseModel):
    """Request payload used to store one CE finding."""

    finding_id: str = ""
    mas_id: str
    trace_id: str = ""
    session_id: str = ""
    outcome: CEOutcome
    reason: str = ""
    source_agent_id: str = ""
    destination_agent_id: str = ""
    action_class: ActionClass = ActionClass.ACTION_CLASS_UNSPECIFIED
    direction: Direction = Direction.DIRECTION_UNSPECIFIED
    timestamp: str = ""


class CEFindingResponse(BaseModel):
    """Response payload that represents one CE finding."""

    finding_id: str
    mas_id: str
    trace_id: str
    session_id: str
    outcome: str
    reason: str
    source_agent_id: str
    destination_agent_id: str
    action_class: str
    direction: str
    timestamp: str


class StoreFindingResponse(BaseModel):
    """Result envelope for finding write requests."""

    success: bool
    finding_id: str


class PriorFindingsResponse(BaseModel):
    """Result envelope for prior-findings retrieval requests."""

    mas_id: str
    findings: list[CEFindingResponse] = Field(default_factory=list)
    context_complete: bool = True


router = APIRouter()


@router.get("/info", response_model=CognitionStoreInfo, tags=["cognition-store"])
def get_cognition_store_info(db: PostgresDB) -> CognitionStoreInfo:
    """Return API version, process uptime in seconds, and stored item count."""
    return CognitionStoreInfo(
        version=_version,
        uptime=monotonic() - _started_at,
        item_count=cognition_store.get_item_count(db),
    )


@router.post("/store", response_model=StoreItem, tags=["cognition-store"])
def create_store_item(request: StoreItemRequest, db: PostgresDB) -> StoreItem:
    """Persist an item, upserting by namespace/key, then evaluate subscriptions.

    The database assigns the timestamp on every write. Callbacks receive the
    full stored item as JSON via HTTP POST after commit. Delivery is best-effort,
    without redirects or retries; failures are logged and do not undo the write.
    """
    item = cognition_store.store_item(db, request)
    subscriptions = cognition_store.get_subscriptions(db)
    cognition_store.notify_subscriptions(item, subscriptions)
    return item


@router.get("/store", response_model=list[StoreItem], tags=["cognition-store"])
def list_store_items(
    filters: Annotated[StoreFilter, Query()], db: PostgresDB
) -> list[StoreItem]:
    """Return exact AND-matches in original insertion order, without pagination."""
    return cognition_store.get_store_items(db, filters)


@router.post(
    "/subscriptions",
    response_model=Subscription,
    response_model_exclude_none=True,
    status_code=201,
    tags=["cognition-store"],
)
def create_subscription(request: SubscriptionRequest, db: PostgresDB) -> Subscription:
    """Register a callback for matching namespace, type, and optional item filters.

    Null metadata filter fields are omitted from the response.
    """
    return cognition_store.create_subscription(db, request)


@router.get(
    "/subscriptions",
    response_model=list[Subscription],
    response_model_exclude_none=True,
    tags=["cognition-store"],
)
def list_subscriptions(
    db: PostgresDB, query: Annotated[SubscriptionQuery, Query()]
) -> list[Subscription]:
    """List subscriptions in insertion order, optionally scoped to a namespace.

    Null metadata filter fields are omitted from each response.
    """
    return cognition_store.get_subscriptions(db, query.namespace)


@router.delete(
    "/subscriptions/{id}",
    status_code=204,
    tags=["cognition-store"],
    responses={404: {"description": "Subscription not found"}},
)
def cancel_subscription(id: UUID, db: PostgresDB) -> Response:
    """Cancel a subscription, returning 404 when it does not exist."""
    cognition_store.delete_subscription(db, id)
    return Response(status_code=204)


@router.post(
    "/findings",
    response_model=StoreFindingResponse,
    tags=["cognition-store"],
)
async def create_ce_finding(
    request: CEFindingRequest,
    db: Connector = Depends(get_postgres_db),  # noqa: B008
) -> StoreFindingResponse:
    """Store one CE finding in cognition-store."""
    finding = CEFinding(
        finding_id=request.finding_id,
        mas_id=request.mas_id,
        trace_id=request.trace_id,
        session_id=request.session_id,
        outcome=request.outcome.value,
        reason=request.reason,
        source_agent_id=request.source_agent_id,
        destination_agent_id=request.destination_agent_id,
        action_class=request.action_class.value,
        direction=request.direction.value,
        timestamp=request.timestamp,
    )
    success = store_finding(db, finding)
    return StoreFindingResponse(success=success, finding_id=finding.finding_id)


@router.get(
    "/findings",
    response_model=PriorFindingsResponse,
    tags=["cognition-store"],
)
async def get_ce_prior_findings(
    mas_id: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Connector = Depends(get_postgres_db),  # noqa: B008
) -> PriorFindingsResponse:
    """Return prior CE findings for a MAS, optionally filtered by trace/session."""
    result = get_prior_findings(
        db,
        mas_id=mas_id,
        trace_id=trace_id,
        session_id=session_id,
        limit=limit,
    )
    return PriorFindingsResponse(
        mas_id=result.mas_id,
        findings=[
            CEFindingResponse(
                finding_id=f.finding_id,
                mas_id=f.mas_id,
                trace_id=f.trace_id,
                session_id=f.session_id,
                outcome=f.outcome,
                reason=f.reason,
                source_agent_id=f.source_agent_id,
                destination_agent_id=f.destination_agent_id,
                action_class=f.action_class,
                direction=f.direction,
                timestamp=f.timestamp,
            )
            for f in result.findings
        ],
        context_complete=result.context_complete,
    )
