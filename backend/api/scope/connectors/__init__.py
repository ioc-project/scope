#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""SCOPE connectors — database connection abstractions."""

from scope.connectors.base import Connector
from scope.connectors.clickhouse import ClickHouseConnector
from scope.connectors.sqlalchemy import SQLAlchemyConnector

__all__ = [
    "Connector",
    "ClickHouseConnector",
    "SQLAlchemyConnector",
]
