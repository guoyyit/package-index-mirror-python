"""In-process request de-duplication (single flight)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class SingleFlight:
    """Coalesces concurrent coroutines keyed by an arbitrary key."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()

    async def do(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.ensure_future(factory())
                self._inflight[key] = task
                leader = True
            else:
                leader = False

        try:
            if leader:
                return await task
            # shield(): cancelling one waiter must not cancel the shared work
            return await asyncio.shield(task)
        finally:
            if leader:
                async with self._lock:
                    self._inflight.pop(key, None)
