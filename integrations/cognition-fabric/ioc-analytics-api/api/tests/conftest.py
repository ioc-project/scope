"""Shared fixtures for PostgreSQL-backed API tests."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _postgres_test_url() -> str | None:
    """Return a SQLAlchemy Postgres URL using an installed DB driver."""
    postgres_host = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port = os.getenv("POSTGRES_PORT", "5432")
    postgres_db = os.getenv("POSTGRES_DB", "oxp")
    postgres_user = os.getenv("POSTGRES_USER")
    postgres_password = os.getenv("POSTGRES_PASSWORD")

    # Integration tests must receive credentials through env vars.
    if not postgres_user or not postgres_password:
        return None

    if importlib.util.find_spec("psycopg2") is not None:
        return (
            f"postgresql://{postgres_user}:{postgres_password}"
            f"@{postgres_host}:{postgres_port}/{postgres_db}"
        )
    if importlib.util.find_spec("psycopg") is not None:
        return (
            f"postgresql+psycopg://{postgres_user}:{postgres_password}"
            f"@{postgres_host}:{postgres_port}/{postgres_db}"
        )
    if importlib.util.find_spec("pg8000") is not None:
        return (
            f"postgresql+pg8000://{postgres_user}:{postgres_password}"
            f"@{postgres_host}:{postgres_port}/{postgres_db}"
        )
    return None


@pytest.fixture
def postgres_test_url() -> str:
    url = os.getenv("TEST_COGNITION_STORE_URL") or _postgres_test_url()
    if url is None:
        pytest.skip(
            "Set TEST_COGNITION_STORE_URL or PostgreSQL credentials and install a "
            "PostgreSQL DBAPI driver to run this integration test."
        )
    return url


@pytest.fixture
def store_client(postgres_test_url: str) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from scope.api import app
    from scope.connectors.sqlalchemy import SQLAlchemyConnector
    from scope.dependencies import get_postgres_db

    schema = f"test_store_item_{uuid4().hex}"
    admin = SQLAlchemyConnector(url=postgres_test_url)
    admin.connect()
    db = SQLAlchemyConnector(
        url=postgres_test_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        admin.execute_command(f"CREATE SCHEMA {schema}")
        db.connect()
        with pytest.MonkeyPatch.context() as patch:
            patch.setitem(app.dependency_overrides, get_postgres_db, lambda: db)
            yield TestClient(app)
    finally:
        db.close()
        try:
            admin.execute_command(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            admin.close()
