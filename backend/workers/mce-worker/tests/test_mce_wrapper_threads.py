#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""The metrics of several sessions are computed at the same time, not one after the other."""

import asyncio
import time

import pytest

from mce_worker.wrapper.mce_wrapper import MCEWrapper


def _wrapper(duration: float) -> MCEWrapper:
    wrapper = MCEWrapper.__new__(MCEWrapper)  # the service behind it is not needed

    def slow_session(session_id: str) -> list:
        time.sleep(duration)  # a blocking evaluation, as with the metrics and the judge model
        return [{"session": session_id}]

    wrapper.process_session = slow_session
    return wrapper


@pytest.mark.asyncio
async def test_sessions_are_computed_concurrently():
    wrapper = _wrapper(duration=0.3)

    started = time.monotonic()
    results = await asyncio.gather(*(wrapper.process_mce_session(f"s{i}") for i in range(6)))
    elapsed = time.monotonic() - started

    assert [r["session_metrics"][0]["session"] for r in results] == [f"s{i}" for i in range(6)]
    assert elapsed < 0.3 * 6 / 2, f"took {elapsed:.2f}s: the sessions ran one after the other"


@pytest.mark.asyncio
async def test_the_event_loop_stays_free_during_an_evaluation():
    wrapper = _wrapper(duration=0.3)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await wrapper.process_mce_session("s1")
    task.cancel()

    assert ticks >= 5
