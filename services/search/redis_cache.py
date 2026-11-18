from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from core.models import SearchCandidate, SearchPage, SearchResultSet
from core.settings import CacheBackendSettings


class RedisSearchCache:
    """Optional JSON-only shared search cache; never stores Python pickles.

    The Redis client is deliberately best-effort: a transient outage must
    neither lose persistent data nor interrupt searching.
    """

    _VERSION = 1

    def __init__(
        self,
        settings: CacheBackendSettings,
        *,
        ttl_seconds: float,
        provider_names: tuple[str, ...],
        client: Any = None,
    ) -> None:
        self._ttl = max(0, int(ttl_seconds))
        self._timeout = settings.redis_timeout_seconds
        self._failure_cooldown_seconds = settings.redis_failure_cooldown_seconds
        self._provider_hash = hashlib.sha256(
            json.dumps(provider_names, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        self._prefix = settings.redis_key_prefix
        self._last_failure = 0.0
        self._unavailable_until = 0.0
        if client is None:
            try:
                from redis import asyncio as redis_async
            except ImportError as exc:
                raise RuntimeError(
                    "Redis requires optional dependencies: "
                    "pip install -r requirements-backends.txt"
                ) from exc
            client = redis_async.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=self._timeout,
                socket_timeout=self._timeout,
                max_connections=settings.redis_max_connections,
            )
        self._redis = client

    def _key(self, kind: str, values: tuple[object, ...]) -> str:
        raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return (
            f"{self._prefix}:search:v{self._VERSION}:"
            f"{self._provider_hash}:{kind}:{digest}"
        )

    async def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if time.monotonic() < self._unavailable_until:
            return None
        try:
            from asyncio import wait_for

            method = getattr(self._redis, operation)
            value = await wait_for(
                method(*args, **kwargs), timeout=self._timeout
            )
            self._unavailable_until = 0.0
            return value
        except Exception as exc:
            now = time.monotonic()
            # An unavailable cache must not impose its socket timeout on
            # every bot request. Retry after a short cooling-off period.
            self._unavailable_until = (
                now + self._failure_cooldown_seconds
            )
            if now - self._last_failure >= 60.0:
                logging.warning(
                    "Redis search cache unavailable, using local/upstream fallback",
                    extra={
                        "event": "search_redis_unavailable",
                        "error_type": type(exc).__name__,
                    },
                )
                self._last_failure = now
            return None

    @staticmethod
    def _candidate(value: object) -> SearchCandidate:
        if not isinstance(value, dict) or not isinstance(value.get("title"), str):
            raise ValueError("invalid search cache candidate")
        fields = SearchCandidate.__dataclass_fields__
        return SearchCandidate(**{key: item for key, item in value.items() if key in fields})

    async def get_page(self, key: tuple[str, int, int]) -> SearchPage | None:
        if self._ttl <= 0:
            return None
        raw = await self._call("get", self._key("page", key))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if data.get("version") != self._VERSION or data.get("kind") != "page":
                return None
            return SearchPage(
                items=[self._candidate(item) for item in data["items"]],
                page=int(data["page"]),
                page_size=int(data["page_size"]),
                has_next=bool(data["has_next"]),
                errors={},
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    async def set_page(self, key: tuple[str, int, int], value: SearchPage) -> None:
        if self._ttl <= 0 or value.errors:
            return
        body = {
            "version": self._VERSION,
            "kind": "page",
            "items": [asdict(item) for item in value.items],
            "page": value.page,
            "page_size": value.page_size,
            "has_next": value.has_next,
        }
        await self._call(
            "set", self._key("page", key),
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            ex=self._ttl,
        )

    async def get_results(
        self, key: tuple[str, int, int, int, int]
    ) -> SearchResultSet | None:
        if self._ttl <= 0:
            return None
        raw = await self._call("get", self._key("results", key))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if data.get("version") != self._VERSION or data.get("kind") != "results":
                return None
            return SearchResultSet(
                items=[self._candidate(item) for item in data["items"]],
                complete=bool(data["complete"]),
                errors={},
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    async def set_results(
        self, key: tuple[str, int, int, int, int], value: SearchResultSet
    ) -> None:
        if self._ttl <= 0 or value.errors:
            return
        body = {
            "version": self._VERSION,
            "kind": "results",
            "items": [asdict(item) for item in value.items],
            "complete": value.complete,
        }
        await self._call(
            "set", self._key("results", key),
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            ex=self._ttl,
        )

    async def aclose(self) -> None:
        close = getattr(self._redis, "aclose", None)
        if callable(close):
            await close()
