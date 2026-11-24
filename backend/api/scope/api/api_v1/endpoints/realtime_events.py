# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Pull access to the normalized events persisted by event-exporter."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import JsonValue

from scope.client._realtime_events import RealtimeEventsClient
from scope.connectors.base import Connector
from scope.dependencies import get_db
from scope.models.realtime_events import (
    EventScope,
    RealtimeEventPage,
    RealtimeEventQuery,
    SpanEventQuery,
)

router = APIRouter()


def _client(db: Annotated[Connector, Depends(get_db)]) -> RealtimeEventsClient:
    from scope.client.local import LocalClient

    return LocalClient(db=db)


@router.get("/sessions/{session_id}/events", response_model=RealtimeEventPage)
def get_session_events(
    session_id: str,
    query: Annotated[RealtimeEventQuery, Query()],
    client: Annotated[RealtimeEventsClient, Depends(_client)],
) -> RealtimeEventPage:
    """Read a session in (event_time, event_id) order, with inclusive event bounds."""
    return client.get_realtime_events(session_id=session_id, query=query)


@router.get("/spans/{span_id}/events", response_model=RealtimeEventPage)
def get_span_session_events(
    span_id: Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")],
    query: Annotated[SpanEventQuery, Query()],
    client: Annotated[RealtimeEventsClient, Depends(_client)],
) -> RealtimeEventPage:
    """Return this span's events; opt into full session history with include_session_events."""
    return client.get_realtime_events_for_span(
        span_id=span_id,
        query=query,
        include_session_events=query.include_session_events,
    )


@router.get("/events/{event_id}")
def get_event(
    event_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    scope: Annotated[EventScope, Query()],
    client: Annotated[RealtimeEventsClient, Depends(_client)],
) -> dict[str, JsonValue]:
    """Retrieve one event with its full normalized payload."""
    return client.get_realtime_event(event_id=event_id, scope=scope)
