#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Span records and the content policy.

Attribute names follow the OpenTelemetry GenAI conventions where one exists
(gen_ai.operation.name, gen_ai.usage.*, gen_ai.tool.*), and use the ioc.*
namespace for everything else, so norm can tell tap evidence from native.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config

# Codex usage keys (rollout token_usage_record / token_count, PostModelRequest).
USAGE_ATTRS = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_input_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_write_input_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "reasoning_output_tokens": "ioc.usage.reasoning_output_tokens",
    "total_tokens": "ioc.usage.total_tokens",
}


@dataclass
class SpanRecord:
    harness: str
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    start_ns: int
    end_ns: int
    attributes: Dict[str, Any] = field(default_factory=dict)
    kind: str = "internal"
    status: str = "unset"  # unset | ok | error
    status_message: str = ""
    links: List[Dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "SpanRecord":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def usage_attributes(usage: Any) -> Dict[str, int]:
    """Map a Codex usage object onto attributes; ignore anything non-integral."""
    if not isinstance(usage, dict):
        return {}
    result = {}
    for key, attr in USAGE_ATTRS.items():
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0 and float(value).is_integer():
            result[attr] = int(value)
    return result


def text_length(value: Any) -> Optional[int]:
    if isinstance(value, str):
        return len(value)
    if value is None:
        return None
    return len(json.dumps(value, ensure_ascii=False, default=str))


def content(cfg: Config, value: Any) -> Optional[str]:
    """Return value as text only when content capture is enabled; truncate it."""
    if not cfg.capture_content or value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > cfg.content_max_chars:
        text = text[: cfg.content_max_chars] + "…[truncated]"
    return text


def messages(cfg: Config, role: str, value: Any) -> Optional[str]:
    """GenAI-convention message envelope, only when content capture is on."""
    text = content(cfg, value)
    if text is None:
        return None
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": text}]}])
