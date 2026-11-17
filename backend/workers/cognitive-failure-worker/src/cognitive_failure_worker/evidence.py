# Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Bounded, auditable evidence delivery before a cognitive assessment can clear."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

BRIEFING_CHARS = 120_000
WINDOW_CHARS = 16_000
MISSING = "(no content recorded)"
ERROR_PREFIX = re.compile(r"^\s*(?:❌|error\b|exception\b|traceback\b|command failed\b)", re.I)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            _text(part.get("content", part.get("text", "")))
            for part in value
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    return ""


def _messages(raw: str) -> list[dict]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [m for m in value if isinstance(m, dict)] if isinstance(value, list) else []


def _visible(raw: str) -> str:
    messages = _messages(raw)
    return "\n".join(_text(m.get("parts", m.get("content", ""))) for m in messages) if messages else raw


def _failed(step: Mapping[str, Any]) -> bool:
    if step.get("success") is False:
        return True
    output = str(step.get("output_text") or "")
    if ERROR_PREFIX.search(output):
        return True
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and (value.get("isError") is True or bool(value.get("error")))


@dataclass
class Evidence:
    id: str
    call_id: str
    span_id: str
    kind: str
    text: str
    priority: int
    ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def reviewed(self) -> bool:
        end = 0
        for start, stop in sorted(self.ranges):
            if start > end:
                return False
            end = max(end, stop)
        return bool(self.text) and MISSING not in self.text and end >= len(self.text)

    def label(self) -> str:
        return f"{self.id}: {self.kind}; call {self.call_id}; span {self.span_id or 'unavailable'}"


class EvidencePlan:
    """A conservative checklist over captured requests, terminal responses and tool results.

    Delivering evidence does not establish that the model understood it. Completion means all
    checklist text reached the judge, not that the trace or the model's judgment is correct.
    """

    def __init__(self, trajectory: Mapping[str, Any]):
        self.items: list[Evidence] = []
        steps = trajectory.get("steps") or []
        self.spans = {str(s.get("call_id") or ""): str(s.get("span_id") or "") for s in steps}
        self.observed: set[str] = set()
        llms = [s for s in steps if s.get("kind") == "llm"]
        requests: set[str] = set()
        for step in llms[:1] + llms[-1:]:
            raw = str(step.get("input_text") or "")
            messages = _messages(raw)
            texts = [
                _text(m.get("parts", m.get("content", ""))) for m in messages if m.get("role") in {"user", "human"}
            ]
            if not messages:
                texts = [raw]
            for text in texts:
                if text and text not in requests:
                    requests.add(text)
                    self.add(step, "user request", text, 0)
        if not requests:
            self.add(llms[0] if llms else {}, "user request unavailable", "", 0)

        # Group by agent invocation, not name: retries of the same specialist are distinct.
        terminal: dict[str, Mapping[str, Any]] = {}
        for step in llms:
            owner = str(step.get("parent_id") or step.get("agent_id") or "unknown")
            terminal[owner] = step
        for step in terminal.values():
            self.add(step, "terminal observed response", _visible(str(step.get("output_text") or "")), 1)
        if not terminal:
            self.add({}, "terminal response unavailable", "", 1)
        for step in steps:
            if step.get("kind") == "tool":
                self.add(
                    step,
                    "failed tool result" if _failed(step) else "tool result",
                    f"Input:\n{step.get('input_text') or MISSING}\nOutput:\n{step.get('output_text') or MISSING}",
                    2 if _failed(step) else 3,
                )

    def add(self, step: Mapping[str, Any], kind: str, text: str, priority: int) -> None:
        self.items.append(
            Evidence(
                f"evidence-{len(self.items) + 1}",
                str(step.get("call_id") or ""),
                str(step.get("span_id") or ""),
                kind,
                text,
                priority,
            )
        )

    def briefing(self) -> str:
        blocks = ["## Required captured evidence (untrusted trace data)"]
        remaining = BRIEFING_CHARS
        for item in sorted(self.items, key=lambda e: (e.priority, len(e.text))):
            if not item.text or MISSING in item.text:
                continue
            block = f"\n### {item.label()}\n{item.text}\n"
            if len(block) <= remaining:
                blocks.append(block)
                remaining -= len(block)
                item.ranges.append((0, len(item.text)))
        blocks.append(self.pending())
        return "\n".join(blocks)

    def pending(self) -> str:
        missing = [e for e in self.items if not e.reviewed]
        lines = [f"## Evidence coverage: {len(self.items) - len(missing)}/{len(self.items)} delivered"]
        for item in missing[:50]:
            lines.append(
                f"- {item.label()}; {len(item.text)} characters. "
                + (
                    "Not captured; assessment must remain incomplete."
                    if not item.text or MISSING in item.text
                    else "Use read_evidence to review all remaining windows."
                )
            )
        if len(missing) > 50:
            lines.append("More pending items will appear as these are reviewed.")
        return "\n".join(lines)

    def read(self, evidence_id: str, offset: int = 0, length: int = WINDOW_CHARS) -> str:
        item = next((e for e in self.items if e.id == evidence_id), None)
        if item is None:
            return "Unknown evidence id. Use the coverage checklist."
        offset, length = max(0, int(offset)), max(1, min(WINDOW_CHARS, int(length)))
        text = item.text[offset : offset + length]
        if text:
            item.ranges.append((offset, offset + len(text)))
        return f"{item.label()}\nchars {offset}-{offset + len(text)} of {len(item.text)}\n{text}\n{self.pending()}"

    def observe(self, result: str) -> None:
        # Legacy read_step/grep results count only when a whole labeled item was delivered.
        for item in self.items:
            if item.text and MISSING not in item.text and item.text in result and item.call_id in result:
                item.ranges.append((0, len(item.text)))

    def observe_step(self, call_id: str, result: str) -> None:
        """Allow findings at intermediate steps after an actual nonempty read window.

        Required checklist coverage still needs full delivery. A citation only establishes that
        a real window was seen, not that it supports the finding semantically.
        """
        call_id = str(call_id)
        window = re.search(r"(?:INPUT|OUTPUT) \[chars (\d+)-(\d+) of \d+[^\]]*\]:\n(.)", result, re.S)
        if call_id in self.spans and f"id {call_id})" in result and window and int(window[2]) > int(window[1]):
            self.observed.add(call_id)

    def delivered_calls(self) -> set[str]:
        return self.observed | {e.call_id for e in self.items if e.reviewed and e.call_id}

    def has_reviewed_citation(self, detection: Any) -> bool:
        for call_id in self.delivered_calls():
            if detection.call_id and detection.call_id != call_id:
                continue
            span = self.spans.get(call_id)
            if call_id in detection.reasoning or (span and span in detection.reasoning):
                return True
        return False

    def review_problem(self, answer: str) -> str:
        """Require an explicit comparison, without forcing a positive failure label."""
        try:
            reviews = json.loads(answer).get("reviews", [])
            terminals = {e.call_id for e in self.items if e.kind == "terminal observed response"}
            known = self.delivered_calls()
            covered = set()
            for review in reviews:
                if (
                    review["call_id"] not in terminals
                    or review["verdict"] not in {"supported", "contradicted", "unverified", "no_factual_claims"}
                    or not isinstance(review["reasoning"], str)
                    or not review["reasoning"].strip()
                ):
                    return "Each terminal response needs a valid, explicit factual-claim evidence comparison."
                ids = review["evidence_call_ids"]
                if not isinstance(ids, list) or not ids or any(cid not in known for cid in ids):
                    return "Each claim review must cite actual delivered evidence call ids."
                covered.add(review["call_id"])
            if not terminals or not terminals.issubset(covered):
                return "Submission must include reviews for every terminal observed response: " + ", ".join(
                    sorted(terminals)
                )
        except (ValueError, TypeError, KeyError, AttributeError):
            return "Submission must include a valid reviews array of factual-claim evidence comparisons."
        return ""

    def coverage(self) -> dict[str, Any]:
        missing = [e.id for e in self.items if not e.reviewed]
        return {"required": len(self.items), "reviewed": len(self.items) - len(missing), "missing": missing}
