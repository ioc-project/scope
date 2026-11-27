#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Codex rollout reconciliation (after Trajectory's hooks-as-boundaries design).

Codex writes one rollout JSONL per session under $CODEX_HOME/sessions. Lines
are {"timestamp", "type", "payload", "ordinal"}. Observed on CLI 0.167:

  session_meta          payload.id / session_id / originator / source / cli_version
  turn_context          payload.turn_id / root_turn_id / model / approval_policy / sandbox_policy
  event_msg task_started   payload.turn_id / trace_id / started_at / turn_attribution{...}
  event_msg task_complete  payload.turn_id / completed_at / duration_ms / time_to_first_token_ms
  token_usage_record    payload.response_id / turn_id / usage{input_tokens,
                        cached_input_tokens, cache_write_input_tokens, output_tokens,
                        reasoning_output_tokens, total_tokens}

Tool lines (response_item function_call / custom_tool_call and their *_output,
paired by call_id) follow public descriptions; they were not present in the
rollouts inspected, so treat that path as unverified until a tool run is seen.

The rollout is not a stable interface (Codex says so), so parsing is tolerant:
unknown line types are counted and skipped, never fatal.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import ids
from .config import Config
from .hooks import CODEX, base_attrs, model_spans_from_hooks, session_context, turn_span
from .spans import SpanRecord, content, usage_attributes
from .spool import Spool

TOOL_CALLS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
TOOL_OUTPUTS = {"function_call_output", "custom_tool_call_output"}
_FRACTION = re.compile(r"\.(\d+)")


def parse_ts(value: Any) -> Optional[int]:
    """ISO-8601 string or epoch number (s / ms / µs / ns) -> epoch nanoseconds."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # Magnitude heuristic: seconds ~1e9, ms ~1e12, µs ~1e15, ns ~1e18.
        if number < 1e11:
            return int(number * 1e9)
        if number < 1e14:
            return int(number * 1e6)
        if number < 1e17:
            return int(number * 1e3)
        return int(number)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1e9)
    return None


@dataclass
class Rollout:
    meta: Dict[str, Any] = field(default_factory=dict)
    turns: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # insertion-ordered
    requests: List[Dict[str, Any]] = field(default_factory=list)
    token_counts: List[Dict[str, Any]] = field(default_factory=list)
    tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)

    @property
    def session(self) -> str:
        return str(self.meta.get("session_id") or self.meta.get("id") or "")


def parse(path: Path) -> Rollout:
    rollout = Rollout()
    current_turn = ""

    def turn(turn_id: str) -> Dict[str, Any]:
        return rollout.turns.setdefault(turn_id, {"turn_id": turn_id})

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle):
            try:
                row = json.loads(line)
            except ValueError:
                rollout.counts["malformed_line"] += 1
                continue
            if not isinstance(row, dict):
                rollout.counts["malformed_line"] += 1
                continue
            kind = row.get("type")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            sub = payload.get("type")
            ts = parse_ts(row.get("timestamp"))
            ordinal = row.get("ordinal", line_no)
            turn_id = str(payload.get("turn_id") or "")
            if turn_id:
                current_turn = turn_id
                entry = turn(turn_id)
                if ts is not None:
                    entry.setdefault("first_ns", ts)
                    entry["last_ns"] = ts

            if kind == "session_meta":
                for key in (
                    "id",
                    "session_id",
                    "originator",
                    "source",
                    "thread_source",
                    "cli_version",
                    "model_provider",
                    "cwd",
                    "parent_thread_id",
                ):
                    if payload.get(key) is not None:
                        rollout.meta[key] = payload[key]
            elif kind == "turn_context" and turn_id:
                entry = turn(turn_id)
                for key in ("model", "approval_policy", "root_turn_id", "multi_agent_version"):
                    if payload.get(key) is not None:
                        entry[key] = payload[key]
                sandbox = payload.get("sandbox_policy")
                if isinstance(sandbox, dict) and sandbox.get("type"):
                    entry["sandbox_policy"] = sandbox["type"]
            elif kind == "event_msg" and sub == "task_started" and turn_id:
                entry = turn(turn_id)
                # Line timestamps carry ms; started_at/completed_at are whole seconds.
                entry["started_ns"] = ts or parse_ts(payload.get("started_at"))
                if payload.get("trace_id"):
                    entry["native_trace_id"] = payload["trace_id"]
                attribution = payload.get("turn_attribution")
                if isinstance(attribution, dict):
                    for key in ("parent_turn_id", "initiating_agent_path", "turn_trigger"):
                        if attribution.get(key):
                            entry[key] = attribution[key]
            elif kind == "event_msg" and sub == "task_complete" and turn_id:
                entry = turn(turn_id)
                entry["completed_ns"] = ts or parse_ts(payload.get("completed_at"))
                for key in ("duration_ms", "time_to_first_token_ms"):
                    if isinstance(payload.get(key), (int, float)):
                        entry[key] = payload[key]
                entry["last_agent_message"] = payload.get("last_agent_message")
            elif kind == "event_msg" and sub == "turn_aborted":
                entry = turn(turn_id or current_turn)
                entry["aborted"] = True
                entry["completed_ns"] = ts
            elif kind == "token_usage_record":
                rollout.requests.append(
                    {
                        "turn_id": turn_id or current_turn,
                        "response_id": str(payload.get("response_id") or ""),
                        "usage": payload.get("usage"),
                        "ts": ts,
                        "ordinal": ordinal,
                        "thread_id": payload.get("thread_id"),
                    }
                )
            elif kind == "event_msg" and sub == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                if info.get("last_token_usage"):
                    rollout.token_counts.append(
                        {
                            "turn_id": current_turn,
                            "usage": info["last_token_usage"],
                            "ts": ts,
                            "ordinal": ordinal,
                        }
                    )
            elif kind == "response_item" and sub in TOOL_CALLS:
                call = str(payload.get("call_id") or payload.get("id") or "")
                if call:
                    rollout.tools.setdefault(call, {}).update(
                        name=payload.get("name") or sub,
                        arguments=payload.get("arguments", payload.get("input")),
                        start_ns=ts,
                        turn_id=turn_id or current_turn,
                        kind=sub,
                    )
                else:
                    rollout.counts["tool_call_without_id"] += 1
            elif kind == "response_item" and sub in TOOL_OUTPUTS:
                call = str(payload.get("call_id") or "")
                if call:
                    rollout.tools.setdefault(call, {}).update(
                        output=payload.get("output"), end_ns=ts
                    )
            else:
                rollout.counts[f"skipped:{kind}/{sub or '-'}"] += 1
    return rollout


def reconcile(
    cfg: Config,
    path: Path,
    session: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> List[SpanRecord]:
    """Emit spans for completed Codex turns in one rollout; return newly spooled spans."""
    rollout = parse(path)
    session = session or rollout.session
    if not session:
        return []
    spool = Spool(cfg)
    env = dict(os.environ if env is None else env)
    spans: List[SpanRecord] = []
    with spool.session_state(CODEX, session) as state:
        first = min(
            (t.get("first_ns") for t in rollout.turns.values() if t.get("first_ns")), default=0
        )
        spans += session_context(spool, state, CODEX, session, env, first, "rollout")
        state.setdefault("turns", {})
        state.setdefault("reconciled_turns", [])
        provider = str(rollout.meta.get("model_provider") or "")
        for turn_id, info in rollout.turns.items():
            hook = state["turns"].get(turn_id, {})
            done = info.get("completed_ns") or hook.get("end_ns")
            if not done:
                continue  # still running; a later reconcile will pick it up
            if info.get("model"):
                hook.setdefault("model", info["model"])
                state["turns"][turn_id] = hook
            start = hook.get("start_ns") or info.get("started_ns") or info.get("first_ns") or done
            end = hook.get("end_ns") or info.get("completed_ns") or info.get("last_ns") or start
            extra: Dict[str, Any] = {
                "ioc.tap.source": "rollout+hook" if hook.get("end_ns") else "rollout"
            }
            for key, attr in (
                ("native_trace_id", "ioc.codex.trace_id"),
                ("root_turn_id", "ioc.root_turn.id"),
                ("parent_turn_id", "ioc.parent_turn.id"),
                ("initiating_agent_path", "ioc.agent.path"),
                ("turn_trigger", "ioc.turn.trigger"),
                ("approval_policy", "ioc.codex.approval_policy"),
                ("sandbox_policy", "ioc.codex.sandbox_policy"),
                ("duration_ms", "ioc.codex.duration_ms"),
                ("time_to_first_token_ms", "ioc.codex.ttft_ms"),
            ):
                if info.get(key) is not None:
                    extra[attr] = info[key]
            for key in ("originator", "source", "cli_version", "parent_thread_id"):
                if rollout.meta.get(key):
                    extra[f"ioc.codex.{key}"] = rollout.meta[key]
            if info.get("last_agent_message") is not None and "response_length" not in hook:
                extra["ioc.response_length"] = len(str(info["last_agent_message"]))
            parent_turn = info.get("parent_turn_id")
            parent = (
                ids.span_id(CODEX, session, "turn", parent_turn)
                if parent_turn and parent_turn in rollout.turns
                else ""
            )
            aborted = info.get("aborted") or hook.get("status") == "error"
            spans.append(
                turn_span(
                    cfg,
                    CODEX,
                    session,
                    state,
                    turn_id,
                    start,
                    end,
                    "rollout",
                    extra,
                    parent,
                    status="error" if aborted else "unset",
                    status_message=hook.get("status_message") or ("aborted" if aborted else ""),
                )
            )
            spans += _model_spans(cfg, rollout, state, session, turn_id, provider)
            spans += _tool_spans(cfg, rollout, state, session, turn_id)
            hook.pop("pending_reconcile", None)
            if turn_id not in state["reconciled_turns"]:
                state["reconciled_turns"].append(turn_id)
    return spool.append(spans)


def _model_spans(cfg, rollout, state, session, turn_id, provider) -> List[SpanRecord]:
    model = state["turns"].get(turn_id, {}).get("model", "")
    hooked = [r for r in state.get("models", {}).get(turn_id, []) if "end_ns" in r]
    requests = [r for r in rollout.requests if r["turn_id"] == turn_id]
    source = "rollout"
    if not requests:
        if hooked:
            return model_spans_from_hooks(CODEX, session, state, turn_id, "hook")
        requests = [r for r in rollout.token_counts if r["turn_id"] == turn_id]
        source = "rollout_token_count"
    # Only borrow hook timing when the two sources agree on the request count.
    timings = hooked if len(hooked) == len(requests) else []
    spans = []
    for index, request in enumerate(requests):
        attrs = base_attrs(CODEX, session, source)
        attrs.update(
            {"gen_ai.operation.name": "chat", "ioc.turn.id": turn_id, "turn.id": turn_id},
            **usage_attributes(request.get("usage")),
        )
        if model:
            attrs["gen_ai.request.model"] = model
        if provider:
            attrs["gen_ai.provider.name"] = provider
        if request.get("response_id"):
            attrs["gen_ai.response.id"] = request["response_id"]
            attrs["request_id"] = request["response_id"]
        if timings:
            start, end = timings[index]["start_ns"], timings[index]["end_ns"]
            attrs["ioc.timing.source"] = "hook"
        else:
            end = request.get("ts") or 0
            start = end
            attrs["ioc.timing.source"] = "rollout_record_time"
        key = request.get("response_id") or f"rec-{request.get('ordinal')}"
        spans.append(
            SpanRecord(
                CODEX,
                state["trace_id"],
                ids.span_id(CODEX, session, "chat", turn_id, key),
                ids.span_id(CODEX, session, "turn", turn_id),
                f"chat {model or 'unknown'}",
                start,
                end,
                attrs,
                kind="client",
            )
        )
    return spans


def _tool_spans(cfg, rollout, state, session, turn_id) -> List[SpanRecord]:
    spans = []
    for call, info in rollout.tools.items():
        if info.get("turn_id") != turn_id or info.get("end_ns") is None:
            continue
        name = str(info.get("name") or "unknown")
        attrs = base_attrs(CODEX, session, "rollout")
        attrs.update(
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": call,
                "call_id": call,
                "ioc.turn.id": turn_id,
                "ioc.timing.source": "rollout",
            }
        )
        for attr, value in (
            ("gen_ai.tool.call.arguments", info.get("arguments")),
            ("gen_ai.tool.call.result", info.get("output")),
        ):
            text = content(cfg, value)
            if text is not None:
                attrs[attr] = text
        start = info.get("start_ns") or info["end_ns"]
        spans.append(
            SpanRecord(
                CODEX,
                state["trace_id"],
                # Same derivation as the hook path, so a hooked tool is not re-emitted.
                ids.span_id(CODEX, session, "tool", call),
                ids.span_id(CODEX, session, "turn", turn_id),
                f"execute_tool {name}",
                start,
                info["end_ns"],
                attrs,
            )
        )
    return spans


def codex_sessions_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    home = env.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(env.get("CODEX_SESSIONS_DIR") or Path(home) / "sessions")


def reconcile_all(cfg: Config, root: Optional[Path] = None) -> Dict[str, int]:
    """Backfill: reconcile every rollout under the Codex sessions directory."""
    root = root or codex_sessions_dir()
    summary = Counter()
    for path in sorted(root.glob("**/rollout-*.jsonl")):
        if path.is_symlink():
            summary["skipped_symlink"] += 1
            continue
        try:
            summary["spans"] += len(reconcile(cfg, path))
            summary["files"] += 1
        except OSError:
            summary["unreadable"] += 1
    return dict(summary)
