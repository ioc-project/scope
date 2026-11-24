"""Request and response contracts for persisted realtime client events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

EventID = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EventCategory = Literal["llm", "tool", "mcp", "a2a", "user"]
NonEmptyString = Annotated[str, Field(min_length=1)]


class EventScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: NonEmptyString | None = None
    mas_id: NonEmptyString | None = Field(
        default=None, description="MAS identifier, stored as application_id."
    )


class RealtimeEventQuery(EventScope):
    start_time: str | None = Field(
        default=None,
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
        ),
        description="Inclusive RFC3339 timestamp, retaining up to nine fractional digits.",
    )
    start_event_id: EventID | None = None
    end_event_id: EventID | None = None
    categories: list[EventCategory] = Field(default_factory=list)
    event_types: list[NonEmptyString] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("start_time")
    @classmethod
    def validate_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_start(self) -> Self:
        if self.start_time is not None and self.start_event_id is not None:
            raise ValueError("Use either start_time or start_event_id, not both")
        return self


class SpanEventQuery(RealtimeEventQuery):
    include_session_events: bool = Field(
        default=False,
        description="Return the resolved session's events instead of only this span's events.",
    )


class RealtimeEventPage(BaseModel):
    session_id: str
    events: list[dict[str, JsonValue]]
    has_more: bool
