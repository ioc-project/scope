"""Contracts for generic cognition-store items and subscriptions."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue


class StoreMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mas_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    tag: str | None = None


class StoreItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ce_id: UUID = Field(description="UUID of the CE instance that wrote this item.")
    namespace: str = Field(
        min_length=1, description="Schema scope, such as a CE domain or global."
    )
    key: str = Field(
        min_length=1, description="Producer-chosen ID, unique per namespace."
    )
    type: str | None = Field(
        default=None,
        description="Optional identifier describing the structure of value.",
    )
    value: JsonValue = Field(
        description="Arbitrary JSON whose shape is defined by the CE."
    )
    metadata: StoreMetadata | None = Field(
        default=None, description="Optional SCOPE correlation IDs and tag."
    )


class StoreItem(StoreItemRequest):
    timestamp: datetime = Field(
        description="Database-assigned UTC time of the latest write.",
        json_schema_extra={"readOnly": True},
    )


class StoreFilter(BaseModel):
    """Exact-match fields used by store-item retrieval."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str | None = None
    ce_id: UUID | None = None
    namespace: str | None = None
    type: str | None = Field(
        default=None, description="Stored value's type identifier to match exactly."
    )
    mas_id: str | None = Field(default=None, alias="metadata.mas_id")
    session_id: str | None = Field(default=None, alias="metadata.session_id")
    trace_id: str | None = Field(default=None, alias="metadata.trace_id")
    span_id: str | None = Field(default=None, alias="metadata.span_id")
    tag: str | None = Field(default=None, alias="metadata.tag")


class SubscriptionFilter(BaseModel):
    """Exact-match metadata fields applied after subscription namespace and type."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mas_id: str | None = Field(default=None, alias="metadata.mas_id")
    session_id: str | None = Field(default=None, alias="metadata.session_id")
    trace_id: str | None = Field(default=None, alias="metadata.trace_id")
    span_id: str | None = Field(default=None, alias="metadata.span_id")

    def matches(self, item: StoreItem) -> bool:
        metadata = item.metadata.model_dump() if item.metadata is not None else {}
        return all(
            metadata.get(name) == value
            for name, value in self.model_dump(exclude_none=True).items()
        )


class SubscriptionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None


class SubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(
        min_length=1, description="Store-item namespace to match exactly."
    )
    type: str = Field(
        min_length=1, description="Store-item value type to match exactly."
    )
    filter: SubscriptionFilter = Field(
        default_factory=SubscriptionFilter,
        description="Additional metadata filters combined with namespace and type using AND.",
    )
    callback_url: HttpUrl


class Subscription(SubscriptionRequest):
    id: UUID

    def matches(self, item: StoreItem) -> bool:
        return (
            self.namespace == item.namespace
            and self.type == item.type
            and self.filter.matches(item)
        )


class CognitionStoreInfo(BaseModel):
    version: str
    uptime: float = Field(ge=0, description="Process uptime in seconds.")
    item_count: int = Field(ge=0)
