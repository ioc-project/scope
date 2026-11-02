#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""A worker runs as many blocking handlers at once as it is allowed messages in flight."""

import asyncio
import threading
import time

import pytest

from worker_base.base_worker import BaseWorker


async def _peak_concurrency(tasks: int) -> int:
    """The most ``asyncio.to_thread`` calls that were running at the same time."""
    running = peak = 0
    lock = threading.Lock()

    def blocking() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.15)
        with lock:
            running -= 1

    await asyncio.gather(*(asyncio.to_thread(blocking) for _ in range(tasks)))
    return peak


@pytest.mark.asyncio
async def test_the_default_pool_of_the_event_loop_is_smaller_than_the_inflight_limit():
    """What the workers had: the pool is min(32, cpus + 4), whatever the limit."""
    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=4))

    assert await _peak_concurrency(24) <= 4


@pytest.mark.asyncio
async def test_the_pool_follows_max_inflight_messages():
    worker = BaseWorker(rabbitmq_url="amqp://localhost", input_queue="q", max_inflight_messages=24)

    worker._size_thread_pool()

    assert await _peak_concurrency(24) == 24


@pytest.mark.asyncio
async def test_one_message_in_flight_runs_one_handler():
    worker = BaseWorker(rabbitmq_url="amqp://localhost", input_queue="q", max_inflight_messages=1)

    worker._size_thread_pool()

    assert await _peak_concurrency(5) == 1
