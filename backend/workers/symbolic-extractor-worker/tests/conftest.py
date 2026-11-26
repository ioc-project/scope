"""Pytest setup: tests marked ``live`` call a real LLM and only run with ``--live``."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Also run tests that call a real LLM (credentials are read from .env)",
    )
    parser.addoption(
        "--sessions",
        default=None,
        help="Comma-separated session ids for the functional test (test_functional_traces.py); "
        "default: the ids of tests/functional_sessions.txt",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs a real LLM; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
