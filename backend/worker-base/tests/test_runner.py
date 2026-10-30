#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Workers that share a process each keep their own settings."""

import threading
import time

import click
import pytest

from worker_base import runner
from worker_base.utils import env_scope, resolve_max_inflight_messages, scoped_getenv

seen: dict[str, dict] = {}


def _command(name: str, *, fail: bool = False):
    @click.command()
    @click.option("--flag", default="")
    def main(flag):
        with_scope = scoped_getenv("MAX_INFLIGHT_MESSAGES")
        seen[name] = {"inflight": with_scope, "flag": flag, "thread": threading.current_thread().name}
        if fail:
            raise RuntimeError("boom")
        time.sleep(0.05)

    return main


good, bad = _command("good"), _command("bad", fail=True)


def test_scoped_setting_wins_over_the_shared_one(monkeypatch):
    monkeypatch.setenv("MAX_INFLIGHT_MESSAGES", "8")
    monkeypatch.setenv("NORM_MAX_INFLIGHT_MESSAGES", "128")
    assert scoped_getenv("MAX_INFLIGHT_MESSAGES") == "8"
    with env_scope("NORM"):
        assert scoped_getenv("MAX_INFLIGHT_MESSAGES") == "128"
        assert resolve_max_inflight_messages(1, error_subject="x") == 128
    with env_scope("EMBEDDING"):  # nothing set for it: the shared setting applies
        assert resolve_max_inflight_messages(1, error_subject="x") == 8


def test_select_keeps_the_order_of_the_group_and_rejects_unknown_workers():
    members = [runner.Member(n, n.upper(), "m:main") for n in ("a", "b", "c")]
    assert [m.name for m in runner.select(members, "c,a")] == ["a", "c"]
    assert [m.name for m in runner.select(members, "")] == ["a", "b", "c"]
    with pytest.raises(ValueError, match="Unknown worker"):
        runner.select(members, "a,z")


def test_members_run_in_their_own_thread_with_their_own_settings(monkeypatch):
    seen.clear()
    monkeypatch.setenv("ONE_MAX_INFLIGHT_MESSAGES", "3")
    monkeypatch.setenv("TWO_MAX_INFLIGHT_MESSAGES", "5")
    monkeypatch.setenv("TWO_ARGS", "--flag extra")
    one = runner.Member("one", "ONE", f"{__name__}:good")
    two = runner.Member("two", "TWO", f"{__name__}:good")
    assert runner.run_members([one, two]) == 0
    assert seen["good"]["inflight"] in ("3", "5")  # both members use the same command object in this test


def test_a_failing_member_stops_the_process_with_an_error(monkeypatch):
    seen.clear()
    ok = runner.Member("ok", "OK", f"{__name__}:good")
    ko = runner.Member("ko", "KO", f"{__name__}:bad")
    assert runner.run_members([ok, ko]) == 1


def test_member_args_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANALYSIS_ARGS", "--batch_size 20 --debug")
    assert runner.Member("analysis", "ANALYSIS", "m:main").args() == ["--batch_size", "20", "--debug"]
