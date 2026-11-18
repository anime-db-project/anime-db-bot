from __future__ import annotations

import asyncio
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

from core.models import SearchCandidate, SearchPage, SearchResultSet
from core.settings import CacheBackendSettings, SearchSettings
from services.search.redis_cache import RedisSearchCache
from services.search.service import SearchService
from services.storage.postgres import PostgresPool
from services.storage.postgres_repositories import (
    PostgresBanStore,
    PostgresImageSearchQuotaStore,
    PostgresUserSettingsStore,
)


class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn = os.environ.get("TEST_POSTGRES_DSN", "")
        if not dsn:
            raise unittest.SkipTest("TEST_POSTGRES_DSN not configured")
        cls.pool = PostgresPool(dsn, max_size=3)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "pool"):
            cls.pool.close()

    def test_user_settings_undo_reconnect_and_bigint(self) -> None:
        first = PostgresUserSettingsStore(self.pool)
        first.clear()
        first.touch_user("telegram", 9000000000000)
        first.set_locale(9000000000000, "ru")
        second = PostgresUserSettingsStore(self.pool)
        self.assertEqual(second.get(9000000000000).locale, "ru")
        self.assertEqual(second.list_user_ids("telegram"), [9000000000000])
        self.assertEqual(second.get(9000000000000, platform="discord").locale, "en")
        second.clear()

    def test_moderation_upserts_and_expiration(self) -> None:
        store = PostgresBanStore(self.pool)
        store.clear()
        store.ban("telegram", 9000000000000, banned_by=100, now=1000)
        self.assertIsNotNone(store.get_active("telegram", 9000000000000, now=1100))
        store.ban("telegram", 9000000000000, banned_by=100, now=2000, duration_seconds=5)
        self.assertIsNone(store.get_active("telegram", 9000000000000, now=2005))
        store.ban("telegram", 222, banned_by=100, now=1000)
        self.assertTrue(store.unban("telegram", 222))
        store.clear()

    def test_image_search_quota_upsert_and_daily_reset(self) -> None:
        store = PostgresImageSearchQuotaStore(self.pool)
        provider = "integration-test"
        period = "2099-12-31"
        with self.pool.connection() as connection:
            connection.execute(
                "DELETE FROM image_search_quota WHERE provider = ? AND period = ?",
                (provider, period),
            )
        store.consume(provider, amount=2, period=period)
        store.consume(provider, amount=3, period=period)
        store.mark_exhausted(provider, period=period)
        state = store.state(provider, period=period)
        self.assertEqual(state.used, 5)
        self.assertTrue(state.exhausted)
        self.assertEqual(store.state(provider, period="2099-12-30").used, 0)

    def test_concurrent_connection_pool_is_safe(self) -> None:
        store = PostgresUserSettingsStore(self.pool)
        store.clear()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(
                lambda uid: store.touch_user("telegram", uid),
                range(1000, 1020),
            ))
        self.assertEqual(len(store.list_user_ids("telegram")), 20)
        store.clear()


class _FakeSource:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, page: int, limit: int) -> SearchPage:
        self.calls += 1
        return SearchPage(
            items=[SearchCandidate(title="Naruto", external_ids={"mal": "20"})],
            page=page,
            page_size=limit,
            has_next=False,
        )


class RedisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        url = os.environ.get("TEST_REDIS_URL", "")
        if not url:
            self.skipTest("TEST_REDIS_URL not configured")
        import redis.asyncio as redis

        self.client = redis.from_url(url, decode_responses=True)
        await self.client.flushdb()
        self.settings = CacheBackendSettings(
            backend="redis",
            redis_url=url,
            redis_key_prefix="integration",
        )

    async def asyncTearDown(self) -> None:
        if hasattr(self, "client"):
            await self.client.flushdb()
            await self.client.aclose()

    async def test_search_page_reused_between_instances(self) -> None:
        cache1 = RedisSearchCache(
            self.settings, ttl_seconds=600, provider_names=("fake",),
            client=self.client,
        )
        source = _FakeSource()
        service = SearchService([source], shared_cache=cache1)
        self.assertEqual((await service.search("Naruto")).items[0].title, "Naruto")
        self.assertEqual(source.calls, 1)

        source2 = _FakeSource()
        cache2 = RedisSearchCache(
            self.settings, ttl_seconds=600, provider_names=("fake",),
            client=self.client,
        )
        service2 = SearchService([source2], shared_cache=cache2)
        self.assertEqual((await service2.search("Naruto")).items[0].title, "Naruto")
        self.assertEqual(source2.calls, 0)

    async def test_result_set_reused_between_instances(self) -> None:
        a = _FakeSource()
        first = SearchService(
            [a],
            shared_cache=RedisSearchCache(
                self.settings, ttl_seconds=600, provider_names=("fake",),
                client=self.client,
            ),
        )
        items = await first.collect("Naruto")
        self.assertEqual(items.items[0].title, "Naruto")
        self.assertEqual(a.calls, 1)

        b = _FakeSource()
        second = SearchService(
            [b],
            shared_cache=RedisSearchCache(
                self.settings, ttl_seconds=600, provider_names=("fake",),
                client=self.client,
            ),
        )
        self.assertEqual((await second.collect("Naruto")).items[0].title, "Naruto")
        self.assertEqual(b.calls, 0)

    async def test_cache_ignores_corrupted_json_and_is_not_authoritative(self) -> None:
        cache = RedisSearchCache(
            self.settings, ttl_seconds=600, provider_names=("fake",),
            client=self.client,
        )
        await self.client.set(cache._key("page", ("naruto", 1, 10)), "{broken")
        self.assertIsNone(await cache.get_page(("naruto", 1, 10)))
        self.assertIsNone(await cache.get_results(("naruto", 20, 4, 55, 1)))
