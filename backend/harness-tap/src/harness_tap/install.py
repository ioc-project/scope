#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Register / remove tap hooks in Codex and Claude Code config files.

Merging is idempotent and only touches entries whose command contains
MARKER; everything else in the user's file is preserved byte-for-byte in
meaning (re-serialised JSON). A one-time backup is written beside the file.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .hooks import CLAUDE, CODEX

MARKER = "-m harness_tap hook"

CODEX_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreModelRequest",
    "PostModelRequest",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
    "SessionEnd",
]
CLAUDE_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "SubagentStop",
    "Stop",
    "SessionEnd",
]
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PermissionRequest"}


def hook_command(harness: str, python: Optional[str] = None) -> str:
    src = Path(__file__).resolve().parents[1]
    return (
        f"env PYTHONPATH={shlex.quote(str(src))} {shlex.quote(python or sys.executable)} "
        f"{MARKER} --harness {harness}"
    )


def config_path(harness: str, scope: str, cwd: Optional[Path] = None) -> Path:
    base = Path.home() if scope == "user" else (cwd or Path.cwd())
    if harness == CODEX:
        return base / ".codex" / "hooks.json"
    return base / ".claude" / ("settings.json" if scope == "user" else "settings.local.json")


def _entry(harness: str, event: str, command: str, use_async: bool) -> Dict:
    hook: Dict = {"type": "command", "command": command}
    if harness == CODEX:
        # Codex caps SessionEnd/Interrupt at 3 s; the tap only appends to a local file.
        hook["timeout"] = 3 if event in {"SessionEnd", "Interrupt"} else 5
        if use_async:
            hook["async"] = True
        return {"hooks": [hook]}
    hook["timeout"] = 5
    entry: Dict = {"hooks": [hook]}
    if event in TOOL_EVENTS:
        entry["matcher"] = "*"
    return entry


def _strip_ours(groups: List) -> List:
    kept = []
    for group in groups if isinstance(groups, list) else []:
        hooks = group.get("hooks", []) if isinstance(group, dict) else []
        if any(MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict)):
            continue
        kept.append(group)
    return kept


def merged(existing: Dict, harness: str, command: str, use_async: bool = True) -> Dict:
    data = dict(existing)
    hooks = dict(data.get("hooks") or {})
    for event in CODEX_EVENTS if harness == CODEX else CLAUDE_EVENTS:
        hooks[event] = _strip_ours(hooks.get(event, [])) + [
            _entry(harness, event, command, use_async)
        ]
    data["hooks"] = hooks
    return data


def removed(existing: Dict) -> Dict:
    data = dict(existing)
    hooks = {}
    for event, groups in (data.get("hooks") or {}).items():
        kept = _strip_ours(groups)
        if kept:
            hooks[event] = kept
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return data


def _load(path: Path) -> Dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text() or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def apply(path: Path, data: Dict, dry_run: bool) -> str:
    text = json.dumps(data, indent=2) + "\n"
    if dry_run:
        return text
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".harness-tap.bak")
    if path.exists() and not backup.exists():
        backup.write_text(path.read_text())
    path.write_text(text)
    return text


def install(
    harness: str,
    scope: str = "user",
    dry_run: bool = False,
    use_async: bool = True,
    cwd: Optional[Path] = None,
) -> Path:
    path = config_path(harness, scope, cwd)
    data = merged(_load(path), harness, hook_command(harness), use_async)
    apply(path, data, dry_run)
    return path


def uninstall(
    harness: str, scope: str = "user", dry_run: bool = False, cwd: Optional[Path] = None
) -> Path:
    path = config_path(harness, scope, cwd)
    if path.exists():
        apply(path, removed(_load(path)), dry_run)
    return path


def installed(harness: str, scope: str = "user", cwd: Optional[Path] = None) -> List[str]:
    try:
        data = _load(config_path(harness, scope, cwd))
    except (OSError, ValueError):
        return []
    return sorted(
        event
        for event, groups in (data.get("hooks") or {}).items()
        if len(_strip_ours(groups)) != len(groups if isinstance(groups, list) else [])
    )


__all__ = ["CLAUDE", "CODEX", "install", "uninstall", "installed", "merged", "removed"]
