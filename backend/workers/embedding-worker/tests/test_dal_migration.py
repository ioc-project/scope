#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""
Direct integration test for scope_api_get_state_content.

Requires a running Neo4j instance.  Connection settings are read from
environment variables with the following defaults:

  NEO4J_HOST     bolt://localhost:7687
  NEO4J_USERNAME     neo4j
  NEO4J_PASSWORD testpassword
  NEO4J_DB       neo4j

Optional env var:
    TEST_SESSION_ID session id used for FILTERS_EQ
"""

import logging
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

try:
    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        get_session_io as scope_api_get_session_io,
    )
    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        get_state_content as scope_api_get_state_content,
    )
    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        ingest_embeddings as scope_api_ingest_embeddings,
    )
    from scope.connectors.neo4j import Neo4JConnector  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError:
    _REPO_ROOT = Path(__file__).resolve().parents[3]
    _API_SRC = _REPO_ROOT / "api"
    if _API_SRC.exists() and str(_API_SRC) not in sys.path:
        sys.path.insert(0, str(_API_SRC))

    def _force_namespace_package(name: str, path: Path) -> None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        # Force replacement in case another plugin loaded a different
        # top-level scope package without the expected submodules.
        sys.modules[name] = module

    _force_namespace_package("scope", _API_SRC / "scope")
    _force_namespace_package("scope.client", _API_SRC / "scope" / "client")
    _force_namespace_package("scope.connectors", _API_SRC / "scope" / "connectors")
    _force_namespace_package("scope.query_builders", _API_SRC / "scope" / "query_builders")
    _force_namespace_package("scope.core", _API_SRC / "scope" / "core")

    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        get_session_io as scope_api_get_session_io,
    )
    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        get_state_content as scope_api_get_state_content,
    )
    from scope.client.dal import (  # pyright: ignore[reportMissingImports]
        ingest_embeddings as scope_api_ingest_embeddings,
    )
    from scope.connectors.neo4j import Neo4JConnector  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _build_neo4j_uri(raw: str) -> str:
    """Ensure the URI has a bolt:// scheme even when NEO4J_HOST is set to
    a bare hostname (e.g. 'localhost' or 'localhost:7687')."""
    if raw and ("://" in raw):
        return raw
    return f"bolt://{raw}"


_NEO4J_HOST = os.getenv("NEO4J_HOST", "localhost:7687")
NEO4J_URI = _build_neo4j_uri(_NEO4J_HOST)
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "testpassword")  # legit:ignore
NEO4J_DB = os.getenv("NEO4J_DB", "neo4j")
TEST_SESSION_ID = os.getenv("TEST_SESSION_ID", "e9815583-76e1-479b-9098-ba0c3f066bc8")
FILTERS_EQ = {"sessionId": TEST_SESSION_ID}
FILTERS_NEQ = {"content": ""}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scope_api_get_state_content():
    connector = Neo4JConnector(
        host=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DB,
    )

    connector.connect()
    try:
        result_api = scope_api_get_state_content(
            db=connector,
            filters_eq=FILTERS_EQ,
            filters_neq=FILTERS_NEQ,
        )
    finally:
        connector.close()

    logger.info("get_state_content → %d item(s)", len(result_api))

    assert isinstance(result_api, list)
    if result_api:
        assert "id" in result_api[0]
        assert "content" in result_api[0]

    for r in result_api:
        print(r)


def test_scope_api_get_session_io():
    connector = Neo4JConnector(
        host=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DB,
    )

    connector.connect()
    try:
        result_api = scope_api_get_session_io(
            db=connector,
            session_id=TEST_SESSION_ID,
        )
    finally:
        connector.close()

    logger.info("get_session_io → %d item(s)", len(result_api))

    assert isinstance(result_api, list)
    if result_api:
        assert "id" in result_api[0]
        assert "content" in result_api[0]

    for r in result_api:
        print(r)


def test_scope_api_ingest_embeddings():
    connector = Neo4JConnector(
        host=NEO4J_URI,
        username=NEO4J_USERNAME,
        password=NEO4J_PASSWORD,
        database=NEO4J_DB,
    )

    created_embedding_id = ""
    connector.connect()
    try:
        state_rows = scope_api_get_state_content(
            db=connector,
            filters_eq=FILTERS_EQ,
            filters_neq=FILTERS_NEQ,
        )
        if not state_rows:
            pytest.skip("No states available for ingest_embeddings integration test")

        state_id = state_rows[0]["id"]
        embedding_model = f"test-model-{uuid.uuid4().hex[:8]}"
        created_embedding_id = f"{TEST_SESSION_ID}_{state_id}_{embedding_model}"
        payload = [
            {
                "SessionId": TEST_SESSION_ID,
                "SpanId": state_id,
                "EmbeddingModel": embedding_model,
                "Embedding": [0.11, 0.22, 0.33],
            }
        ]

        scope_api_ingest_embeddings(
            db=connector,
            embeddings=payload,
        )

        node_rows = connector.execute(
            """
            MATCH (e:Embedding {id: $embedding_id})
            RETURN e.id AS id, e.embeddingModel AS embeddingModel, e.embeddingVector AS embeddingVector
            """,
            {"embedding_id": created_embedding_id},
        )
        assert len(node_rows) == 1
        assert node_rows[0]["id"] == created_embedding_id
        assert node_rows[0]["embeddingModel"] == embedding_model
        assert node_rows[0]["embeddingVector"] == [0.11, 0.22, 0.33]

        rel_rows = connector.execute(
            """
            MATCH (:State {sessionId: $session_id, id: $state_id})<-[:represents]-(e:Embedding {id: $embedding_id})
            RETURN count(e) AS rel_count
            """,
            {
                "session_id": TEST_SESSION_ID,
                "state_id": state_id,
                "embedding_id": created_embedding_id,
            },
        )
        assert rel_rows[0]["rel_count"] == 1
    finally:
        if created_embedding_id:
            connector.execute_command(
                "MATCH (e:Embedding {id: $embedding_id}) DETACH DELETE e",
                {"embedding_id": created_embedding_id},
            )
        connector.close()
