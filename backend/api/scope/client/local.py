#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Local (in-process, DB-backed) implementation of :class:`SCOPEAPIClient`.

Usage::

    from scope.client import LocalClient

    with LocalClient.from_settings() as client:
        sessions = client.list_sessions(limit=10)
        apps     = client.get_applications()
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Optional

from scope.client._concepts import ConceptsClient
from scope.client._kg import KGClient
from scope.client._labels import LabelsClient
from scope.client._live_topology import LiveTopologyClient
from scope.client._metrics import MetricsClient
from scope.client._semanticgroups import SemanticGroupsClient
from scope.client._sessions import SessionsClient
from scope.client._symbolic import SymbolicClient
from scope.client._ui import UIClient
from scope.client.base import SCOPEAPIClient
from scope.connectors.base import Connector
from scope.interfaces.metrics_provider import MetricsProvider
from scope.query_builders.types import Dialect


class LocalClient(
    UIClient,
    KGClient,
    SessionsClient,
    MetricsClient,
    ConceptsClient,
    LabelsClient,
    SemanticGroupsClient,
    SymbolicClient,
    LiveTopologyClient,
    SCOPEAPIClient,
):
    """Concrete client that queries a local database via :class:`Connector`.

    Domain-specific operations are provided by sub-clients:

    * :class:`~scope.client._ui.UIClient` — UI operations
    * :class:`~scope.client._kg.KGClient` — KG (knowledge-graph) operations

    Internally follows a three-step pipeline for every operation:

    1. **Query Builder** → ``scope.query_builders.<domain>`` → SQLAlchemy ``Select``
    2. **Connector**     → ``self.db.execute(stmt)`` → raw rows
    3. **Model mapping** → Pydantic response models → typed result

    Parameters
    ----------
    db:
        An explicit connector instance.  When *None* the factory in
        ``scope.repositories`` picks the right backend based on
        the ``SCOPE_LOCAL`` environment variable.
    metrics_provider:
        An optional :class:`MetricsProvider` instance.  When given, all
        metric operations delegate to this provider. When omitted, a
        SCOPEMetricsProvider is created using the supplied connector.
    """

    def __init__(
        self,
        db: Optional[Connector] = None,
        metrics_provider: Optional[MetricsProvider] = None,
    ) -> None:
        self._owned_resources = ExitStack()
        if db is not None:
            self.db = db
        else:
            from scope.repositories import get_otel_repository

            self.db = get_otel_repository()

        if metrics_provider is not None:
            self.metrics_provider = metrics_provider
        else:
            from scope.providers import SCOPEMetricsProvider

            self.metrics_provider = SCOPEMetricsProvider(db=self.db)

    @classmethod
    def from_settings(
        cls, *, persist_metrics: bool = False, neo4j: Optional[Connector]
    ) -> LocalClient:
        """Create a client with owned connections from API-library settings.

        Read spans from ClickHouse. When requested, persist metrics through the
        Neo4j provider. Use as a context manager or call close() after use.
        Explicitly injected connectors in LocalClient(db=...) remain caller-owned.
        Pass neo4j to reuse a shared connector, such as the one from
        scope.dependencies.get_neo4j_connector(), instead of opening a new one; it
        stays caller-owned and close() leaves it open.
        """
        from scope.connectors.clickhouse import ClickHouseConnector
        from scope.core.config import settings

        with ExitStack() as resources:
            db = ClickHouseConnector(
                host=settings.CLICKHOUSE_HOST,
                port=settings.CLICKHOUSE_PORT,
                username=settings.CLICKHOUSE_USERNAME,
                password=settings.CLICKHOUSE_PASSWORD,
                database=settings.CLICKHOUSE_DATABASE,
            )
            resources.callback(db.close)
            provider = None
            if persist_metrics:
                from scope.providers import SCOPEMetricsProvider

                kg = neo4j
                if kg is None:
                    from scope.connectors.neo4j import Neo4JConnector

                    kg = Neo4JConnector(
                        host=settings.NEO4J_HOST,
                        port=settings.NEO4J_PORT,
                        username=settings.NEO4J_USERNAME,
                        password=settings.NEO4J_PASSWORD,
                        database=settings.NEO4J_DATABASE,
                    )
                    resources.callback(kg.close)
                provider = SCOPEMetricsProvider(db=kg)
            client = cls(db=db, metrics_provider=provider)
            client._owned_resources = resources.pop_all()
            return client

    @property
    def _dialect(self) -> Dialect:
        """Infer the SQL dialect from the connector type."""
        from scope.connectors.neo4j import Neo4JConnector
        from scope.connectors.sqlalchemy import SQLAlchemyConnector

        if isinstance(self.db, Neo4JConnector):
            return Dialect.NEO4J
        if isinstance(self.db, SQLAlchemyConnector):
            url = getattr(self.db, "_url", "")
            if "sqlite" in str(url):
                return Dialect.SQLITE
            return Dialect.CLICKHOUSE
        return Dialect.CLICKHOUSE

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close only connections created by from_settings(); safe to call twice."""
        self._owned_resources.close()

    # ── common ────────────────────────────────────────────────────────────

    def info(self, domain: str = "scope") -> str:
        return f"Hello! This is the {domain.upper()} endpoint."
