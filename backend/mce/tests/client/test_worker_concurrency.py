#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Several sessions can be computed at once: the engine they share is built only once."""

from __future__ import annotations

import threading
import time

from mce.client.worker import MCEWorkerService


def _service() -> MCEWorkerService:
    service = MCEWorkerService.__new__(
        MCEWorkerService
    )  # no config or knowledge graph needed
    service._restricted_engine = None
    service._engine_lock = threading.Lock()
    return service


def test_threads_asking_at_once_build_the_engine_once(monkeypatch) -> None:
    service = _service()
    built: list[object] = []

    def build() -> object:
        time.sleep(0.1)  # long enough for the other threads to arrive
        engine = object()
        built.append(engine)
        return engine

    monkeypatch.setattr(service, "_build_restricted_engine", build)
    engines: list[object] = []
    threads = [
        threading.Thread(
            target=lambda: engines.append(service._get_restricted_engine())
        )
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 1
    assert len({id(engine) for engine in engines}) == 1 and len(engines) == 8
