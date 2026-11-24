"""OXP public library interface.

Exports are loaded lazily so submodule imports do not eagerly pull optional
runtime dependencies.
"""

from typing import Any

__all__ = ["OXPAPIClient", "Filters", "LocalClient"]


def __getattr__(name: str) -> Any:
    if name == "OXPAPIClient":
        from scope.client.base import OXPAPIClient

        return OXPAPIClient
    if name == "LocalClient":
        from scope.client.local import LocalClient

        return LocalClient
    if name == "Filters":
        from scope.models.otel_traces import Filters

        return Filters
    raise AttributeError(f"module 'oxp' has no attribute {name!r}")
