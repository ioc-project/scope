# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

import json

import httpx
import pytest

from scope_client import cli


class Transport(list):
    """Captured requests; ``respond`` sets the handler producing responses."""

    def __init__(self):
        super().__init__()
        self.respond = lambda request: httpx.Response(200, json={"ok": True})


@pytest.fixture
def transport(monkeypatch):
    """Route all CLI requests through an httpx.MockTransport."""
    seen = Transport()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return seen.respond(request)

    real_make_client = cli._make_client

    def make_client(args):
        client = real_make_client(args)
        client._httpx_args = {"transport": httpx.MockTransport(handler)}
        return client

    monkeypatch.setattr(cli, "_make_client", make_client)
    return seen


def test_health_ok(transport, capsys):
    assert cli.main(["--base-url", "http://x", "health"]) == 0
    assert transport[0].url.path == "/healthz"
    assert '"ok": true' in capsys.readouterr().out


def test_health_v1(transport):
    assert cli.main(["--base-url", "http://x", "health", "--v1"]) == 0
    assert transport[0].url.path == "/api/v1/healthz"


def test_token_sets_auth_header(transport):
    transport.respond = lambda r: httpx.Response(200, json={"metrics": []})
    cli.main(["--base-url", "http://x", "--token", "abc", "metrics-catalog"])
    assert transport[0].headers["authorization"] == "Bearer abc"


def test_env_base_url(monkeypatch, transport):
    monkeypatch.setenv("OXP_BASE_URL", "http://from-env")
    cli.main(["features-info"])
    assert transport[0].url.host == "from-env"


def test_non_2xx_exits_1(transport):
    transport.respond = lambda r: httpx.Response(500, text="boom")
    assert cli.main(["--base-url", "http://x", "health"]) == 1


def test_expect_status(transport):
    transport.respond = lambda r: httpx.Response(404)
    assert cli.main(["--base-url", "http://x", "--expect-status", "404", "health"]) == 0


def test_expect_json_key(transport):
    assert cli.main(["--base-url", "http://x", "--expect-json-key", "ok", "health"]) == 0
    assert cli.main(["--base-url", "http://x", "--expect-json-key", "nope", "health"]) == 1


def test_connection_error_exits_3(transport):
    def boom(request):
        raise httpx.ConnectError("refused")

    transport.respond = boom
    assert cli.main(["--base-url", "http://x", "health"]) == 3


def test_retries_until_success(transport):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={})

    transport.respond = flaky
    assert cli.main(["--base-url", "http://x", "--retries", "3", "--retry-delay", "0", "health"]) == 0
    assert calls["n"] == 3


def test_metrics_compute_body(transport):
    cli.main(
        ["--base-url", "http://x", "metrics-compute", "--session-id", "s1", "--session-id", "s2", "--metric", "Cost"]
    )
    request = transport[0]
    assert request.method == "POST"
    assert request.read() == b'{"session_ids":["s1","s2"],"metric_ids":["Cost"],"recursive":false}'


def test_schema_mismatch_exits_1(transport):
    # {"ok": true} is not a valid MetricCatalogResponse/SessionSpansResponse
    assert cli.main(["--base-url", "http://x", "session-spans", "abc"]) == 1


def test_session_spans_path_and_query(transport):
    cli.main(["--base-url", "http://x", "session-spans", "abc", "--limit", "5"])
    assert transport[0].url.path == "/api/v1/sessions/abc/spans"
    assert transport[0].url.params["limit"] == "5"


def test_usage_error_exits_2():
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


CE_ID = "11111111-1111-1111-1111-111111111111"
SUB_ID = "22222222-2222-2222-2222-222222222222"


def test_store_put_body(transport):
    cli.main(
        [
            "--base-url",
            "http://x",
            "store-put",
            "--ce-id",
            CE_ID,
            "--namespace",
            "ns",
            "--key",
            "k",
            "--value",
            '{"a":1}',
            "--type",
            "t",
            "--session-id",
            "s1",
        ]
    )
    request = transport[0]
    assert (request.method, request.url.path) == ("POST", "/api/v1/cognition-store/store")
    assert json.loads(request.read()) == {
        "ce_id": CE_ID,
        "namespace": "ns",
        "key": "k",
        "value": {"a": 1},
        "type": "t",
        "metadata": {"session_id": "s1"},
    }


def test_store_list_query(transport):
    cli.main(
        ["--base-url", "http://x", "store-list", "--namespace", "ns", "--span-id", "sp", "--type", "t", "--tag", "g"]
    )
    params = transport[0].url.params
    assert transport[0].url.path == "/api/v1/cognition-store/store"
    assert (params["namespace"], params["metadata.span_id"], params["type"], params["metadata.tag"]) == (
        "ns",
        "sp",
        "t",
        "g",
    )


def test_subscribe_body(transport):
    cli.main(
        [
            "--base-url",
            "http://x",
            "subscribe",
            "--namespace",
            "ns",
            "--type",
            "t",
            "--callback-url",
            "http://cb/hook",
            "--trace-id",
            "tr",
        ]
    )
    request = transport[0]
    assert (request.method, request.url.path) == ("POST", "/api/v1/cognition-store/subscriptions")
    assert json.loads(request.read()) == {
        "namespace": "ns",
        "type": "t",
        "callback_url": "http://cb/hook",
        "filter": {"metadata.trace_id": "tr"},
    }


def test_unsubscribe_delete(transport):
    transport.respond = lambda request: httpx.Response(204)
    assert cli.main(["--base-url", "http://x", "unsubscribe", SUB_ID]) == 0
    assert (transport[0].method, transport[0].url.path) == (
        "DELETE",
        f"/api/v1/cognition-store/subscriptions/{SUB_ID}",
    )
