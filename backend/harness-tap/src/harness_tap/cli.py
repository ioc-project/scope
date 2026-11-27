#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Command line: hook | reconcile | flush | dump | install | uninstall | doctor."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import config as config_mod
from . import otlp
from .hooks import CLAUDE, CODEX, HookTap
from .spool import Spool


def _hook(args, cfg) -> int:
    # A hook must never break the harness: swallow everything, print nothing.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        HookTap(cfg).handle(args.harness, payload)
    except Exception as error:  # noqa: BLE001 - fail open by design
        try:
            cfg.state_dir.mkdir(parents=True, exist_ok=True)
            with open(cfg.state_dir / "hook-errors.log", "a") as log:
                log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {type(error).__name__}: {error}\n")
        except OSError:
            pass
    return 0


def _flush(cfg) -> dict:
    spool = Spool(cfg)
    sent, failed, detail = 0, 0, "nothing to send"
    for batch in spool.claim():
        spans = spool.read(batch)
        ok, detail = otlp.post(cfg, spans)
        if ok:
            sent += len(spans)
            batch.unlink(missing_ok=True)
        else:
            failed += len(spans)
            break  # keep order; retry on next flush
    status = {
        "time": time.time(),
        "sent": sent,
        "failed": failed,
        "detail": detail,
        "endpoint": cfg.endpoint,
    }
    spool.write_status("last_flush", status)
    return status


def _reconcile(args, cfg) -> int:
    from .codex_rollout import reconcile, reconcile_all

    try:
        if args.transcript:
            spans = reconcile(cfg, Path(args.transcript), args.session)
            result = {"spans": len(spans)}
        else:
            result = reconcile_all(cfg, Path(args.root) if args.root else None)
    except Exception as error:  # noqa: BLE001
        result = {"error": f"{type(error).__name__}: {error}"}
        if args.session:
            _fallback_from_hooks(cfg, args.session)
    if args.flush:
        result["flush"] = _flush(cfg)
    print(json.dumps(result, sort_keys=True))
    return 0 if "error" not in result else 1


def _fallback_from_hooks(cfg, session: str) -> None:
    """If rollout parsing fails, still emit turns recorded by hooks."""
    from .hooks import model_spans_from_hooks, turn_span

    spool = Spool(cfg)
    spans = []
    with spool.session_state(CODEX, session) as state:
        if not state.get("trace_id"):
            return
        for turn, info in state.get("turns", {}).items():
            if info.pop("pending_reconcile", False) and info.get("end_ns"):
                spans.append(
                    turn_span(
                        cfg, CODEX, session, state, turn, info["start_ns"], info["end_ns"], "hook"
                    )
                )
                spans += model_spans_from_hooks(CODEX, session, state, turn)
    spool.append(spans)


def _dump(args, cfg) -> int:
    spans = Spool(cfg).all_spans()
    if args.format == "otlp":
        print(json.dumps(otlp.export_request(cfg, spans), indent=2))
    else:
        for row in otlp.clickhouse_rows(cfg, spans):
            print(json.dumps(row, sort_keys=True))
    return 0


def _install(args, cfg) -> int:
    from . import install as inst

    changed = False
    if args.endpoint:
        cfg.endpoint, changed = args.endpoint, True
    for pair in args.resource or []:
        key, _, value = pair.partition("=")
        if key and value:
            cfg.resource[key], changed = value, True
    if args.capture_content is not None:
        cfg.capture_content, changed = args.capture_content == "on", True
    if args.claude_mode:
        cfg.claude_mode, changed = args.claude_mode, True
    if changed or not (cfg.home / "config.json").exists():
        if not args.dry_run:
            print(f"config: {config_mod.save(cfg)}")
    for harness in args.harness:
        path = inst.install(harness, args.scope, args.dry_run, not args.no_async)
        print(f"{'would update' if args.dry_run else 'updated'} {harness} hooks: {path}")
    if CODEX in args.harness:
        print(
            "Codex: run /hooks once in the Codex TUI to trust the new hooks; non-interactive "
            "runs (codex exec, agentbridge register) skip untrusted hooks."
        )
    return 0


def _uninstall(args, cfg) -> int:
    from . import install as inst

    for harness in args.harness:
        print(f"removed {harness} hooks from {inst.uninstall(harness, args.scope, args.dry_run)}")
    return 0


def _doctor(args, cfg) -> int:
    from . import install as inst
    from .codex_rollout import codex_sessions_dir

    spool = Spool(cfg)
    pending = spool.all_spans()
    sessions = []
    for path in sorted((cfg.state_dir / "sessions").glob("*.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        harness, _, session = path.stem.partition("--")
        sessions.append(
            {
                "harness": harness,
                "session": session,
                "traceparent_observed": bool(state.get("parent")),
                "turns": len(state.get("turns", {})),
                "pending_reconcile": sum(
                    bool(t.get("pending_reconcile")) for t in state.get("turns", {}).values()
                ),
                "reconciled_turns": len(state.get("reconciled_turns", [])),
            }
        )
    try:
        last = json.loads((cfg.state_dir / "last_flush.json").read_text())
    except (OSError, ValueError):
        last = None
    report = {
        "config": {**cfg.to_file_dict(), "home": str(cfg.home)},
        "hooks": {
            h: {s: inst.installed(h, s) for s in ("user", "project")} for h in (CODEX, CLAUDE)
        },
        "codex_sessions_dir": str(codex_sessions_dir()),
        "spooled_unsent_spans": len(pending),
        "last_flush": last,
        "sessions": sessions[-20:],
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-tap", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hook", help="read one hook payload on stdin (used by harness configs)")
    p.add_argument("--harness", required=True, choices=[CODEX, CLAUDE])

    p = sub.add_parser("reconcile", help="emit Codex spans from rollout files")
    p.add_argument("--transcript", help="one rollout file (default: all under CODEX_HOME)")
    p.add_argument("--session", help="session id to key spans by (from the hook payload)")
    p.add_argument("--root", help="override the Codex sessions directory")
    p.add_argument("--flush", action="store_true", help="export after reconciling")

    sub.add_parser("flush", help="export spooled spans over OTLP/HTTP")

    p = sub.add_parser("dump", help="print spooled spans (sent spans are not kept)")
    p.add_argument(
        "--format",
        choices=["rows", "otlp"],
        default="rows",
        help="rows = ClickHouse otel_traces shape (loadable by norm)",
    )

    for name in ("install", "uninstall"):
        p = sub.add_parser(name, help=f"{name} hooks in harness config files")
        p.add_argument("--harness", nargs="+", choices=[CODEX, CLAUDE], default=[CODEX, CLAUDE])
        p.add_argument("--scope", choices=["user", "project"], default="user")
        p.add_argument("--dry-run", action="store_true")
        if name == "install":
            p.add_argument("--endpoint", help="OTLP/HTTP base URL, e.g. http://localhost:4318")
            p.add_argument(
                "--resource",
                action="append",
                metavar="KEY=VALUE",
                help="extra resource attribute, e.g. ioc.agent.id=planner",
            )
            p.add_argument("--capture-content", choices=["on", "off"])
            p.add_argument("--claude-mode", choices=["anchor", "full"])
            p.add_argument(
                "--no-async", action="store_true", help="register Codex hooks without async: true"
            )

    sub.add_parser("doctor", help="show config, installed hooks, spool and session state")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_mod.load()
    if args.command == "hook":
        return _hook(args, cfg)
    if args.command == "reconcile":
        return _reconcile(args, cfg)
    if args.command == "flush":
        print(json.dumps(_flush(cfg), sort_keys=True))
        return 0
    return {"dump": _dump, "install": _install, "uninstall": _uninstall, "doctor": _doctor}[
        args.command
    ](args, cfg)
