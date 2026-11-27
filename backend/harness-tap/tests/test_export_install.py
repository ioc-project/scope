#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import FIXTURES

from harness_tap import cli, install, otlp
from harness_tap.codex_rollout import reconcile
from harness_tap.spool import Spool

ENV = {"HARNESS_TAP_NO_BACKGROUND": "1"}


def test_otlp_json_shape(cfg):
    cfg.resource["ioc.agent.id"] = "planner"
    spans = reconcile(cfg, FIXTURES / "codex_rollout.jsonl", env=ENV)
    body = otlp.export_request(cfg, spans)
    (resource,) = body["resourceSpans"]
    attrs = {a["key"]: a["value"] for a in resource["resource"]["attributes"]}
    assert attrs["service.name"] == {"stringValue": "harness-tap"}
    assert attrs["ioc.agent.harness"] == {"stringValue": "codex"}
    assert attrs["ioc.agent.id"] == {"stringValue": "planner"}
    out = resource["scopeSpans"][0]["spans"]
    anchor = next(s for s in out if s["name"] == "ioc.session codex")
    assert "parentSpanId" not in anchor
    chat = next(s for s in out if s["name"].startswith("chat"))
    tokens = {a["key"]: a["value"] for a in chat["attributes"]}
    assert tokens["gen_ai.usage.input_tokens"] == {"intValue": "100"}
    assert chat["kind"] == 3 and len(chat["traceId"]) == 32 and len(chat["spanId"]) == 16


def test_clickhouse_rows_match_norm_input_shape(cfg):
    spans = reconcile(cfg, FIXTURES / "codex_rollout.jsonl", env=ENV)
    rows = otlp.clickhouse_rows(cfg, spans)
    chat = next(r for r in rows if r["SpanName"].startswith("chat"))
    assert set(chat) >= {
        "Timestamp",
        "TraceId",
        "SpanId",
        "ParentSpanId",
        "SpanName",
        "ServiceName",
        "ResourceAttributes",
        "SpanAttributes",
        "Duration",
    }
    assert chat["SpanAttributes"]["gen_ai.usage.input_tokens"] == "100"
    assert len(chat["Timestamp"]) == len("2026-11-20 14:50:02.400000000")


class _Collector(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        type(self).received.append((self.path, json.loads(self.rfile.read(length))))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_flush_posts_and_retries_on_failure(cfg):
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg.endpoint = f"http://127.0.0.1:{server.server_port}"
        reconcile(cfg, FIXTURES / "codex_rollout.jsonl", env=ENV)
        _Collector.status = 503
        status = cli._flush(cfg)
        assert status["failed"] == 5 and status["sent"] == 0
        assert len(Spool(cfg).all_spans()) == 5  # kept for retry
        _Collector.status = 200
        status = cli._flush(cfg)
        assert status["sent"] == 5 and Spool(cfg).all_spans() == []
        path, body = _Collector.received[-1]
        assert path == "/v1/traces" and body["resourceSpans"]
    finally:
        server.shutdown()


def test_install_merge_is_idempotent_and_preserves_user_hooks():
    user = {
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say hi"}]}]},
    }
    command = install.hook_command("codex", python="/usr/bin/python3")
    once = install.merged(user, "codex", command)
    twice = install.merged(once, "codex", command)
    assert once == twice
    assert once["model"] == "x"
    assert once["hooks"]["Stop"][0]["hooks"][0]["command"] == "say hi"
    ours = once["hooks"]["Stop"][1]["hooks"][0]
    assert install.MARKER in ours["command"] and ours["async"] is True
    assert once["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3
    assert "PostModelRequest" in once["hooks"]
    assert install.removed(once) == user


def test_install_claude_matchers_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    install.install("claude-code", "user")
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert "matcher" not in data["hooks"]["Stop"][0]
    assert install.installed("claude-code", "user") == sorted(install.CLAUDE_EVENTS)
    assert (settings.parent / "settings.json.harness-tap.bak").exists()
    install.uninstall("claude-code", "user")
    assert json.loads(settings.read_text()) == {"permissions": {"allow": ["Bash(ls:*)"]}}
