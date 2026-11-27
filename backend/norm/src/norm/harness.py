#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Replay ClickHouse harness telemetry without IOA Observe inference."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from scope_ontology.models import edges as E
from scope_ontology.models import nodes as N
from scope_ontology.models.base import KGEdge, KGNode

from .ioa_observe.fields import get_start_time

CLAUDE, CODEX = "claude-code", "codex"
INTERACTION, LLM, TOOL = ("claude_code." + name for name in ("interaction", "llm_request", "tool"))
EXECUTION, PERMISSION = f"{TOOL}.execution", f"{TOOL}.blocked_on_user"
# Hop spans: ioa_observe SLIMRPC names and agntcy/shadi agentbridge names.
BRIDGES = {
    "ioa_observe": ("a2a_slimrpc_send.task", "a2a_slimrpc_receive.task"),
    "agentbridge": ("shadi.a2a.send", "shadi.a2a.receive"),
}
SENDS = frozenset(send for send, _ in BRIDGES.values())
RECEIVES = frozenset(receive for _, receive in BRIDGES.values())
BRIDGE_SPANS = SENDS | RECEIVES
BRIDGE_PROTOCOL = {name: proto for proto, names in BRIDGES.items() for name in names}
CODEX_EVENTS = {
    "conversation_starts",
    "startup_phase",
    "websocket_connect",
    "websocket_request",
    "user_prompt",
    "turn_ttft",
    "sse_event",
    "tool_result",
    "agent_response",
    "api_request",
}
CODEX_HOUSEKEEPING = {
    "initialize",
    "config/read",
    "auth",
    "thread/list",
    "list_models",
    "endpoint_session.execute_with",
    "fetch_recommended_plugins",
    "recommended_plugins_mode_for_config",
}
CLAUDE_HOUSEKEEPING = {"away_summary", "generate_session_title", "prompt_suggestion"}
CLAUDE_LOG_EVENTS = {
    f"claude_code.{name}"
    for name in ("api_request", "tool_result", "tool_decision", "user_prompt", "assistant_response")
}

Row = dict[str, Any]
Key = tuple[str, ...]


def _id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]
    return f"harness:{kind}:{digest}"


def _text(*values: Any) -> str:
    return next((v for v in values if isinstance(v, str) and v.strip()), "")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _tokens(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if number.is_finite() and 0 <= number < 2**63 and number == number.to_integral_value():
        return int(number)
    return None


def _success(value: Any) -> bool | None:
    text = str(value).lower()
    return True if text in {"true", "ok"} else False if text in {"false", "error"} else None


def _label(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[\w./:@+-]{1,160}", value) else ""


@dataclass(eq=False)
class Signal:
    fingerprint: str
    kind: str
    name: str
    source: str
    attrs: Row
    resource: Row
    trace: str
    span: str
    parent: str
    time: float | None
    duration: float | None
    outcome: bool | None
    native: str
    shared: str
    scope: Key = ()

    @property
    def span_key(self) -> tuple[str, str]:
        return self.trace, self.span

    @property
    def agent(self) -> str:
        return _text(self.resource.get("ioc.agent.id"), self.attrs.get("ioc.agent.id"))


def _signals(rows: Iterable[Row], kind: str, counts: Counter[str]) -> list[Signal]:
    result: list[Signal] = []
    seen: set[str] = set()
    maps = ("ResourceAttributes", "SpanAttributes" if kind == "trace" else "LogAttributes")
    for index, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict) or not all(isinstance(row.get(m, {}), dict) for m in maps):
                raise ValueError
            resource, attrs = (row.get(m, {}) for m in maps)
            if kind == "trace" and not _text(row.get("SpanName")):
                raise ValueError
            if kind == "log" and not any(k in row for k in ("LogAttributes", "EventName", "Body")):
                raise ValueError
            timestamp = get_start_time(row, {}) if row.get("Timestamp") else None
            duration = _number(row.get("Duration")) if kind == "trace" else None
            if kind == "trace" and "Duration" in row and duration is None:
                raise ValueError
            fingerprint = _id("row", json.dumps(row, sort_keys=True, allow_nan=False))
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"Malformed {kind} row {index}") from None
        if fingerprint in seen:
            counts["duplicate_rows"] += 1
            continue
        seen.add(fingerprint)
        name = (
            _text(row.get("SpanName"))
            if kind == "trace"
            else _text(attrs.get("event.name"), row.get("EventName"))
        )
        service = _text(resource.get("service.name"), row.get("ServiceName"))
        source = (
            CLAUDE
            if service in {"claude-code", "claude_code"}
            else (CODEX if service in {"codex", "codex-app-server", "codex-cli"} else "")
        )
        if kind == "log" and source == CLAUDE and f"claude_code.{name}" in CLAUDE_LOG_EVENTS:
            name = f"claude_code.{name}"
        native_key = "session.id" if source == CLAUDE else "conversation.id"
        outcome = _success(attrs.get("success"))
        if outcome is None:
            outcome = _success(row.get("StatusCode"))
        result.append(
            Signal(
                fingerprint,
                kind,
                name,
                source,
                attrs,
                resource,
                _text(row.get("TraceId")),
                _text(row.get("SpanId")),
                _text(row.get("ParentSpanId")),
                timestamp,
                duration / 1e6 if duration is not None else _number(attrs.get("duration_ms")),
                outcome,
                _text(attrs.get(native_key), resource.get(native_key)),
                _text(resource.get("ioc.session.id")),
            )
        )
    return result


@dataclass
class Call:
    signal: Signal
    id: str
    agent: str = ""
    owner: str = ""
    label: str = ""
    provider: str = ""
    enrichment: Signal | None = None
    details: dict[str, Any] = field(default_factory=dict)
    sides: tuple[tuple[str, Any], tuple[str, Any]] = (("", None), ("", None))


@dataclass
class HarnessReplay:
    nodes: list[Row]
    edges: list[Row]
    coverage: Row


class _Replay:
    def __init__(self, capture: str, traces: Iterable[Row], logs: Iterable[Row], content: bool):
        self.capture = capture
        self.content = content
        self.counts: Counter[str] = Counter()
        self.spans: dict[tuple[str, str], Signal] = {}
        self.costs: dict[str, float] = {}
        self.nodes: dict[str, Row] = {}
        self.edges: dict[tuple[str, str, str], Row] = {}
        spans: dict[tuple[str, str], list[Signal]] = defaultdict(list)
        for signal in _signals(traces, "trace", self.counts):
            if all(signal.span_key):
                spans[signal.span_key].append(signal)
            else:
                self.counts["missing_span_identity"] += 1
        for key, candidates in spans.items():
            if len(candidates) == 1:
                self.spans[key] = candidates[0]
            else:
                self.counts["ambiguous_span_identity"] += len(candidates)
        self.logs = _signals(logs, "log", self.counts)
        self.rows = [*self.spans.values(), *self.logs]
        self._scope_rows()

    def identity(self, kind: str, *parts: str) -> str:
        return _id(kind, self.capture, *parts)

    def lineage(self, row: Signal) -> Iterable[Signal]:
        if not row.trace:
            return
        parent = row.parent if row.kind == "trace" else row.span
        visited = {row.span} if row.kind == "trace" else set()
        while parent and parent not in visited:
            visited.add(parent)
            ancestor = self.spans.get((row.trace, parent))
            if ancestor is None:
                break
            yield ancestor
            parent = ancestor.parent

    def _scope_rows(self) -> None:
        trace_natives: dict[Key, set[str]] = defaultdict(set)
        for row in self.rows:
            if row.source and row.trace and row.native:
                trace_natives[row.source, row.trace].add(row.native)
        memberships: dict[Key, set[str]] = defaultdict(set)
        for row in self.rows:
            if not row.source:
                continue
            if not row.native and row.trace:
                possibilities = trace_natives[row.source, row.trace]
                if len(possibilities) == 1:
                    row.native = next(iter(possibilities))
                    self.counts["derived_native_session"] += 1
            if row.native and row.shared:
                memberships[row.source, row.native].add(row.shared)
        for row in self.rows:
            if not row.source:
                continue
            options = memberships[row.source, row.native] if row.native else set()
            if len(options) > 1:
                self.counts["conflicting_shared_session"] += 1
                continue
            shared = row.shared or next(iter(options), "")
            if shared:
                row.scope = ("shared", shared)
            elif row.native:
                row.scope = ("native", row.source, row.native)

    def span_id(self, kind: str, span: Signal) -> str:
        return self.identity(kind, span.source, *span.scope, *span.span_key)

    def join_scope(self, row: Signal) -> Key:
        # A shared session is membership, not permission to join native call IDs.
        return (*row.scope, row.source, row.native) if row.scope and row.native else ()

    def join(
        self, left: Iterable[Signal], right: Iterable[Signal], key: str, label: str
    ) -> dict[Signal, Signal]:
        left_by_key: dict[Key, list[Signal]] = defaultdict(list)
        right_by_key: dict[Key, list[Signal]] = defaultdict(list)
        for rows, index in ((left, left_by_key), (right, right_by_key)):
            for row in rows:
                value = _text(row.attrs.get(key))
                scope = self.join_scope(row)
                if value and scope:
                    index[(*scope, value)].append(row)
                elif index is right_by_key:
                    self.counts[f"unmatched_{label}"] += 1
        matches = {}
        for scoped_key, candidates in right_by_key.items():
            owners = left_by_key.get(scoped_key, [])
            if len(candidates) == len(owners) == 1:
                matches[owners[0]] = candidates[0]
                self.counts[f"joined_{label}"] += 1
            elif owners:
                self.counts[f"ambiguous_{label}"] += len(candidates)
            else:
                self.counts[f"unmatched_{label}"] += len(candidates)
        return matches

    def claude(self) -> tuple[list[Call], list[Call]]:
        spans = [s for s in self.spans.values() if s.source == CLAUDE and s.scope]
        logs = [s for s in self.logs if s.source == CLAUDE]
        interactions = [s for s in spans if s.name == INTERACTION]
        models = [s for s in spans if s.name == LLM]
        tools = [s for s in spans if s.name == TOOL]
        model_logs = self.join(
            models,
            (s for s in logs if s.name == "claude_code.api_request"),
            "request_id",
            "claude_api_request",
        )
        tool_logs = self.join(
            tools,
            (s for s in logs if s.name == "claude_code.tool_result"),
            "tool_use_id",
            "claude_tool_result",
        )
        self.join(
            tools,
            (s for s in logs if s.name == "claude_code.tool_decision"),
            "tool_use_id",
            "claude_tool_decision",
        )
        enrichments: dict[tuple[Signal, str], list[Signal]] = defaultdict(list)
        for span in spans:
            if span.name in {EXECUTION, PERMISSION}:
                tool = next(
                    (
                        p
                        for p in self.lineage(span)
                        if p.name == TOOL and self._same_native(span, p)
                    ),
                    None,
                )
                if tool is not None:
                    enrichments[tool, span.name].append(span)
                else:
                    self.counts["unmatched_claude_tool_detail"] += 1
        owners = {
            s: Call(
                s,
                self.span_id("agent-call", s),
                agent=s.agent,
                sides=(("user_prompt", s.attrs.get("user_prompt")), ("", None)),
            )
            for s in interactions
        }
        # Some Claude logs carry a prompt ID but no OTLP span context. Only an
        # explicit prompt ID or ancestry can attach a payload to a traced turn.
        for log_name, side, keys in (
            ("claude_code.user_prompt", 0, ("prompt", "prompt_text")),
            ("claude_code.assistant_response", 1, ("response",)),
        ):
            matches: dict[Signal, list[Signal]] = defaultdict(list)
            prompt_ids: dict[Key, list[Signal]] = defaultdict(list)
            for span in interactions:
                scope = self.join_scope(span)
                if scope and (prompt_id := _text(span.attrs.get("prompt.id"))):
                    prompt_ids[(*scope, prompt_id)].append(span)
            label = "claude_" + log_name.split(".")[-1]
            for log in (s for s in logs if s.name == log_name and not s.attrs.get("agent_id")):
                if _text(log.attrs.get("query_source")) in CLAUDE_HOUSEKEEPING:
                    self.counts["claude_housekeeping_response"] += 1
                    continue
                ancestry = [p for p in self.lineage(log) if self._same_native(log, p)]
                if any(p.attrs.get("agent_id") for p in ancestry):
                    continue
                direct = next((p for p in ancestry if p in owners), None)
                scope = self.join_scope(log)
                prompt_id = _text(log.attrs.get("prompt.id"))
                keyed = prompt_ids.get((*scope, prompt_id), []) if scope and prompt_id else []
                if direct and keyed and direct not in keyed:
                    self.counts[f"ambiguous_{label}"] += 1
                elif direct:
                    matches[direct].append(log)
                elif len(keyed) == 1:
                    matches[keyed[0]].append(log)
                else:
                    self.counts[f"{'ambiguous' if keyed else 'unmatched'}_{label}"] += 1
            for span, candidates in matches.items():
                if len(candidates) != 1:
                    self.counts[f"ambiguous_{label}"] += len(candidates)
                    continue
                log = candidates[0]
                self.counts[f"joined_{label}"] += 1
                sides = list(owners[span].sides)
                if not sides[side][1]:
                    sides[side] = (keys[0], _text(*(log.attrs.get(k) for k in keys)))
                owners[span].sides = (sides[0], sides[1])
        subagents: dict[Key, list[Signal]] = defaultdict(list)
        calls: list[Call] = []
        for span in models + tools:
            a = span.attrs
            if span.name == LLM and _text(a.get("query_source_safe")) in CLAUDE_HOUSEKEEPING:
                self.counts["claude_housekeeping"] += 1
                continue
            ancestry = [p for p in self.lineage(span) if self._same_native(span, p)]
            interaction = next((p for p in ancestry if p in owners), None)
            subagent = _text(
                a.get("agent_id"),
                *(p.attrs.get("agent_id") for p in ancestry if p is not interaction),
            )
            owner = owners[interaction].id if interaction is not None else ""
            if subagent:
                # The outer interaction can end before a subagent finishes.
                subkey = (
                    *span.scope,
                    span.native,
                    span.trace,
                    interaction.span if interaction else "",
                    subagent,
                )
                subagents[subkey].append(span)
                owner = self.identity("claude-subagent-call", *subkey)
            if not owner:
                self.counts["missing_claude_owner"] += 1
                continue
            if span.name == LLM:
                log = model_logs.get(span)
                call = Call(
                    span,
                    self.span_id("llm-call", span),
                    owner=owner,
                    enrichment=log,
                    label=_label(
                        _text(
                            a.get("gen_ai.request.model"),
                            a.get("model"),
                            log.attrs.get("model") if log else None,
                        )
                    ),
                    provider=_label(a.get("gen_ai.provider.name")),
                    sides=(
                        ("gen_ai.input.messages", a.get("gen_ai.input.messages")),
                        ("gen_ai.output.messages", a.get("gen_ai.output.messages")),
                    ),
                )
                for prop, keys in {
                    "promptTokenCount": ("gen_ai.usage.input_tokens", "input_tokens"),
                    "completionTokenCount": ("gen_ai.usage.output_tokens", "output_tokens"),
                    "cacheReadTokenCount": (
                        "gen_ai.usage.cache_read.input_tokens",
                        "cache_read_tokens",
                    ),
                    "totalTokenCount": ("gen_ai.usage.total_tokens",),
                }.items():
                    values = [
                        _tokens(attrs.get(k))
                        for attrs in (a, log.attrs if log else {})
                        for k in keys
                    ]
                    value = next((v for v in values if v is not None), None)
                    if value is not None:
                        call.details[prop] = value
                finish = a.get("gen_ai.response.finish_reasons")
                if isinstance(finish, list):
                    finish = finish[0] if finish else None
                if reason := _label(_text(finish, a.get("stop_reason"))):
                    call.details["finishReason"] = reason
                if log and (cost := _number(log.attrs.get("cost_usd"))) is not None:
                    self.costs[call.id] = cost
            else:
                executions = enrichments.get((span, EXECUTION), [])
                if len(executions) > 1:
                    self.counts["ambiguous_claude_tool_execution"] += 1
                if enrichments.get((span, PERMISSION)):
                    self.counts["observed_claude_permission"] += 1
                log = tool_logs.get(span)
                execution = executions[0] if len(executions) == 1 else None
                call = Call(
                    span,
                    self.span_id("tool-call", span),
                    owner=owner,
                    label=_label(
                        _text(
                            a.get("gen_ai.tool.name"), a.get("tool_name_safe"), a.get("tool_name")
                        )
                    ),
                    enrichment=log
                    if log is not None and log.outcome is not None
                    else execution or log,
                )
            calls.append(call)
        extra_owners = []
        for key, activity in subagents.items():
            anchor = min(activity, key=lambda s: s.span_key)
            call = Call(
                anchor,
                self.identity("claude-subagent-call", *key),
                agent=anchor.agent or self.identity("claude-subagent", anchor.native, key[-1]),
            )
            starts = [s.time for s in activity if s.time is not None]
            ends = [s.time + (s.duration or 0) / 1000 for s in activity if s.time is not None]
            if starts:
                call.details.update(
                    startTime=min(starts),
                    endTime=max(ends),
                    duration=(max(ends) - min(starts)) * 1000,
                )
            extra_owners.append(call)
        self.counts["observed_claude_interaction"] += len(owners)
        self.counts["derived_claude_subagent"] += len(extra_owners)
        self.counts["observed_claude_llm"] += sum(c.signal.name == LLM for c in calls)
        self.counts["observed_claude_tool"] += sum(c.signal.name == TOOL for c in calls)
        return [*owners.values(), *extra_owners], calls

    @staticmethod
    def _same_native(a: Signal, b: Signal) -> bool:
        return (a.source, a.native, a.scope) == (b.source, b.native, b.scope)

    def codex(self) -> tuple[list[Call], list[Call]]:
        rows = [s for s in self.logs if s.source == CODEX]
        self.counts["codex_housekeeping"] += sum(
            s.source == CODEX and (s.name in CODEX_HOUSEKEEPING or s.name.startswith("fs."))
            for s in self.spans.values()
        )
        self.counts["unsupported_codex_span"] += sum(
            s.source == CODEX and s.name not in CODEX_HOUSEKEEPING and not s.name.startswith("fs.")
            for s in self.spans.values()
        )
        turn_rows: dict[Key, list[Signal]] = defaultdict(list)
        trace_turns: dict[Key, set[str]] = defaultdict(set)
        for row in rows:
            event = row.name.removeprefix("codex.")
            if row.name == f"codex.{event}" and event in CODEX_EVENTS and self.join_scope(row):
                if turn := _text(row.attrs.get("turn.id")):
                    turn_rows[(*self.join_scope(row), turn)].append(row)
                    if row.trace:
                        trace_turns[(*self.join_scope(row), row.trace)].add(turn)
        owners: dict[Key, Call] = {}
        prompts: dict[Key, list[Signal]] = defaultdict(list)
        responses: dict[Key, list[Signal]] = defaultdict(list)
        tool_rows: dict[Key, list[Signal]] = defaultdict(list)
        request_rows: dict[Key, list[Signal]] = defaultdict(list)
        usage_rows: dict[Key, list[Signal]] = defaultdict(list)
        for row in rows:
            if row.name in {
                "codex.startup_phase",
                "codex.websocket_connect",
                "codex.conversation_starts",
            } or (row.name == "codex.api_request" and row.attrs.get("endpoint") == "/models"):
                self.counts["codex_housekeeping"] += 1
                continue
            scope = self.join_scope(row)
            explicit = _text(row.attrs.get("turn.id"))
            turns = trace_turns.get((*scope, row.trace), set()) if row.trace else set()
            inferred = next(iter(turns)) if len(turns) == 1 else ""
            turn = explicit or inferred
            key = (*scope, turn) if scope and turn else ()
            request_id = _text(row.attrs.get("request_id"))
            if (
                row.name == "codex.sse_event"
                and row.attrs.get("event.kind") == "response.completed"
            ):
                if scope and request_id:
                    usage_rows[(*scope, request_id)].append(row)
                else:
                    self.counts["unmatched_codex_sse_usage"] += 1
                continue
            if row.name == "codex.websocket_request":
                if key and request_id:
                    request_rows[(*scope, request_id)].append(row)
                else:
                    self.counts["unidentified_codex_model_request"] += 1
                continue
            if row.name in {
                "codex.agent_response",
                "codex.user_prompt",
                "codex.tool_result",
                "codex.turn_ttft",
            }:
                if not key:
                    self.counts[
                        "ambiguous_codex_turn" if len(turns) > 1 else "missing_codex_turn"
                    ] += 1
                    continue
                if not explicit:
                    self.counts["derived_codex_turn_trace"] += 1
                if row.name == "codex.user_prompt":
                    prompts[key].append(row)
                elif row.name == "codex.agent_response":
                    responses[key].append(row)
                elif row.name == "codex.tool_result":
                    if call_id := _text(row.attrs.get("call_id")):
                        tool_rows[(*key, call_id)].append(row)
                    else:
                        self.counts["missing_codex_tool_call_id"] += 1
            else:
                self.counts["unsupported_codex_log"] += 1
        calls: list[Call] = []
        for key in prompts.keys() | responses.keys() | {k[:-1] for k in tool_rows}:
            prompt = prompts[key][0] if len(prompts[key]) == 1 else None
            response = responses[key][0] if len(responses[key]) == 1 else None
            if len(prompts[key]) > 1 or len(responses[key]) > 1:
                self.counts["ambiguous_codex_agent_payload"] += 1
            candidates = prompts[key] + responses[key] + turn_rows[key]
            if not candidates:
                candidates = [
                    s for k, entries in tool_rows.items() if k[:-1] == key for s in entries
                ]
            anchor = min(candidates, key=lambda s: (s.time is None, s.time, s.fingerprint))
            agents = {s.agent for s in candidates if s.agent}
            if len(agents) > 1:
                self.counts["conflicting_codex_agent"] += 1
            owner = Call(
                anchor,
                self.identity("codex-agent-call", *key),
                agent=next(iter(agents)) if len(agents) == 1 else "",
                sides=(
                    ("prompt", prompt.attrs.get("prompt") if prompt else None),
                    (
                        "response",
                        response.attrs.get("response")
                        if response
                        and _success(response.attrs.get("response_truncated")) is not True
                        else None,
                    ),
                ),
            )
            if (
                prompt
                and response
                and prompt.time is not None
                and response.time is not None
                and response.time >= prompt.time
            ):
                owner.details.update(
                    startTime=prompt.time,
                    endTime=response.time,
                    duration=(response.time - prompt.time) * 1000,
                )
            else:
                self.counts["missing_codex_turn_bounds"] += 1
            owners[key] = owner
        for key, results in sorted(tool_rows.items()):
            if len(results) != 1:
                self.counts["ambiguous_codex_tool_result"] += len(results)
                continue
            row = results[0]
            namespace, tool = (
                _label(row.attrs.get("tool_namespace")),
                _label(row.attrs.get("tool_name")),
            )
            if not tool:
                self.counts["missing_codex_tool_name"] += 1
                continue
            path = _text(row.attrs.get("agent_name"))
            owner = owners[key[:-1]]
            if (path and path != "/root") or (
                row.agent and owner.agent and row.agent != owner.agent
            ):
                agent_key = (*key[:-1], row.agent or path)
                owner = owners.setdefault(
                    agent_key,
                    Call(
                        row,
                        self.identity("codex-agent-call", *agent_key),
                        agent=row.agent or self.identity("codex-agent", row.native, path),
                    ),
                )
            calls.append(
                Call(
                    row,
                    self.identity("codex-tool-call", *key),
                    owner=owner.id,
                    label=f"{namespace}.{tool}" if namespace else tool,
                    sides=(
                        ("arguments", row.attrs.get("arguments")),
                        (
                            "output",
                            row.attrs.get("output")
                            if _success(row.attrs.get("output_truncated")) is not True
                            else None,
                        ),
                    ),
                )
            )
        for key, candidates in request_rows.items():
            if len(candidates) != 1:
                self.counts["ambiguous_codex_model_request"] += len(candidates)
                continue
            row = candidates[0]
            turn = _text(row.attrs.get("turn.id")) or next(
                iter(trace_turns[(*key[:-1], row.trace)])
            )
            owner = owners.get((*key[:-1], turn))
            if owner is None:
                self.counts["missing_codex_model_owner"] += 1
                continue
            usage = usage_rows.pop(key, [])
            if len(usage) > 1:
                self.counts["ambiguous_codex_sse_usage"] += len(usage)
            call = Call(
                row,
                self.identity("codex-llm-call", *key),
                owner=owner.id,
                label=_label(row.attrs.get("model")),
            )
            if len(usage) == 1:
                self.counts["joined_codex_sse_usage"] += 1
                for prop, attr in (
                    ("promptTokenCount", "input_token_count"),
                    ("completionTokenCount", "output_token_count"),
                    ("cacheReadTokenCount", "cached_token_count"),
                ):
                    if (value := _tokens(usage[0].attrs.get(attr))) is not None:
                        call.details[prop] = value
            calls.append(call)
        self.counts["unmatched_codex_sse_usage"] += sum(map(len, usage_rows.values()))
        self.counts["observed_codex_tool"] += sum(
            c.signal.name == "codex.tool_result" for c in calls
        )
        self.counts["observed_codex_llm"] += sum(
            c.signal.name == "codex.websocket_request" for c in calls
        )
        return list(owners.values()), calls

    def node(self, node: KGNode, *, execution: bool = False) -> str:
        data = node.to_json()
        if execution:
            data = {k: v for k, v in data.items() if k in node.model_fields_set or k == "node_type"}
        for key in ("hierarchyLevel", "layer"):
            if (value := getattr(node, key, None)) is not None:
                data[key] = value
        self.nodes[data["id"]] = data
        return data["id"]

    def edge(self, cls: type[KGEdge], source: str, target: str) -> None:
        edge = cls(source_id=source, target_id=target).to_json()
        self.edges[edge["edge_type"], source, target] = edge

    def states(self, call: Call, session: str) -> None:
        initial, final = (self.identity("state", call.id, side) for side in ("initial", "final"))
        for state, (key, value) in zip((initial, final), call.sides):
            content = ""
            if not self.content:
                self.counts["payload_excluded"] += 1
            elif isinstance(value, str) and value.strip().lower() not in {
                "",
                "<redacted>",
                "[redacted]",
                "redacted",
                "(redacted)",
            }:
                content = value
            elif key.startswith("gen_ai.") and isinstance(value, list) and value:
                content = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if self.content:
                self.counts["payload_observed" if content else "payload_missing_or_redacted"] += 1
            self.node(
                N.State(id=state, sessionId=session, content=content or "(no content recorded)")
            )
            self.edge(E.hasState, call.id, state)
        transition = self.node(
            N.Transition(id=self.identity("transition", call.id), sessionId=session)
        )
        self.edge(E.hasInitialState, call.id, initial)
        self.edge(E.hasFinalState, call.id, final)
        self.edge(E.inputTo, initial, transition)
        self.edge(E.leadsTo, transition, final)
        self.edge(E.correspondsToTransition, call.id, transition)
        self.edge(E.representsExecution, transition, call.id)

    def emit(self, owners: list[Call], calls: list[Call]) -> None:
        owner_agents = {}
        capabilities = {}
        sessions: dict[Key, list[tuple[float, float]]] = defaultdict(list)
        for call in sorted(owners + calls, key=lambda c: c.id):
            row = call.signal
            session = self.identity("session", *row.scope)
            duration = row.duration
            if duration is None and call.enrichment:
                duration = call.enrichment.duration
            time = row.time
            ms = duration or 0.0
            end = (time or 0.0) + (ms / 1000 if row.kind == "trace" else 0)
            start = end - ms / 1000 if row.kind == "log" and time is not None else time or 0.0
            timing = {
                k: call.details[k]
                for k in ("startTime", "endTime", "duration")
                if k in call.details
            }
            fields = dict(
                id=call.id,
                name=call.id,
                sessionId=session,
                startTime=start,
                endTime=end,
                duration=ms,
            )
            fields.update(timing)
            if time is not None or timing:
                sessions[row.scope].append((fields["startTime"], fields["endTime"]))
            else:
                self.counts["missing_timing"] += 1
                sessions.setdefault(row.scope, [])
            if row.kind == "trace" and not timing:
                fields.update(spanId=row.span, parentSpanId=row.parent)
            outcome = (
                row.outcome
                if row.outcome is not None
                else (call.enrichment.outcome if call.enrichment else None)
            )
            if outcome is not None and (call.owner or row.name == INTERACTION):
                fields["success"] = outcome
            else:
                self.counts["missing_outcome"] += 1
            if not call.owner:
                self.node(N.AgentCall(**fields), execution=True)
                agent_id = call.agent or self.identity(
                    "agent-placeholder", row.source, row.native, *row.scope
                )
                self.counts["observed_agent_identity" if call.agent else "placeholder_agent"] += 1
                agent_id = self.identity("agent", row.source, agent_id)
                self.node(N.Agent(id=agent_id, name=f"{row.source} [{agent_id}]"))
                owner_agents[call.id] = agent_id
                self.edge(E.executesAgent, call.id, agent_id)
                mas_call = self.identity("mas-call", *row.scope)
                self.edge(E.hasAgentCall, mas_call, call.id)
                self.edge(E.belongsToMAS, agent_id, self.identity("mas", *row.scope))
            elif row.name in {LLM, "codex.websocket_request"}:
                extras = {k: v for k, v in call.details.items() if k not in timing}
                if not call.label:
                    self.counts["missing_model_name"] += 1
                llm_id = self.identity(
                    "llm", row.source, call.provider, call.label, call.id if not call.label else ""
                )
                self.node(
                    N.LLM(
                        id=llm_id,
                        name=f"{call.label or 'Unknown model'} [{llm_id}]",
                        provider=call.provider,
                    )
                )
                self.node(N.LLMCall(**fields, **extras), execution=True)
                self.edge(E.executesLLM, call.id, llm_id)
                self.edge(E.hasLLMCall, call.owner, call.id)
                capabilities[call.id] = (E.usesLLM, llm_id)
            else:
                if not call.label:
                    self.counts["missing_tool_name"] += 1
                tool_id = self.identity(
                    "tool", row.source, call.label, call.id if not call.label else ""
                )
                self.node(N.Tool(id=tool_id, name=f"{call.label or 'Unknown tool'} [{tool_id}]"))
                self.node(N.ToolCall(**fields), execution=True)
                self.edge(E.executesTool, call.id, tool_id)
                self.edge(E.hasToolCall, call.owner, call.id)
                capabilities[call.id] = (E.usesTool, tool_id)
            self.states(call, session)
        for call in calls:
            edge, target = capabilities[call.id]
            self.edge(edge, owner_agents[call.owner], target)
        for scope, bounds in sessions.items():
            session, mas, mas_call = (
                self.identity(k, *scope) for k in ("session", "mas", "mas-call")
            )
            start = min((pair[0] for pair in bounds), default=0.0)
            end = max((pair[1] for pair in bounds), default=0.0)
            fields = dict(
                sessionId=session, startTime=start, endTime=end, duration=(end - start) * 1000
            )
            self.node(N.Session(id=session, name=session, **fields), execution=True)
            self.node(N.MAS(id=mas, name=mas))
            self.node(N.MASCall(id=mas_call, name=mas_call, **fields), execution=True)
            self.edge(E.executesSession, session, mas)
            self.edge(E.hasMASCall, session, mas_call)
            self.edge(E.executesMAS, mas_call, mas)
            self.counts[
                "observed_shared_session" if scope[0] == "shared" else "native_session"
            ] += 1

    def bridge(self, owners: list[Call]) -> list[Row]:
        by_span: dict[Signal, list[str]] = defaultdict(list)
        for owner in owners:
            by_span[owner.signal].append(owner.id)
        received_by: dict[Signal, list[str]] = defaultdict(list)
        for owner in owners:
            nearest = next((s for s in self.lineage(owner.signal) if s.name in RECEIVES), None)
            if nearest is not None and (
                not nearest.shared or owner.signal.scope == ("shared", nearest.shared)
            ):
                received_by[nearest].append(owner.id)
        hops = []
        matched_sends = set()
        for receive in (s for s in self.spans.values() if s.name in RECEIVES):
            send = next((s for s in self.lineage(receive) if s.name in BRIDGE_SPANS), None)
            if send is None or send.name not in SENDS:
                self.counts["missing_bridge_send"] += 1
                continue
            if BRIDGE_PROTOCOL[send.name] != BRIDGE_PROTOCOL[receive.name]:
                self.counts["mixed_bridge_protocol"] += 1
                continue
            if receive.shared and send.shared and receive.shared != send.shared:
                self.counts["conflicting_bridge_sessions"] += 1
                continue
            matched_sends.add(send)
            shared = send.shared or receive.shared
            if shared and receive.shared != shared:
                callees = [
                    call_id
                    for call_id in received_by[receive]
                    if self.nodes[call_id]["sessionId"]
                    == self.identity("session", "shared", shared)
                ]
            else:
                callees = received_by[receive]
            callers = next(
                (
                    by_span[s]
                    for s in self.lineage(send)
                    if s in by_span and (not shared or s.scope == ("shared", shared))
                ),
                [],
            )
            hop = {
                "id": self.identity("hop", *send.span_key, *receive.span_key),
                "classification": "end_to_end"
                if len(callers) == len(callees) == 1
                else "bridge_only",
                "context_id_observed": any(
                    _text(s.attrs.get("a2a.context_id")) for s in (send, receive)
                ),
                "peer_did_observed": any(
                    _text(s.attrs.get("peer.did"), s.resource.get("peer.did"))
                    for s in (send, receive)
                ),
                "task_id_observed": any(_text(s.attrs.get("a2a.task_id")) for s in (send, receive)),
                "bridge": BRIDGE_PROTOCOL[receive.name],
            }
            # agentbridge records the peer's reply state (send) and admission result (receive).
            for side, span in (("send", send), ("receive", receive)):
                if outcome := _label(span.attrs.get("a2a.outcome")):
                    hop[f"{side}_outcome"] = outcome
            if len(callers) == 1:
                hop["caller_call_id"] = callers[0]
            if len(callees) == 1:
                hop["callee_call_id"] = callees[0]
            if len(callers) > 1 or len(callees) > 1:
                self.counts["ambiguous_bridge_attribution"] += 1
            hops.append(hop)
        self.counts["missing_bridge_receive"] += sum(
            s.name in SENDS and s not in matched_sends for s in self.spans.values()
        )
        return sorted(hops, key=lambda h: h["id"])

    def run(self) -> HarnessReplay:
        claude_owners, claude_calls = self.claude()
        codex_owners, codex_calls = self.codex()
        owners = claude_owners + codex_owners
        self.emit(owners, claude_calls + codex_calls)
        hops = self.bridge(owners)
        for row in self.rows:
            if not row.source and not (row.kind == "trace" and row.name in BRIDGE_SPANS):
                self.counts["unsupported_source"] += 1
            elif row.source == CLAUDE:
                supported = (
                    {INTERACTION, LLM, TOOL, EXECUTION, PERMISSION}
                    if row.kind == "trace"
                    else CLAUDE_LOG_EVENTS
                )
                if row.name not in supported:
                    self.counts["unsupported_claude_signal"] += 1
                elif not row.scope:
                    self.counts["missing_claude_session"] += 1
        return HarnessReplay(
            [self.nodes[k] for k in sorted(self.nodes)],
            [self.edges[k] for k in sorted(self.edges)],
            {
                "counts": {k: v for k, v in sorted(self.counts.items()) if v},
                "hops": hops,
                "claude_cost_usd_by_call": dict(sorted(self.costs.items())),
                "codex_dollar_cost": "unknown",
                "content_enabled": self.content,
            },
        )


def normalize_harness(
    capture_id: str,
    *,
    traces: Iterable[Row] = (),
    logs: Iterable[Row] = (),
    include_content: bool = False,
) -> HarnessReplay:
    """Normalize one capture; reuse its ID when replaying the same evidence.

    Unsupported or unattributable evidence appears in coverage, not as guessed
    calls or edges. Only opt into content for an approved sanitized capture.
    """
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise ValueError("A nonempty capture ID is required")
    return _Replay(capture_id, traces, logs, include_content).run()
