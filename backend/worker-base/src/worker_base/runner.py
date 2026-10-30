#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Run several workers in one process, to share the interpreter and the libraries they have in common.

Each worker keeps its own command line, queues and configuration: a member is the ``main`` click command of the
worker's CLI, started in a thread of its own with its own event loop (``asyncio.run`` is what the CLI does anyway). A
few settings that all the workers read from the same variable can be set for one of them with its prefix:
``NORM_MAX_INFLIGHT_MESSAGES`` rather than ``MAX_INFLIGHT_MESSAGES`` (see ``worker_base.utils.env_scope``), and
``<PREFIX>_ARGS`` adds command line options for one member (``ANALYSIS_ARGS="--batch_size 20"``).

A member that is CPU-bound and forks processes of its own (the analysis worker) is run as a child process instead
(``isolated``): forking from a process that has other threads is not safe.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from worker_base.utils import env_scope

logger = logging.getLogger(__name__)

# The settings that every worker reads from one variable, and that can be set per worker with the member's prefix.
SCOPED_SETTINGS = ("MAX_INFLIGHT_MESSAGES", "PERIODIC_TRIGGER_INTERVAL_SECONDS")


@dataclass(frozen=True)
class Member:
    """A worker of the group: ``entry`` is ``"package.cli:main"``, the click command of its CLI."""

    name: str
    prefix: str
    entry: str
    isolated: bool = False

    def args(self) -> list[str]:
        return shlex.split(os.getenv(f"{self.prefix}_ARGS", ""))

    def load(self) -> Callable:
        module, _, attribute = self.entry.partition(":")
        return getattr(importlib.import_module(module), attribute)


def _run_in_thread(member: Member, command: Callable, errors: dict[str, BaseException]) -> None:
    with env_scope(member.prefix):
        try:
            command.main(args=member.args(), prog_name=member.name, standalone_mode=False)
        except BaseException as exc:  # noqa: BLE001 - reported to the supervisor, which stops the process
            logger.exception("Worker %s stopped with an error", member.name)
            errors[member.name] = exc


def _isolated_environment(member: Member) -> dict[str, str]:
    env = dict(os.environ)
    for setting in SCOPED_SETTINGS:
        value = os.getenv(f"{member.prefix}_{setting}")
        if value not in (None, ""):
            env[setting] = value
    return env


def _start_isolated(member: Member) -> subprocess.Popen:
    module, _, attribute = member.entry.partition(":")
    code = f"import sys; from {module} import {attribute} as main; main(sys.argv[1:], prog_name={member.name!r})"
    return subprocess.Popen(
        [sys.executable, "-c", code, *member.args()],
        env=_isolated_environment(member),
    )


def select(members: Sequence[Member], names: str | Sequence[str] | None) -> list[Member]:
    """The members called ``names`` (a comma separated string or a list), in the order of the group; all by default."""
    if isinstance(names, str):
        names = [name for name in names.split(",")]
    wanted = [name.strip() for name in names or [] if name.strip()]
    if not wanted:
        return list(members)
    known = {member.name for member in members}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise ValueError(f"Unknown worker(s) {', '.join(unknown)}: this image runs {', '.join(sorted(known))}")
    return [member for member in members if member.name in wanted]


def run_members(members: Sequence[Member]) -> int:
    """Run the members until one of them fails or all of them have stopped. Returns the exit code."""
    commands = {member.name: member.load() for member in members if not member.isolated}  # import errors show up now

    errors: dict[str, BaseException] = {}
    threads = [
        threading.Thread(
            target=_run_in_thread, args=(member, commands[member.name], errors), name=member.name, daemon=True
        )
        for member in members
        if not member.isolated
    ]
    children = {member.name: _start_isolated(member) for member in members if member.isolated}

    def _terminate(signum, frame):  # the orchestrator stopped the container
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)

    for thread in threads:
        thread.start()
    logger.info("Running workers: %s", ", ".join(member.name for member in members))

    code = 0
    try:
        stop = threading.Event()
        while not stop.wait(0.5):
            if errors:
                code = 1
                break
            exited = {name: child.poll() for name, child in children.items() if child.poll() is not None}
            failed = {name: rc for name, rc in exited.items() if rc != 0}
            if failed:
                logger.error("Worker(s) %s exited with %s", ", ".join(failed), failed)
                code = 1
                break
            if not any(thread.is_alive() for thread in threads) and len(exited) == len(children):
                break
    except (KeyboardInterrupt, SystemExit) as exc:
        logger.info("Stopping workers")
        code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 0
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
    return code


def main(group: str, members: Sequence[Member], argv: Sequence[str] | None = None) -> int:
    """Command line of a group of workers: ``--workers norm,embedding`` picks some of them (``<GROUP>_WORKERS``)."""
    env_name = f"{group.upper().replace('-', '_')}_WORKERS"
    parser = argparse.ArgumentParser(
        prog=f"{group}-worker-cli",
        description=f"Run the {group} workers in one process: {', '.join(member.name for member in members)}.",
    )
    parser.add_argument(
        "--workers",
        default=os.getenv(env_name, ""),
        help=f"comma separated workers to run (default: all of them; env: {env_name})",
    )
    parser.add_argument("--test", action="store_true", help="print 'I'm Alive' and exit")
    args = parser.parse_args(argv)
    if args.test:
        print("I'm Alive")
        return 0
    try:
        chosen = select(members, args.workers)
    except ValueError as exc:
        parser.error(str(exc))
    return run_members(chosen)
