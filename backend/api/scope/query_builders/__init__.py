#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""SCOPE query builders — one module per feature domain.

Example::

    from scope.query_builders import ui

    stmt = ui.sessions_query(app_name="my-app")
"""

from scope.query_builders.types import Dialect, QueryAndParams

__all__ = ["Dialect", "QueryAndParams"]
