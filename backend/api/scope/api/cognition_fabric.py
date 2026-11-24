# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Optional Cognition Fabric routes and their Postgres startup setup."""

import logging
from contextlib import contextmanager

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from scope.api.api_v1.endpoints import cognition_store, realtime_events
from scope.dependencies import get_postgres_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])
router.include_router(
    realtime_events.router, prefix="/realtime", tags=["realtime-events"]
)
router.include_router(
    cognition_store.router, prefix="/cognition-store", tags=["cognition-store"]
)

CREATE_STORE_ITEMS = """
CREATE TABLE IF NOT EXISTS cognition_store_items (
    insertion_id BIGSERIAL PRIMARY KEY,
    ce_id UUID NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    type TEXT,
    value JSONB NOT NULL,
    metadata JSONB,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (namespace, key)
)
"""

CREATE_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS cognition_store_subscriptions (
    insertion_id BIGSERIAL PRIMARY KEY,
    id UUID NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    type TEXT NOT NULL,
    filter JSONB NOT NULL,
    callback_url TEXT NOT NULL
)
"""

MIGRATE_SUBSCRIPTIONS_NAMESPACE = """
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = to_regclass('cognition_store_subscriptions')
          AND attname = 'subscriber_id'
          AND NOT attisdropped
    ) THEN
        LOCK TABLE cognition_store_subscriptions IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_attribute
            WHERE attrelid = to_regclass('cognition_store_subscriptions')
              AND attname = 'subscriber_id'
              AND NOT attisdropped
        ) THEN
            ALTER TABLE cognition_store_subscriptions
                RENAME COLUMN subscriber_id TO namespace;
        END IF;
    END IF;
END
$migration$
"""


def ensure_cognition_store_tables() -> None:
    try:
        with contextmanager(get_postgres_db)() as db:
            db.execute_command(CREATE_STORE_ITEMS)
            db.execute_command(MIGRATE_SUBSCRIPTIONS_NAMESPACE)
            db.execute_command(CREATE_SUBSCRIPTIONS)
    except (SQLAlchemyError, ImportError):
        logger.exception("Could not initialize Cognition Store tables in Postgres")
