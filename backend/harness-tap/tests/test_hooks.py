#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
import io
import json

from conftest import TRACEPARENT, by_name

from harness_tap import cli
from harness_tap.hooks import CLAUDE, CODEX, HookTap
from harness_tap.spool import Spool

ENV = {"HARNESS_TAP_NO_BACKGROUND": "1"}


def codex(event, **fields):
    return {
        "hook_event_name": event,
        "session_id": "sess-1",
        "cwd": "/work",
        "model": "gpt-6-luna",
        **fields,
    }


def test_codex_hooks_without_rollout_emit_turn_tool_and_model_spans(cfg, clock):
    tap = HookTap(cfg, env={**ENV, "TRACEPARENT": TRACEPARENT}, now_ns=clock)
    spans = []
    for payload in (
        codex("SessionStart", source="startup"),
        codex("UserPromptSubmit", turn_id="t1", prompt="what time is it"),
        codex("PreModelRequest", turn_id="t1", agent_id="/root"),
        codex(
            "PreToolUse",
            turn_id="t1",
            tool_name="exec",
            tool_use_id="call_1",
            tool_input={"cmd": "date"},
        ),
        codex(
            "PostToolUse",
            turn_id="t1",
            tool_name="exec",
            tool_use_id="call_1",
            tool_input={"cmd": "date"},
            tool_response={"output": "Fri"},
        ),
        codex(
            "PostModelRequest",
            turn_id="t1",
            status="completed",
            token_usage={"input_tokens": 50, "cached_input_tokens": 40, "output_tokens": 7},
        ),
        codex("Stop", turn_id="t1", last_assistant_message="It is Friday."),
    ):
        spans += tap.handle(CODEX, payload)

    (anchor,) = by_name(spans, "ioc.session")
    assert (anchor.trace_id, anchor.parent_span_id) == (TRACEPARENT[3:35], TRACEPARENT[36:52])
    assert anchor.attributes["ioc.traceparent.observed"] is True
    (turn,) = by_name(spans, "invoke_agent")
    (tool,) = by_name(spans, "execute_tool")
    (chat,) = by_name(spans, "chat")
    assert turn.parent_span_id == anchor.span_id
    assert tool.parent_span_id == chat.parent_span_id == turn.span_id
    assert {s.trace_id for s in spans} == {anchor.trace_id}
    assert tool.end_ns - tool.start_ns == 100_000_000
    assert turn.attributes["turn.id"] == "t1"
    assert turn.attributes["conversation.id"] == "sess-1"
    assert turn.attributes["ioc.prompt_length"] == len("what time is it")
    assert chat.attributes["gen_ai.usage.cache_read.input_tokens"] == 40
    assert tool.attributes["call_id"] == "call_1"
    # content capture is off by default
    assert "gen_ai.tool.call.arguments" not in tool.attributes
    assert "gen_ai.input.messages" not in turn.attributes


def test_codex_stop_with_rollout_defers_to_reconciler(cfg, clock, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("")
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    tap.handle(CODEX, codex("UserPromptSubmit", turn_id="t1", transcript_path=str(rollout)))
    spans = tap.handle(CODEX, codex("Stop", turn_id="t1", transcript_path=str(rollout)))
    assert spans == []  # anchor was spooled on the first event
    state = Spool(cfg).read_session(CODEX, "sess-1")
    assert state["turns"]["t1"]["pending_reconcile"] is True


def test_claude_anchor_mode_skips_tools_and_numbers_turns(cfg, clock):
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    base = {"session_id": "claude-s", "transcript_path": "/nope.jsonl"}
    spans = []
    for event, extra in (
        ("SessionStart", {}),
        ("UserPromptSubmit", {"prompt": "hi"}),
        ("PreToolUse", {"tool_name": "Bash", "tool_use_id": "toolu_1"}),
        ("PostToolUse", {"tool_name": "Bash", "tool_use_id": "toolu_1"}),
        ("Stop", {}),
        ("UserPromptSubmit", {"prompt": "again"}),
        ("Stop", {}),
    ):
        spans += tap.handle(CLAUDE, {**base, "hook_event_name": event, **extra})
    turns = by_name(spans, "invoke_agent")
    assert [t.attributes["ioc.turn.id"] for t in turns] == ["turn-1", "turn-2"]
    assert turns[0].attributes["session.id"] == "claude-s"
    assert not by_name(spans, "execute_tool")


def test_claude_full_mode_with_content_capture(cfg, clock):
    cfg.claude_mode, cfg.capture_content, cfg.content_max_chars = "full", True, 5
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    base = {"session_id": "claude-s"}
    tap.handle(CLAUDE, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "hello world"})
    (tool,) = tap.handle(
        CLAUDE,
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__github__search",
            "tool_use_id": "toolu_9",
            "tool_input": "abcdefgh",
            "tool_response": {"is_error": True},
        },
    )
    assert tool.attributes["gen_ai.tool.call.arguments"] == "abcde…[truncated]"
    assert tool.attributes["ioc.mcp.server"] == "github"
    assert tool.attributes["ioc.timing.source"] == "hook_post_only"
    assert tool.status == "error"


def test_subagent_and_interrupt(cfg, clock):
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    tap.handle(CODEX, codex("UserPromptSubmit", turn_id="t1"))
    tap.handle(CODEX, codex("SubagentStart", turn_id="t1", agent_id="a1", agent_type="worker"))
    (sub,) = tap.handle(CODEX, codex("SubagentStop", turn_id="t1", agent_id="a1"))
    assert sub.attributes["gen_ai.agent.id"] == "a1"
    assert sub.name == "invoke_agent worker"
    spans = tap.handle(CODEX, codex("Interrupt", turn_id="t1"))
    (turn,) = by_name(spans, "invoke_agent")
    assert (turn.status, turn.status_message) == ("error", "interrupted")
    assert sub.parent_span_id == turn.span_id


def test_events_are_idempotent_and_garbage_is_ignored(cfg, clock):
    tap = HookTap(cfg, env=ENV, now_ns=clock)
    assert tap.handle(CODEX, {"hook_event_name": "Stop"}) == []  # no session
    assert tap.handle("other", codex("Stop")) == []
    assert tap.handle(CODEX, ["not", "a", "dict"]) == []
    first = tap.handle(CODEX, codex("SessionStart"))
    assert len(first) == 1 and tap.handle(CODEX, codex("SessionStart")) == []


def test_cli_hook_fails_open(cfg, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_TAP_HOME", str(cfg.home))
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["hook", "--harness", "codex"]) == 0
    assert capsys.readouterr().out == ""
    assert "JSONDecodeError" in (cfg.home / "state" / "hook-errors.log").read_text()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(codex("SessionStart"))))
    assert cli.main(["hook", "--harness", "codex"]) == 0
    assert capsys.readouterr().out == ""
    assert len(Spool(cfg).all_spans()) == 1
