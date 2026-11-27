#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Configuration: defaults <- ~/.harness-tap/config.json <- environment.

Harnesses do not reliably pass OTEL_* variables to hook processes (Claude
Code explicitly does not), so the config file written by `install` is the
primary source; environment variables only override it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

DEFAULT_ENDPOINT = "http://localhost:4318"


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _pairs(value: str) -> Dict[str, str]:
    """Parse OTEL-style 'k=v,k2=v2' lists."""
    result = {}
    for item in value.split(","):
        if "=" in item:
            key, _, val = item.partition("=")
            if key.strip():
                result[key.strip()] = val.strip()
    return result


def tap_home() -> Path:
    return Path(os.environ.get("HARNESS_TAP_HOME") or Path.home() / ".harness-tap")


@dataclass
class Config:
    endpoint: str = DEFAULT_ENDPOINT
    headers: Dict[str, str] = field(default_factory=dict)
    service_name: str = "harness-tap"
    # Extra resource attributes, e.g. ioc.agent.id / ioc.mas.id / ioc.session.id.
    resource: Dict[str, str] = field(default_factory=dict)
    capture_content: bool = False
    content_max_chars: int = 4000
    # "anchor": session + turn spans only (Claude already exports model/tool spans).
    # "full": also tool spans from hooks.
    claude_mode: str = "anchor"
    export_timeout_s: float = 5.0
    home: Path = field(default_factory=tap_home)

    @property
    def spool_dir(self) -> Path:
        return self.home / "spool"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    def to_file_dict(self) -> dict:
        data = asdict(self)
        data.pop("home")
        return data


def load(home: Optional[Path] = None) -> Config:
    cfg = Config(home=home or tap_home())
    path = cfg.home / "config.json"
    if path.is_file():
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            stored = {}
        for key, value in stored.items():
            if key in Config.__dataclass_fields__ and key != "home":
                setattr(cfg, key, value)
    env = os.environ
    if env.get("HARNESS_TAP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        cfg.endpoint = env.get("HARNESS_TAP_ENDPOINT") or env["OTEL_EXPORTER_OTLP_ENDPOINT"]
    if env.get("OTEL_EXPORTER_OTLP_HEADERS"):
        cfg.headers.update(_pairs(env["OTEL_EXPORTER_OTLP_HEADERS"]))
    if env.get("OTEL_RESOURCE_ATTRIBUTES"):
        cfg.resource.update(_pairs(env["OTEL_RESOURCE_ATTRIBUTES"]))
    for key, attr in (
        ("IOC_AGENT_ID", "ioc.agent.id"),
        ("IOC_SESSION_ID", "ioc.session.id"),
        ("IOC_MAS_ID", "ioc.mas.id"),
    ):
        if env.get(key):
            cfg.resource[attr] = env[key]
    if env.get("HARNESS_TAP_CAPTURE_CONTENT"):
        cfg.capture_content = _bool(env["HARNESS_TAP_CAPTURE_CONTENT"])
    if env.get("HARNESS_TAP_CLAUDE_MODE") in {"anchor", "full"}:
        cfg.claude_mode = env["HARNESS_TAP_CLAUDE_MODE"]
    return cfg


def save(cfg: Config) -> Path:
    cfg.home.mkdir(parents=True, exist_ok=True)
    path = cfg.home / "config.json"
    path.write_text(json.dumps(cfg.to_file_dict(), indent=2, sort_keys=True) + "\n")
    return path
