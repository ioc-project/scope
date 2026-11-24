"""OXP client public exports.

Exports are loaded lazily so utility modules like ``scope.client.dal`` can be
imported without importing LocalClient and its optional dependencies.
"""

from typing import Any

__all__ = ["OXPAPIClient", "GoldenClient", "LocalClient"]


def __getattr__(name: str) -> Any:
    if name == "OXPAPIClient":
        from scope.client.base import OXPAPIClient

        return OXPAPIClient
    if name == "GoldenClient":
        from scope.client.mock import GoldenClient

        return GoldenClient
    if name == "LocalClient":
        from scope.client.local import LocalClient

        return LocalClient
    raise AttributeError(f"module 'scope.client' has no attribute {name!r}")
