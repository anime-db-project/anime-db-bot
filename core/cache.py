from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from core.runtime_metrics import record_cache_access

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class _Entry(Generic[V]):
    expires_at: float
    value: V
    weight: int = 1


class BoundedTTLCache(Generic[K, V]):
    """Small fixed-TTL LRU cache for process-local data.

    Reads update LRU order but never extend the TTL. Expired entries are removed
    lazily, and insertion always enforces ``max_entries`` so a high-cardinality
    upstream cannot grow process memory without bound.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        name: str = "cache",
        max_weight: int | None = None,
        weigher: Callable[[V], int] | None = None,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.name = str(name or "cache").strip().casefold() or "cache"
        self.ttl_seconds = None if ttl_seconds is None else max(0.0, float(ttl_seconds))
        self._clock = clock
        self._items: OrderedDict[K, _Entry[V]] = OrderedDict()
        self.max_weight = None if max_weight is None else max(1, int(max_weight))
        self._weigher = weigher
        self._weight = 0

    def _prune_expired(self, now: float | None = None) -> None:
        if not self._items:
            return
        timestamp = self._clock() if now is None else now
        expired = [
            key for key, entry in self._items.items() if entry.expires_at <= timestamp
        ]
        for key in expired:
            entry = self._items.pop(key, None)
            if entry is not None:
                self._weight -= entry.weight
        if expired:
            logging.debug(
                "Cache entries expired",
                extra={
                    "event": "cache_expire",
                    "cache": self.name,
                    "evicted": len(expired),
                    "size": len(self._items),
                },
            )

    def set(self, key: K, value: V, *, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        if ttl is None:
            raise ValueError("TTL must be configured on the cache or set() call")
        if ttl <= 0:
            self.pop(key)
            return

        weight = (
            max(1, int(self._weigher(value))) if self._weigher is not None else 1
        )
        if self.max_weight is not None and weight > self.max_weight:
            self.pop(key)
            return

        now = self._clock()
        self._prune_expired(now)
        old = self._items.pop(key, None)
        if old is not None:
            self._weight -= old.weight
        self._items[key] = _Entry(expires_at=now + ttl, value=value, weight=weight)
        self._weight += weight
        self._items.move_to_end(key)
        evicted = 0
        while len(self._items) > self.max_entries or (
            self.max_weight is not None and self._weight > self.max_weight
        ):
            _, discarded = self._items.popitem(last=False)
            self._weight -= discarded.weight
            evicted += 1
        if evicted:
            logging.info(
                "Cache capacity eviction",
                extra={
                    "event": "cache_eviction",
                    "cache": self.name,
                    "evicted": evicted,
                    "size": len(self._items),
                },
            )

    def get(self, key: K, default: V | None = None) -> V | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: K) -> V:
        entry = self._items.get(key)
        if entry is None:
            record_cache_access(self.name, "miss")
            logging.debug(
                "Cache miss",
                extra={
                    "event": "cache_access",
                    "cache": self.name,
                    "cache_result": "miss",
                    "size": len(self._items),
                },
            )
            raise KeyError(key)
        if entry.expires_at <= self._clock():
            self._items.pop(key, None)
            self._weight -= entry.weight
            record_cache_access(self.name, "expired")
            logging.debug(
                "Cache entry expired",
                extra={
                    "event": "cache_access",
                    "cache": self.name,
                    "cache_result": "expired",
                    "size": len(self._items),
                },
            )
            raise KeyError(key)
        self._items.move_to_end(key)
        record_cache_access(self.name, "hit")
        logging.debug(
            "Cache hit",
            extra={
                "event": "cache_access",
                "cache": self.name,
                "cache_result": "hit",
                "size": len(self._items),
            },
        )
        return entry.value

    def __contains__(self, key: object) -> bool:
        try:
            self[key]  # type: ignore[index]
        except (KeyError, TypeError):
            return False
        return True

    def pop(self, key: K, default: V | None = None) -> V | None:
        entry = self._items.pop(key, None)
        if entry is None:
            return default
        self._weight -= entry.weight
        if entry.expires_at <= self._clock():
            return default
        return entry.value

    def clear(self) -> None:
        self._items.clear()
        self._weight = 0

    def __len__(self) -> int:
        self._prune_expired()
        return len(self._items)
