"""Shared helpers for endpoint modules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from scope.client.base import OXPAPIClient
from scope.connectors.base import Connector as _Connector
from scope.connectors.neo4j import Neo4JConnector
from scope.dependencies import get_db, get_neo4j_connector
from scope.models.otel_traces import Filters

if TYPE_CHECKING:
    from scope.interfaces.metrics_provider import MetricsProvider


def build_filters(request: Request) -> Filters:
    """Parse query-string parameters into a :class:`Filters` instance."""
    params = dict(request.query_params)
    return Filters(**{k: v for k, v in params.items() if k in Filters.model_fields})


# ── Client registry (one LocalClient per connector) ─────────────────────────

_clients: dict[int, "OXPAPIClient"] = {}


def get_client(
    api_client: _Connector | None = None,
    metrics_provider: MetricsProvider | None = None,
) -> OXPAPIClient:
    """Return (and lazily create) a :class:`LocalClient` for *api_client*.

    Each distinct connector instance gets its own ``LocalClient`` so that
    SQL-backed and Neo4j-backed endpoints don't share a single client.

    Parameters
    ----------
    api_client:
        Database connector. *None* uses the API's shared ClickHouse connector,
        independently of the library repository's ``OXP_LOCAL`` setting.
    metrics_provider:
        Optional :class:`MetricsProvider` to inject into the client.
        Otherwise, metrics use the shared Neo4j connector, reusing *api_client*
        when it is already a Neo4j connector.
    """
    if api_client is None:
        api_client = next(get_db())
    key = id(api_client)
    if key not in _clients:
        from scope.client.local import LocalClient

        if metrics_provider is None:
            from scope.providers import OXPMetricsProvider

            metrics_db = (
                api_client
                if isinstance(api_client, Neo4JConnector)
                else get_neo4j_connector()
            )
            metrics_provider = OXPMetricsProvider(db=metrics_db)
        _clients[key] = LocalClient(db=api_client, metrics_provider=metrics_provider)
    else:
        # Update provider on an existing client if a new one is supplied
        if metrics_provider is not None:
            _clients[key].metrics_provider = metrics_provider  # type: ignore[attr-defined]
    return _clients[key]


def reset_client() -> None:
    """Force re-creation of all shared clients (used by tests)."""
    _clients.clear()
