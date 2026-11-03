#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The UMAP projection is an optional extra of the API: without it the endpoint says so instead of failing."""

from __future__ import annotations

import sys

import pytest
from fastapi import HTTPException

from scope.client import sessions


def test_latent_space_without_umap_answers_501(monkeypatch):
    monkeypatch.setitem(sys.modules, "umap", None)  # importing it raises ImportError, as when it is not installed
    with pytest.raises(HTTPException) as raised:
        sessions._get_session_latent_space(None, None, session_id="s1")
    assert raised.value.status_code == 501
    assert "ioc-scope-api[latent-space]" in raised.value.detail
