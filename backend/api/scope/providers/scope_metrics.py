#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Concrete :class:`MetricsProvider` backed by the existing SCOPE connector + query builders."""

from scope.providers import SCOPEMetricsProvider

__all__ = ["SCOPEMetricsProvider"]
