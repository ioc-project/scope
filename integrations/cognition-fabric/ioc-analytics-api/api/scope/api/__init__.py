"""FastAPI REST wrapper — a thin HTTP layer around the local library."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from scope.api.api_v1.api import router as v1_router
from scope.api.api_v2.api import router as v2_router
from scope.core.exceptions import OXPError
from scope.health import healthz

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run one-time setup before the app starts serving requests."""
    _ensure_tables()
    _check_redis()
    yield


CREATE_TRACE_LABELS = """
CREATE TABLE IF NOT EXISTS trace_labels (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL UNIQUE
)
"""

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


def _check_redis() -> None:
    """Ping Redis and log the result so operators can confirm it is reachable."""
    try:
        from scope.core.config import settings
        from scope.dependencies import get_redis

        client = get_redis()
        client.ping()
        logger.info("Redis is reachable — %s", settings.CACHE_REDIS_URL)
        logger.info("Redis CACHING ENABLED !")
    except Exception as exc:
        logger.warning("Redis is NOT reachable: %s", exc)
        logger.info("Redis CACHING will be DISABLED !")


def _ensure_tables() -> None:
    """Create tables that may not exist yet (idempotent)."""
    try:
        from scope.dependencies import get_db

        db = next(get_db())

        db.execute_command(CREATE_TRACE_LABELS)
        logger.info("trace_labels table ensured")

        db.execute_command(CREATE_STORE_ITEMS)
        logger.info("cognition_store_items table ensured")

        db.execute_command(MIGRATE_SUBSCRIPTIONS_NAMESPACE)
        logger.info("cognition_store_subscriptions migration applied")

        db.execute_command(CREATE_SUBSCRIPTIONS)
        logger.info("cognition_store_subscriptions table ensured")
    except Exception as exc:
        logger.error("could not ensure all tables: %s", exc)


app = FastAPI(
    title="OXP API",
    description="REST API to retrieve data from databases",
    version="0.2.0",
    lifespan=lifespan,
)


# ── Centralized exception handlers ───────────────────────────────────────────


@app.exception_handler(OXPError)
async def oxp_error_handler(request: Request, exc: OXPError) -> JSONResponse:
    """Map OXP domain exceptions to structured JSON error responses."""
    if exc.status_code < 500:
        # Expected client errors (e.g. 404, 422) — log concisely without a traceback.
        logger.info("%s [%d]: %s", type(exc).__name__, exc.status_code, exc.message)
    else:
        logger.error(
            "%s [%d]: %s",
            type(exc).__name__,
            exc.status_code,
            exc.message,
            exc_info=exc,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected errors — log and return 500."""
    logger.error("Unhandled %s: %s", type(exc).__name__, exc, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/healthz", tags=["health"])
def health_check():
    """Liveness / keep-alive probe."""
    return healthz()


# ── Versioned API (all new endpoints live here) ──────────────────────────────
app.include_router(v1_router)
app.include_router(v2_router)
