from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Generic, TypeVar
from core.cache import BoundedTTLCache

K = TypeVar("K")
V = TypeVar("V")


class AsyncSnapshotCache(Generic[K, V]):
    def __init__(self, *, entries: int, ttl: float, name: str):
        self.cache = BoundedTTLCache[K, V](
            max_entries=entries, ttl_seconds=ttl, name=name
        )
        self.ttl = ttl
        self.pending: dict[K, tuple[asyncio.Task[V], int]] = {}

    async def get(
        self,
        key: K,
        loader: Callable[[], Awaitable[V]],
        *,
        cacheable: Callable[[V], bool] | None = None,
    ) -> V:
        if self.ttl <= 0:
            return await loader()
        cached = self.cache.get(key)
        if cached is not None:
            return deepcopy(cached)
        existing = self.pending.get(key)
        if existing is None:

            async def run() -> V:
                result = await loader()
                if cacheable is None or cacheable(result):
                    self.cache.set(key, deepcopy(result))
                return result

            task = asyncio.create_task(run())
            self.pending[key] = (task, 1)
        else:
            task, waiters = existing
            self.pending[key] = (task, waiters + 1)
        try:
            return deepcopy(await asyncio.shield(task))
        finally:
            entry = self.pending.get(key)
            if entry is not None and entry[0] is task:
                if entry[1] <= 1:
                    self.pending.pop(key, None)
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                else:
                    self.pending[key] = (task, entry[1] - 1)
