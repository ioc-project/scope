#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import json
import shutil

from conftest import FIXTURES, by_name

from harness_tap.codex_rollout import parse, parse_ts, reconcile, reconcile_all
from harness_tap.hooks import CODEX, HookTap

ENV = {"HARNESS_TAP_NO_BACKGROUND": "1"}


def test_parse_reads_observed_fields():
    rollout = parse(FIXTURES / "codex_rollout.jsonl")
    assert rollout.session == "sess-1"
    assert rollout.meta["originator"] == "codex_exec"
    assert list(rollout.turns) == ["turn-a", "turn-b"]
    turn = rollout.turns["turn-a"]
    assert turn["native_trace_id"] == "390cc4405dda786b89443806dbaaf970"
    assert turn["sandbox_policy"] == "read-only"
    assert turn["time_to_first_token_ms"] == 900
    assert [r["response_id"] for r in rollout.requests] == ["resp_1", "resp_2"]
    assert rollout.tools["call_1"]["output"] == "Fri Nov 20"
    assert rollout.counts["malformed_line"] == 1
    assert rollout.counts["skipped:world_state/-"] == 1


def test_parse_ts_scales():
    assert parse_ts(1795611001) == parse_ts(1795611001000) == 1795611001 * 10**9
    assert parse_ts("2026-11-20T14:50:00.1Z") == parse_ts("2026-11-20T14:50:00.100000+00:00")
    assert parse_ts("garbage") is None and parse_ts(True) is None


def test_reconcile_emits_completed_turns_once(cfg, tmp_path):
    path = tmp_path / "rollout.jsonl"
    shutil.copy(FIXTURES / "codex_rollout.jsonl", path)
    spans = reconcile(cfg, path, env=ENV)
    (anchor,) = by_name(spans, "ioc.session")
    (turn,) = by_name(spans, "invoke_agent")
    chats = by_name(spans, "chat")
    (tool,) = by_name(spans, "execute_tool")
    assert turn.attributes["turn.id"] == "turn-a"  # turn-b has not completed yet
    assert turn.attributes["ioc.codex.trace_id"] == "390cc4405dda786b89443806dbaaf970"
    assert turn.attributes["gen_ai.request.model"] == "gpt-6-luna"
    assert [c.attributes["gen_ai.response.id"] for c in chats] == ["resp_1", "resp_2"]
    assert [c.attributes["request_id"] for c in chats] == ["resp_1", "resp_2"]
    assert chats[0].attributes["gen_ai.usage.input_tokens"] == 100
    assert chats[0].attributes["ioc.usage.reasoning_output_tokens"] == 2
    assert chats[0].attributes["gen_ai.provider.name"] == "openai"
    assert {c.parent_span_id for c in chats} == {tool.parent_span_id} == {turn.span_id}
    assert turn.parent_span_id == anchor.span_id
    assert tool.end_ns - tool.start_ns == 300_000_000

    assert reconcile(cfg, path, env=ENV) == []  # nothing new

    with open(path, "a") as out:
        out.write(
            json.dumps(
                {
                    "timestamp": "2026-11-20T14:51:09Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-b"},
                }
            )
            + "\n"
        )
    (late,) = reconcile(cfg, path, env=ENV)
    assert late.attributes["turn.id"] == "turn-b"


def test_reconcile_merges_hook_timing_and_skips_hooked_tools(cfg, clock, tmp_path):
    path = tmp_path / "rollout.jsonl"
    shutil.copy(FIXTURES / "codex_rollout.jsonl", path)
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    hooked = []
    for event, extra in (
        ("UserPromptSubmit", {}),
        ("PreModelRequest", {}),
        ("PreToolUse", {"tool_name": "exec", "tool_use_id": "call_1"}),
        ("PostToolUse", {"tool_name": "exec", "tool_use_id": "call_1"}),
        ("PostModelRequest", {}),
        ("PreModelRequest", {}),
        ("PostModelRequest", {}),
        ("Stop", {}),
    ):
        hooked += tap.handle(
            CODEX,
            {
                "hook_event_name": event,
                "session_id": "sess-1",
                "turn_id": "turn-a",
                "transcript_path": str(path),
                **extra,
            },
        )
    assert [s.name for s in hooked] == ["ioc.session codex", "execute_tool exec"]

    spans = reconcile(cfg, path, session="sess-1", env=ENV)
    assert not by_name(spans, "execute_tool")  # already spooled by the hook
    assert not by_name(spans, "ioc.session")
    (turn,) = by_name(spans, "invoke_agent")
    chats = by_name(spans, "chat")
    assert turn.attributes["ioc.tap.source"] == "rollout+hook"
    assert all(c.attributes["ioc.timing.source"] == "hook" for c in chats)
    assert all(c.end_ns > c.start_ns for c in chats)
    assert turn.start_ns == clock.now - 7 * 100_000_000  # UserPromptSubmit time


def test_reconcile_all_backfills_a_directory(cfg, tmp_path):
    root = tmp_path / "sessions" / "2026" / "11" / "20"
    root.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex_rollout.jsonl", root / "rollout-2026-11-20T14-50-00-sess.jsonl")
    summary = reconcile_all(cfg, tmp_path / "sessions")
    assert summary["files"] == 1 and summary["spans"] == 5
