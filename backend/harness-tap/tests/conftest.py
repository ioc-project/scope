#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from harness_tap.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TAP_NO_BACKGROUND", "1")
    return Config(home=tmp_path / "tap")


class Clock:
    def __init__(self, start=1_795_611_000_000_000_000):
        self.now = start

    def __call__(self):
        self.now += 100_000_000  # each event 100 ms after the previous one
        return self.now


@pytest.fixture
def clock():
    return Clock()


def by_name(spans, prefix):
    return [s for s in spans if s.name.startswith(prefix)]
