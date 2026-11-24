"""Shared types for query-builder modules."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Tuple

QueryAndParams = Tuple[str, Dict[str, Any]]
QueryPair = Tuple[str, Dict[str, Any]]


class Dialect(str, Enum):
    """Supported SQL dialects."""

    CLICKHOUSE = "clickhouse"
    SQLITE = "sqlite"
    SQLALCHEMY = "sqlalchemy"
    NEO4J = "neo4j"
