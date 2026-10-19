#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Detect cognitive challenges in one session's trajectory.

The simple version: one model call per session. The model gets the trace, every step with
its id, and the definition of each challenge to look for, and answers with the detections as
JSON. ``detect`` is the only entry point, so a stronger method can replace it without
touching the worker.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# The trace is cut to about this many characters (roughly 30k tokens); each step gets an equal share.
MAX_TRACE_CHARS = 120_000
MIN_FIELD_CHARS = 300

SYSTEM_PROMPT = """\
You review traces of LLM-based multi-agent systems for cognitive challenges from a fixed taxonomy.

You get the trace, one numbered block per step with its id, and the definition of each challenge to \
look for. Report a challenge only when the trace itself shows what its detection heuristic asks for, \
and say where. Most traces show few challenges or none: an empty answer is normal and correct. Do not \
report a challenge for style, for minor imperfections, or because it merely could have happened."""


class DetectionItem(BaseModel):
    """One detection, as the model must report it. Its schema is what the model is asked to follow."""

    code: str = Field(description="Code of the detected challenge, as given in its definition, e.g. IC-2.")
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "high: the trace clearly meets every condition of the detection heuristic. "
            "medium: it meets the heuristic but one condition rests on inference. "
            "low: the evidence is plausible but weak or ambiguous."
        )
    )
    reasoning: str = Field(min_length=1, description="One or two sentences: what in the trace matches the challenge.")
    call_id: str | None = Field(
        default=None,
        description="Id of the step where it happened, or null if it only shows across the whole session.",
    )

    @field_validator("code", "reasoning", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("code", mode="after")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("confidence", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("call_id", mode="before")
    @classmethod
    def _no_step(cls, value: Any) -> Any:
        text = str(value).strip() if value is not None else ""
        return None if text in ("", "null", "None") else text


class DetectionAnswer(BaseModel):
    """What the model answers: the detections, possibly none."""

    detections: list[DetectionItem] = Field(description="The challenges the trace shows. Empty if none.")


class _Envelope(BaseModel):
    """The answer with its detections not yet checked, so one bad detection does not lose the others."""

    detections: list[Any]


ANSWER_FORMAT = (
    "Answer with JSON only, matching this JSON schema. Report each challenge at most once per step. "
    'No detection: {"detections": []}.\n\n' + json.dumps(DetectionAnswer.model_json_schema(), indent=2)
)


class DetectorError(RuntimeError):
    """The model's answer could not be used (not JSON, not the expected shape)."""


@dataclass(frozen=True)
class Detection:
    code: str
    confidence: str
    reasoning: str
    call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "confidence": self.confidence, "reasoning": self.reasoning, "call_id": self.call_id}


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


# system prompt, user prompt -> the model's answer
Complete = Callable[[str, str], Completion]


@dataclass(frozen=True)
class DetectionResult:
    detections: list[Detection]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return f"{text[:head]} …[{len(text) - head - tail} characters cut]… {text[-tail:]}", True


def render_trace(trajectory: Mapping[str, Any]) -> tuple[str, set[str], bool]:
    """The trace as text: a block per step. Returns the text, the step ids, and whether text was cut."""
    steps = trajectory.get("steps") or []
    limit = max(MIN_FIELD_CHARS, MAX_TRACE_CHARS // max(1, 2 * len(steps)))
    blocks, ids, truncated = [], set(), False
    for step in steps:
        call_id = str(step["call_id"])
        ids.add(call_id)
        label = f"{step['kind']} {step['name']}"
        if step.get("agent_id"):
            label += f" (agent: {step['agent_id']})"
        if step.get("success") is False:
            label += " [FAILED]"
        text_in, cut_in = _clip(step.get("input_text") or "", limit)
        text_out, cut_out = _clip(step.get("output_text") or "", limit)
        truncated = truncated or cut_in or cut_out
        blocks.append(f"[step {call_id}] {label}\nINPUT: {text_in}\nOUTPUT: {text_out}")
    return "\n\n".join(blocks), ids, truncated


def render_challenges(challenges: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for c in challenges:
        signs = "\n".join(f"- {s}" for s in c["signature"])
        blocks.append(
            f"### {c['code']} — {c['name']} ({c['locus']}, {c['function']})\n{c['summary']}\n"
            f"Signs:\n{signs}\nDetect when: {c['heuristic']}"
        )
    return "\n\n".join(blocks)


def parse_detections(text: str, codes: set[str], step_ids: set[str]) -> list[Detection]:
    """The valid detections of the model's answer. Raises ``DetectorError`` if it is not the expected JSON.

    A detection that does not fit ``DetectionItem`` (an unknown confidence, no reasoning, ...) or whose
    challenge was not asked for is dropped. A ``call_id`` that is not a step of the trace becomes ``None``
    (the detection stays, on the session).
    """
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        envelope = _Envelope.model_validate_json(match.group(0) if match else "")
    except ValidationError as exc:
        raise DetectorError(f"unusable answer ({exc.errors()[0]['msg']}): {(text or '')[:200]!r}") from exc

    detections: dict[tuple[str, str | None], Detection] = {}
    for raw in envelope.detections:
        try:
            item = DetectionItem.model_validate(raw)
        except ValidationError as exc:
            logger.warning("Dropping invalid detection %r: %s", raw, exc.errors()[0]["msg"])
            continue
        if item.code not in codes:
            logger.warning("Dropping detection of challenge %s, which was not asked for", item.code)
            continue
        call_id = item.call_id
        if call_id is not None and call_id not in step_ids:
            logger.warning("Detection %s names unknown step %r; keeping it on the session", item.code, call_id)
            call_id = None
        detections.setdefault((item.code, call_id), Detection(item.code, item.confidence, item.reasoning, call_id))
    return list(detections.values())


def detect(
    trajectory: Mapping[str, Any],
    challenges: Sequence[Mapping[str, Any]],
    complete: Complete,
) -> DetectionResult:
    """The cognitive challenges of ``challenges`` that the trajectory shows.

    Raises ``DetectorError`` if the model's answer cannot be used; the caller decides what that means.
    """
    trace, step_ids, truncated = render_trace(trajectory)
    if truncated:
        logger.info("Trace of session %s was cut to fit %d characters", trajectory.get("session_id"), MAX_TRACE_CHARS)
    user = f"## Challenges\n\n{render_challenges(challenges)}\n\n## Trace\n\n{trace}\n\n## Answer\n\n{ANSWER_FORMAT}"
    completion = complete(SYSTEM_PROMPT, user)
    detections = parse_detections(completion.text, {c["code"] for c in challenges}, step_ids)
    return DetectionResult(detections, completion.prompt_tokens, completion.completion_tokens, truncated)
