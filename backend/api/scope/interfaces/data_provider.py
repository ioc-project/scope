#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""DataProvider — scope-api's own KG data-fetch interface.

MCE defines a minimal ``DataProvider`` Protocol in ``mce.core.provider``
(structural subtyping — ``fetch`` + ``fetch_batch`` only).  That Protocol
is what the ``MetricEngine`` uses internally; **scope-api does not need
to import or extend it** — ``SCOPEKGProvider`` satisfies it structurally.

This module defines the *scope-api* ABC for KG data providers with the
additional ``resolve_session_id`` method that is a KG-specific concept
and does not belong in MCE.

Dependency direction::

    mce-core     → DataProvider Protocol (fetch only, no KG concepts)
    scope-api   → DataProvider ABC (fetch + resolve_session_id)  ← this file
    SCOPEKGProvider implements this ABC
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from scope.interfaces.models import RetrievalRequest


class DataProvider(ABC):
    """scope-api abstract interface for KG execution-data providers.

    Satisfies ``mce.core.provider.DataProvider`` structurally (duck typing)
    so ``MetricEngine`` can use it without any import of scope-api.

    The long-term contract is the faceted retrieval protocol exposed by
    :meth:`retrieve`. The legacy :meth:`fetch` entry point remains for current
    engine compatibility and single-anchor workflows.
    """

    @abstractmethod
    def resolve_session_id(self, resource_id: str) -> str | None:
        """Resolve an arbitrary resource ID to its parent Session ID."""

    @abstractmethod
    def retrieve(self, request: RetrievalRequest) -> dict[str, Any]:
        """Retrieve KG data from a faceted, query-language-agnostic request.

        Implementations should translate the request into as few backend
        operations as possible, ideally a single request when supported.
        """

    @abstractmethod
    def fetch(self, resource_id: str, requirements: Any) -> dict[str, Any]:
        """Fetch metric data for a single anchor resource.

        Implementations may translate metric requirements into a
        :class:`RetrievalRequest` and delegate to :meth:`retrieve`.
        """


__all__ = ["DataProvider"]
