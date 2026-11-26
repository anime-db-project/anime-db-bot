from __future__ import annotations

import asyncio
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor

from core.models import SearchCandidate, SearchPage, SearchResultSet
from core.sqlite import managed_sqlite_connection
from core.settings import AppSettings, CacheBackendSettings, SearchSettings
from services.runtime.app_context import AppContext
from scripts.migrate_sqlite_to_postgres import count_rows, migrate
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
        if not dsn or os.environ.get("TEST_BACKENDS_ALLOW_DESTRUCTIVE") != "1":
            raise unittest.SkipTest("isolated PostgreSQL test services not configured")
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

    def test_migration_from_sqlite_is_complete_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            users_path = root / "users.db"
            bans_path = root / "bans.db"
            quota_path = root / "quota.db"
            with managed_sqlite_connection(sqlite3.connect(users_path)) as conn:
                conn.execute(
                    "CREATE TABLE user_settings (platform TEXT, user_id INTEGER, "
                    "locale TEXT, updated_at TEXT)"
                )
                conn.execute(
                    "CREATE TABLE known_users (platform TEXT, user_id INTEGER, "
                    "first_seen_at TEXT, last_seen_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO user_settings VALUES (?,?,?,?)",
                    ("telegram", 9876543211, "ru", "2026-10-01 11:22:33"),
                )
                conn.execute(
                    "INSERT INTO known_users VALUES (?,?,?,?)",
                    (
                        "telegram", 9876543211,
                        "2026-10-01 11:22:33", "2026-11-01 11:22:33",
                    ),
                )
            with managed_sqlite_connection(sqlite3.connect(bans_path)) as conn:
                conn.execute(
                    "CREATE TABLE bans (platform TEXT, user_id INTEGER, "
                    "reason TEXT, banned_by INTEGER, created_at INTEGER, "
                    "expires_at INTEGER)"
                )
                conn.execute(
                    "INSERT INTO bans VALUES (?,?,?,?,?,?)",
                    ("telegram", 9876543211, "test", 99, 500, None),
                )
            with managed_sqlite_connection(sqlite3.connect(quota_path)) as conn:
                conn.execute(
                    "CREATE TABLE image_search_quota "
                    "(provider TEXT,period TEXT,used INTEGER,exhausted INTEGER)"
                )
                conn.execute(
                    "INSERT INTO image_search_quota VALUES (?,?,?,?)",
                    ("migration-test", "2099-12-31", 7, 1),
                )
            self.assertEqual(count_rows(users_path, "known_users"), 1)
            for attempt in range(2):
                migrated = migrate(
                    self.pool, users_path, bans_path, quota_path
                )
                self.assertEqual(
                    migrated,
                    {
                        "user_settings": 1,
                        "known_users": 1,
                        "bans": 1,
                        "image_search_quota": 1,
                    },
                )
            user_store = PostgresUserSettingsStore(self.pool)
            self.assertEqual(user_store.get(9876543211).locale, "ru")
            self.assertIn(9876543211, user_store.list_user_ids("telegram"))
            self.assertEqual(
                PostgresBanStore(self.pool)
                .get_active("telegram", 9876543211, now=1000)
                .reason,
                "test",
            )
            self.assertEqual(
                PostgresImageSearchQuotaStore(self.pool)
                .state("migration-test", period="2099-12-31")
                .used,
                7,
            )
            # The SQLite sources have not been touched or deleted.
            self.assertEqual(count_rows(users_path, "user_settings"), 1)
            self.assertEqual(count_rows(bans_path, "bans"), 1)

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
        if not url or os.environ.get("TEST_BACKENDS_ALLOW_DESTRUCTIVE") != "1":
            self.skipTest("isolated Redis test services not configured")
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

    async def test_app_context_selects_real_optional_backends(self) -> None:
        settings = AppSettings.from_env(
            {
                "BOT_TOKEN": "test-token",
                "DATABASE_BACKEND": "postgres",
                "DATABASE_URL": os.environ["TEST_POSTGRES_DSN"],
                "DATABASE_POOL_SIZE": "2",
                "CACHE_BACKEND": "redis",
                "REDIS_URL": os.environ["TEST_REDIS_URL"],
                "MAPPINGS_AUTO_UPDATE": "0",
            }
        )
        context = AppContext.create(settings)
        try:
            self.assertIsInstance(context.user_settings_store, PostgresUserSettingsStore)
            self.assertIsInstance(context.ban_store, PostgresBanStore)
            self.assertIsInstance(
                context.image_search_quota_store, PostgresImageSearchQuotaStore
            )
            self.assertIsInstance(context.redis_cache, RedisSearchCache)
            self.assertIs(context.search_service._shared_cache, context.redis_cache)
        finally:
            await context.aclose()

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
