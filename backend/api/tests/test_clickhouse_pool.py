#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The ClickHouse client keeps enough connections for the threads of a busy worker."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import clickhouse_connect
import pytest
from clickhouse_connect.driver import httputil

from scope import dependencies
from scope.connectors.clickhouse import DEFAULT_POOL_SIZE, ClickHouseConnector


@pytest.fixture
def client_kwargs(monkeypatch):
    seen: dict = {}

    def fake_get_client(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    return seen


def _pool_size(kwargs: dict) -> int:
    return kwargs["pool_mgr"].connection_pool_kw["maxsize"]


def test_the_connector_gets_a_pool_of_the_size_it_is_given(client_kwargs) -> None:
    ClickHouseConnector(pool_size=12).connect()

    assert _pool_size(client_kwargs) == 12


def test_the_default_pool_is_larger_than_the_one_of_the_client_library(
    client_kwargs,
) -> None:
    ClickHouseConnector().connect()

    assert (
        _pool_size(client_kwargs)
        == DEFAULT_POOL_SIZE
        > httputil.default_pool_manager().connection_pool_kw["maxsize"]
    )


def test_the_pool_size_is_set_by_the_environment(monkeypatch) -> None:
    built: dict = {}

    class FakeConnector:
        def __init__(self, **kwargs):
            built.update(kwargs)

        def connect(self):
            pass

    monkeypatch.setattr(dependencies, "ClickHouseConnector", FakeConnector)
    monkeypatch.setattr(dependencies, "_db_connector", None)
    monkeypatch.setenv("CLICKHOUSE_POOL_SIZE", "48")

    dependencies._get_db_singleton()

    assert built["pool_size"] == 48
    monkeypatch.setattr(dependencies, "_db_connector", None)


# ---------------------------------------------------------------- what the size changes


class _Slow(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(0.3)  # long enough for all the requests to be in flight together
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        pass


class _Server(ThreadingHTTPServer):
    request_queue_size = 128  # the default backlog resets a burst of connections
    daemon_threads = True


def _discarded_connections(maxsize: int, requests: int, caplog) -> int:
    server = _Server(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manager = httputil.get_pool_manager(maxsize=maxsize)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="urllib3.connectionpool"):
        with ThreadPoolExecutor(max_workers=requests) as pool:
            list(
                pool.map(lambda _: manager.request("GET", url).status, range(requests))
            )
    server.shutdown()
    return sum(
        "Connection pool is full" in record.getMessage() for record in caplog.records
    )


def test_more_requests_than_connections_discards_connections(caplog) -> None:
    assert _discarded_connections(maxsize=4, requests=16, caplog=caplog) > 0


def test_a_pool_as_large_as_the_requests_discards_none(caplog) -> None:
    assert _discarded_connections(maxsize=16, requests=16, caplog=caplog) == 0
