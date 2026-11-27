#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Hook events -> spans.

Span hierarchy (after opentelemetry-hooks' session > generation > hook-event
layering, but with GenAI operation names so any OTel backend can read it):

    ioc.session <harness>          zero-length anchor; child of TRACEPARENT if one
                                   was inherited (e.g. from `agentbridge register`)
      invoke_agent <harness>       one per turn
        execute_tool <tool>        one per tool call
        chat <model>               one per model request (Codex)
        invoke_agent <subagent>    one per subagent

The anchor is spooled at the first event of a session, so a cross-harness hop
is linkable the moment the receiving agent starts, not when it ends.

For Codex, the rollout file is the authority for turns and model requests
(it carries response_id, turn_id and exact usage); hooks contribute exact
boundaries and tool timing. See codex_rollout.reconcile.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import ids
from .config import Config
from .spans import SpanRecord, content, messages, text_length, usage_attributes
from .spool import Spool

CLAUDE, CODEX = "claude-code", "codex"
HARNESSES = {CLAUDE, CODEX}
# Each harness's own session key, so norm's existing native joins line up.
NATIVE_SESSION_KEY = {CLAUDE: "session.id", CODEX: "conversation.id"}
NATIVE_TOOL_KEY = {CLAUDE: "tool_use_id", CODEX: "call_id"}


def _text(*values: Any) -> str:
    return next((v for v in values if isinstance(v, str) and v.strip()), "")


def base_attrs(harness: str, session: str, source: str) -> Dict[str, Any]:
    return {
        "gen_ai.conversation.id": session,
        NATIVE_SESSION_KEY[harness]: session,
        "ioc.agent.harness": harness,
        "ioc.tap.source": source,
    }


def session_context(
    spool: Spool,
    state: dict,
    harness: str,
    session: str,
    env: Mapping[str, str],
    now_ns: int,
    source: str,
) -> List[SpanRecord]:
    """Initialise trace context for a session once; return the anchor span if new."""
    if state.get("trace_id"):
        return []
    parent = ids.parse_traceparent(env.get("TRACEPARENT"))
    state["trace_id"] = parent[0] if parent else ids.trace_id("harness-tap", harness, session)
    state["parent"] = parent[1] if parent else ""
    state["anchor"] = ids.span_id(harness, session, "session")
    state["started_ns"] = now_ns
    attrs = base_attrs(harness, session, source)
    attrs["ioc.span.type"] = "session_anchor"
    attrs["ioc.traceparent.observed"] = bool(parent)
    return [
        SpanRecord(
            harness,
            state["trace_id"],
            state["anchor"],
            state["parent"],
            f"ioc.session {harness}",
            now_ns,
            now_ns,
            attrs,
        )
    ]


def turn_span_id(harness: str, session: str, turn: str) -> str:
    return ids.span_id(harness, session, "turn", turn)


def turn_span(
    cfg: Config,
    harness: str,
    session: str,
    state: dict,
    turn: str,
    start_ns: int,
    end_ns: int,
    source: str,
    extra: Optional[Dict[str, Any]] = None,
    parent: str = "",
    status: str = "unset",
    status_message: str = "",
) -> SpanRecord:
    info = state.get("turns", {}).get(turn, {})
    attrs = base_attrs(harness, session, source)
    attrs.update({"gen_ai.operation.name": "invoke_agent", "ioc.turn.id": turn})
    if harness == CODEX:
        attrs["turn.id"] = turn
    if info.get("model"):
        attrs["gen_ai.request.model"] = info["model"]
    for key in ("prompt_length", "response_length", "permission_requests"):
        if info.get(key) is not None:
            attrs[f"ioc.{key}"] = info[key]
    for key, role in (("prompt", "user"), ("response", "assistant")):
        if info.get(key) is not None:
            env = messages(cfg, role, info[key])
            if env:
                attrs["gen_ai.input.messages" if role == "user" else "gen_ai.output.messages"] = env
    attrs.update(extra or {})
    return SpanRecord(
        harness,
        state["trace_id"],
        turn_span_id(harness, session, turn),
        parent or state["anchor"],
        f"invoke_agent {harness}",
        start_ns,
        max(end_ns, start_ns),
        attrs,
        status=status,
        status_message=status_message,
    )


def model_spans_from_hooks(
    harness: str, session: str, state: dict, turn: str, source: str = "hook"
) -> List[SpanRecord]:
    spans = []
    for index, record in enumerate(state.get("models", {}).get(turn, [])):
        if "end_ns" not in record:
            continue
        attrs = base_attrs(harness, session, source)
        attrs.update(
            {"gen_ai.operation.name": "chat", "ioc.turn.id": turn, "turn.id": turn},
            **usage_attributes(record.get("usage")),
        )
        model = record.get("model") or state.get("turns", {}).get(turn, {}).get("model", "")
        if model:
            attrs["gen_ai.request.model"] = model
        if record.get("agent_id"):
            attrs["gen_ai.agent.id"] = record["agent_id"]
        failed = bool(record.get("error")) or record.get("status") not in (
            None,
            "",
            "ok",
            "completed",
        )
        spans.append(
            SpanRecord(
                harness,
                state["trace_id"],
                ids.span_id(harness, session, "chat", turn, f"hook-{index}"),
                turn_span_id(harness, session, turn),
                f"chat {model or 'unknown'}",
                record["start_ns"],
                record["end_ns"],
                attrs,
                kind="client",
                status="error" if failed else "unset",
                status_message=_text(record.get("error")) if failed else "",
            )
        )
    return spans


def _tool_status(response: Any) -> str:
    if isinstance(response, dict):
        if response.get("is_error") is True or response.get("success") is False:
            return "error"
        if response.get("error"):
            return "error"
    return "unset"


class HookTap:
    def __init__(self, cfg: Config, env: Optional[Mapping[str, str]] = None, now_ns=None):
        self.cfg = cfg
        self.spool = Spool(cfg)
        self.env = dict(os.environ if env is None else env)
        self._now = now_ns or time.time_ns

    def handle(self, harness: str, payload: Dict[str, Any]) -> List[SpanRecord]:
        """Process one hook payload; return the spans spooled for it."""
        if harness not in HARNESSES or not isinstance(payload, dict):
            return []
        event = _text(payload.get("hook_event_name"), payload.get("hookEventName"))
        session = _text(payload.get("session_id"), payload.get("sessionId"))
        if not event or not session:
            return []
        now = self._now()
        spans: List[SpanRecord] = []
        background = None
        with self.spool.session_state(harness, session) as state:
            spans += session_context(self.spool, state, harness, session, self.env, now, "hook")
            state.setdefault("turns", {})
            if payload.get("transcript_path"):
                state["transcript_path"] = payload["transcript_path"]
            if payload.get("cwd"):
                state["cwd"] = payload["cwd"]
            handler = getattr(self, f"_on_{event}", None)
            if handler is not None:
                result = handler(harness, session, state, payload, now)
                if isinstance(result, tuple):
                    result, background = result
                spans += result or []
            else:
                state["unhandled_events"] = state.get("unhandled_events", 0) + 1
        written = self.spool.append(spans)
        if background:
            spawn_background(background, self.env)
        return written

    # ---- turn bookkeeping -----------------------------------------------------
    def _turn(self, harness: str, state: dict, payload: dict, opening: bool = False) -> str:
        if harness == CODEX and _text(payload.get("turn_id")):
            return payload["turn_id"]
        # Claude hooks carry no turn id: number turns by prompt submissions.
        if opening:
            state["turn_seq"] = state.get("turn_seq", 0) + 1
        return f"turn-{state.get('turn_seq', 0)}"

    def _on_SessionStart(self, harness, session, state, payload, now):
        state["source"] = _text(payload.get("source"))
        return []

    def _on_UserPromptSubmit(self, harness, session, state, payload, now):
        turn = self._turn(harness, state, payload, opening=True)
        info = state["turns"].setdefault(turn, {})
        info.setdefault("start_ns", now)
        info["prompt_length"] = text_length(payload.get("prompt"))
        if self.cfg.capture_content:
            info["prompt"] = payload.get("prompt")
        if payload.get("model"):
            info["model"] = payload["model"]
        state["current_turn"] = turn
        return []

    def _on_PermissionRequest(self, harness, session, state, payload, now):
        info = state["turns"].setdefault(self._turn(harness, state, payload), {})
        info["permission_requests"] = info.get("permission_requests", 0) + 1
        return []

    # ---- tools -------------------------------------------------------------------
    def _on_PreToolUse(self, harness, session, state, payload, now):
        call = _text(payload.get("tool_use_id"))
        if call:
            state.setdefault("tools", {})[call] = {"start_ns": now}
        return []

    def _on_PostToolUse(self, harness, session, state, payload, now):
        if harness == CLAUDE and self.cfg.claude_mode != "full":
            state.get("tools", {}).pop(_text(payload.get("tool_use_id")), None)
            return []
        call = _text(payload.get("tool_use_id"))
        name = _text(payload.get("tool_name")) or "unknown"
        if not call:
            state["tools_without_id"] = state.get("tools_without_id", 0) + 1
            return []
        start = state.get("tools", {}).pop(call, {}).get("start_ns", now)
        turn = self._turn(harness, state, payload)
        attrs = base_attrs(harness, session, "hook")
        attrs.update(
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": call,
                NATIVE_TOOL_KEY[harness]: call,
                "ioc.turn.id": turn,
                "ioc.timing.source": "hook" if start != now else "hook_post_only",
            }
        )
        if name.startswith("mcp__") and name.count("__") >= 2:
            _, server, tool = name.split("__", 2)
            attrs.update({"ioc.mcp.server": server, "ioc.mcp.tool": tool})
        for attr, value in (
            ("gen_ai.tool.call.arguments", payload.get("tool_input")),
            ("gen_ai.tool.call.result", payload.get("tool_response")),
        ):
            text = content(self.cfg, value)
            if text is not None:
                attrs[attr] = text
        parent = turn_span_id(harness, session, turn)
        agent = _text(payload.get("agent_id"))
        if agent:
            attrs["gen_ai.agent.id"] = agent
            parent = ids.span_id(harness, session, "subagent", agent)
        return [
            SpanRecord(
                harness,
                state["trace_id"],
                ids.span_id(harness, session, "tool", call),
                parent,
                f"execute_tool {name}",
                start,
                now,
                attrs,
                status=_tool_status(payload.get("tool_response")),
            )
        ]

    # ---- model requests (Codex) --------------------------------------------------
    def _on_PreModelRequest(self, harness, session, state, payload, now):
        turn = self._turn(harness, state, payload)
        state.setdefault("models", {}).setdefault(turn, []).append(
            {
                "start_ns": now,
                "agent_id": _text(payload.get("agent_id")),
                "model": payload.get("model"),
            }
        )
        return []

    def _on_PostModelRequest(self, harness, session, state, payload, now):
        turn = self._turn(harness, state, payload)
        records = state.setdefault("models", {}).setdefault(turn, [])
        record = next((r for r in records if "end_ns" not in r), None)
        if record is None:
            record = {"start_ns": now, "timing": "post_only"}
            records.append(record)
        record.update(
            end_ns=now,
            usage=payload.get("token_usage"),
            status=payload.get("status"),
            error=payload.get("error"),
        )
        return []

    # ---- subagents -------------------------------------------------------------
    def _on_SubagentStart(self, harness, session, state, payload, now):
        agent = _text(payload.get("agent_id"))
        if agent:
            state.setdefault("subagents", {})[agent] = {
                "start_ns": now,
                "type": _text(payload.get("agent_type")),
                "turn": self._turn(harness, state, payload),
            }
        return []

    def _on_SubagentStop(self, harness, session, state, payload, now):
        agent = _text(payload.get("agent_id"))
        if not agent:
            state["subagents_without_id"] = state.get("subagents_without_id", 0) + 1
            return []
        info = state.get("subagents", {}).pop(agent, {})
        turn = info.get("turn") or self._turn(harness, state, payload)
        kind = _text(payload.get("agent_type"), info.get("type")) or "subagent"
        attrs = base_attrs(harness, session, "hook")
        attrs.update(
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.id": agent,
                "gen_ai.agent.name": kind,
                "ioc.turn.id": turn,
                "ioc.span.type": "subagent",
                "ioc.response_length": text_length(payload.get("last_assistant_message")),
            }
        )
        env = messages(self.cfg, "assistant", payload.get("last_assistant_message"))
        if env:
            attrs["gen_ai.output.messages"] = env
        return [
            SpanRecord(
                harness,
                state["trace_id"],
                ids.span_id(harness, session, "subagent", agent),
                turn_span_id(harness, session, turn),
                f"invoke_agent {kind}",
                info.get("start_ns", now),
                now,
                attrs,
            )
        ]

    # ---- turn end ----------------------------------------------------------------
    def _close_turn(self, harness, session, state, payload, now, status="unset", message=""):
        turn = self._turn(harness, state, payload)
        info = state["turns"].setdefault(turn, {})
        info["end_ns"] = now
        info.setdefault("start_ns", now)
        if payload.get("last_assistant_message") is not None:
            info["response_length"] = text_length(payload["last_assistant_message"])
            if self.cfg.capture_content:
                info["response"] = payload["last_assistant_message"]
        info["status"], info["status_message"] = status, message
        transcript = state.get("transcript_path")
        if harness == CODEX and transcript and Path(transcript).is_file():
            # The rollout owns Codex turn + model spans; hand off to the reconciler.
            info["pending_reconcile"] = True
            return [], ["reconcile", "--transcript", transcript, "--session", session, "--flush"]
        spans = [
            turn_span(
                self.cfg,
                harness,
                session,
                state,
                turn,
                info["start_ns"],
                now,
                "hook",
                status=status,
                status_message=message,
            )
        ]
        if harness == CODEX:
            spans += model_spans_from_hooks(harness, session, state, turn)
        return spans, ["flush"]

    def _on_Stop(self, harness, session, state, payload, now):
        return self._close_turn(harness, session, state, payload, now)

    def _on_Interrupt(self, harness, session, state, payload, now):
        return self._close_turn(harness, session, state, payload, now, "error", "interrupted")

    def _on_SessionEnd(self, harness, session, state, payload, now):
        state["ended_ns"] = now
        state["end_reason"] = _text(payload.get("reason"))
        return [], ["flush"]


def spawn_background(args: List[str], env: Mapping[str, str]) -> None:
    """Run `harness_tap <args>` detached so the hook returns immediately."""
    if env.get("HARNESS_TAP_NO_BACKGROUND"):
        return
    src = str(Path(__file__).resolve().parents[1])
    child_env = dict(env)
    child_env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "harness_tap", *args],
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
