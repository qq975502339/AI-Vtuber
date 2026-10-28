from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from person_sidecar.app import _runtime_worker


class FlakyRuntime:
    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[Exception] = []
        self.state = SimpleNamespace(game_state=None)

    async def drain_once(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("malformed event")

    def record_worker_failure(self, error: Exception) -> None:
        self.failures.append(error)


@pytest.mark.asyncio
async def test_worker_recovers_after_one_runtime_failure() -> None:
    runtime = FlakyRuntime()
    worker = asyncio.create_task(_runtime_worker(runtime))
    try:
        await asyncio.sleep(0.12)
    finally:
        worker.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await worker

    assert runtime.calls >= 2
    assert len(runtime.failures) == 1

class TickRuntime:
    def __init__(self) -> None:
        self.state = SimpleNamespace(game_state=SimpleNamespace(phase="answering"))
        self.events = []
        self.failures = []

    async def drain_once(self) -> None:
        return None

    async def publish(self, event) -> None:
        self.events.append(event)

    def record_worker_failure(self, error: Exception) -> None:
        self.failures.append(error)


@pytest.mark.asyncio
async def test_worker_publishes_timer_tick_for_answering_game() -> None:
    runtime = TickRuntime()
    worker = asyncio.create_task(_runtime_worker(runtime))
    try:
        await asyncio.sleep(0.06)
    finally:
        worker.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await worker

    assert any(event.type == "timer.tick" for event in runtime.events)
    assert runtime.failures == []