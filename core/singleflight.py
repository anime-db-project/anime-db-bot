from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class SingleFlight(Generic[K, V]):
    """Deduplicate concurrent identical requests within one event loop.

    Each caller can cancel its own wait without aborting the shared operation.
    Completed tasks are removed immediately, including failures.
    """

    def __init__(self) -> None:
        self._tasks: dict[K, asyncio.Task[V]] = {}

    async def run(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._tasks[key] = task

            def forget(done: asyncio.Task[V]) -> None:
                if self._tasks.get(key) is done:
                    self._tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Consume errors if every caller left.

            task.add_done_callback(forget)
        return await asyncio.shield(task)
