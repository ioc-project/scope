# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Scoped, chronological access to persisted realtime client events."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import JsonValue, TypeAdapter

from scope.connectors.base import Connector
from scope.core.exceptions import NotFoundError, ValidationError
from scope.models.realtime_events import EventScope, RealtimeEventPage, RealtimeEventQuery
from scope.query_builders import realtime_events as queries

_event_adapter = TypeAdapter(dict[str, JsonValue])


def _decode_event_id(event_id: str | bytes) -> str:
    return event_id.decode("ascii") if isinstance(event_id, bytes) else event_id


def _decode_event(event_id: str | bytes, payload: str) -> dict[str, JsonValue]:
    event = _event_adapter.validate_json(payload)
    event["event_id"] = _decode_event_id(event_id)
    return event


class RealtimeEventsClient:
    db: Connector

    def get_session_application_ids(self, *, session_id: str) -> list[str]:
        rows = self.db.execute(
            queries.session_application_ids_query(), {"session_id": session_id}
        )
        return [application_id for (application_id,) in rows]

    def get_realtime_event(
        self, *, event_id: str, scope: EventScope
    ) -> dict[str, JsonValue]:
        rows = self.db.execute(
            queries.event_query(scope),
            queries.scope_params(scope) | {"event_id": event_id},
        )
        if not rows:
            raise NotFoundError("Event not found in the requested scope")
        if len(rows) != 1:
            raise ValidationError("Event ID is ambiguous in the requested scope")
        return _decode_event(rows[0][1], rows[0][2])

    def get_realtime_events_for_span(
        self,
        *,
        span_id: str,
        query: RealtimeEventQuery,
        include_session_events: bool = False,
    ) -> RealtimeEventPage:
        rows = self.db.execute(
            queries.session_query(query),
            queries.scope_params(query) | {"span_id": span_id},
        )
        if not rows:
            raise NotFoundError("No session found for the span in the requested scope")
        if len(rows) != 1:
            raise ValidationError(
                "Span belongs to multiple sessions; query by session ID"
            )
        return self.get_realtime_events(
            session_id=rows[0][0],
            query=query,
            span_id=None if include_session_events else span_id,
        )

    def get_realtime_events(
        self,
        *,
        session_id: str,
        query: RealtimeEventQuery,
        span_id: str | None = None,
    ) -> RealtimeEventPage:
        anchors: dict[str, tuple[int, str]] = {}
        for name in ("start_event_id", "end_event_id"):
            event_id = getattr(query, name)
            if event_id is None:
                continue
            rows = self.db.execute(
                queries.event_query(query, session_id=session_id),
                queries.scope_params(query)
                | {"session_id": session_id, "event_id": event_id},
            )
            if not rows:
                raise NotFoundError(f"{name} not found in the requested scoped session")
            if len(rows) != 1:
                raise ValidationError(f"{name} is ambiguous in the requested session")
            anchors[name] = (rows[0][0], _decode_event_id(rows[0][1]))
        end = anchors.get("end_event_id")
        if end is not None:
            if "start_event_id" in anchors and anchors["start_event_id"] > end:
                raise ValidationError("start_event_id must not follow end_event_id")
            if query.start_time is not None:
                delta = datetime.fromisoformat(query.start_time) - datetime(
                    1970, 1, 1, tzinfo=timezone.utc
                )
                start_ns = (
                    (delta.days * 86400 + delta.seconds) * 1_000_000
                    + delta.microseconds
                ) * 1000
                fraction = re.search(r"\.(\d+)", query.start_time)
                if fraction is not None:
                    start_ns += int(fraction[1].ljust(9, "0")) % 1000
                if start_ns > end[0]:
                    raise ValidationError("start_time must not follow end_event_id")
        sql, params = queries.history_query(query, session_id, anchors, span_id=span_id)
        rows = self.db.execute(sql, params)
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        return RealtimeEventPage(
            session_id=session_id,
            events=[_decode_event(event_id, payload) for event_id, payload in rows],
            has_more=has_more,
        )
