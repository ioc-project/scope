#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""SCOPE public library interface.

Exports are loaded lazily so submodule imports do not eagerly pull optional
runtime dependencies.
"""

from typing import Any

__all__ = ["SCOPEAPIClient", "Filters", "LocalClient"]


def __getattr__(name: str) -> Any:
    if name == "SCOPEAPIClient":
        from scope.client.base import SCOPEAPIClient

        return SCOPEAPIClient
    if name == "LocalClient":
        from scope.client.local import LocalClient

        return LocalClient
    if name == "Filters":
        from scope.models.otel_traces import Filters

        return Filters
    raise AttributeError(f"module 'scope' has no attribute {name!r}")
