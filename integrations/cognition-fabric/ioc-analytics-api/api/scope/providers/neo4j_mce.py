"""neo4j_mce — backward-compat re-export.

``Neo4jMCEProvider`` has been merged into ``OXPMetricsProvider``
(``scope.providers``), which now implements all three canonical interfaces:
``MetricsProvider``, ``DataProvider``, and ``KGProvider``.

This module is kept exclusively so that existing imports of the form::

    from scope.providers.neo4j_mce import Neo4jMCEProvider

continue to work without modification.  Do not add new code here.
"""

from scope.providers import OXPMetricsProvider as Neo4jMCEProvider  # noqa: F401

__all__ = ["Neo4jMCEProvider"]
