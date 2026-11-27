#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Local-first storage: an append-only span spool plus small JSON state files.

Every span is written to disk before any network export (as in Trajectory's
design), so a sleeping hub or an offline laptop loses nothing; `flush` ships
the spool later. An `emitted` index records span IDs already spooled so the
hook path and the rollout reconciler never emit the same entity twice.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator, List, Set

from .config import Config
from .spans import SpanRecord

try:  # POSIX advisory locks; Windows falls back to unlocked best effort.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        if fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120] or "_"


class Spool:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pending = cfg.spool_dir / "pending.jsonl"
        self.emitted_path = cfg.state_dir / "emitted.txt"
        self.lock_path = cfg.state_dir / ".spool.lock"

    # ---- spans -----------------------------------------------------------
    def emitted(self) -> Set[str]:
        try:
            return set(self.emitted_path.read_text().split())
        except OSError:
            return set()

    def append(self, spans: Iterable[SpanRecord]) -> List[SpanRecord]:
        """Spool spans not emitted before; return the ones actually written."""
        with locked(self.lock_path):
            seen = self.emitted()
            fresh = []
            for span in spans:
                key = f"{span.trace_id}:{span.span_id}"
                if key not in seen:
                    seen.add(key)
                    fresh.append(span)
            if fresh:
                self.pending.parent.mkdir(parents=True, exist_ok=True)
                with open(self.pending, "a") as out:
                    for span in fresh:
                        out.write(json.dumps(span.to_json(), sort_keys=True) + "\n")
                with open(self.emitted_path, "a") as idx:
                    idx.write("".join(f"{s.trace_id}:{s.span_id}\n" for s in fresh))
            return fresh

    def claim(self) -> List[Path]:
        """Move pending spans aside for export; return all unsent batches."""
        with locked(self.lock_path):
            if self.pending.exists() and self.pending.stat().st_size:
                batch = self.cfg.spool_dir / f"batch-{time.time_ns()}-{os.getpid()}.jsonl"
                self.pending.rename(batch)
            return sorted(self.cfg.spool_dir.glob("batch-*.jsonl"))

    @staticmethod
    def read(path: Path) -> List[SpanRecord]:
        spans = []
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    spans.append(SpanRecord.from_json(json.loads(line)))
                except (ValueError, TypeError):
                    continue
        return spans

    def all_spans(self) -> List[SpanRecord]:
        paths = sorted(self.cfg.spool_dir.glob("batch-*.jsonl"))
        if self.pending.exists():
            paths.append(self.pending)
        return [span for path in paths for span in self.read(path)]

    # ---- per-session hook state ---------------------------------------------
    def session_path(self, harness: str, session: str) -> Path:
        return self.cfg.state_dir / "sessions" / f"{_safe(harness)}--{_safe(session)}.json"

    @contextlib.contextmanager
    def session_state(self, harness: str, session: str) -> Iterator[dict]:
        path = self.session_path(harness, session)
        with locked(path.with_suffix(".lock")):
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = {}
            yield state
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, sort_keys=True))
            tmp.replace(path)

    def read_session(self, harness: str, session: str) -> dict:
        try:
            return json.loads(self.session_path(harness, session).read_text())
        except (OSError, ValueError):
            return {}

    def write_status(self, name: str, data: dict) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / f"{name}.json").write_text(json.dumps(data, sort_keys=True))
