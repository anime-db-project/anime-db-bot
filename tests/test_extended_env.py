from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bots.telegram.rich_media import RichMediaDelivery
from bots.telegram.search.handlers import SearchUiState
from core.http_client import HttpClientPool
from core.settings import AppSettings, HttpSettings, RichMediaSettings
from services.media.people import PeopleProgressService
from services.users.settings import SQLiteUserSettingsStore
from sources.providers.jikan import JikanSource


class ExtendedEnvironmentTests(unittest.TestCase):
    def test_custom_resource_limits_are_parsed(self) -> None:
        config = AppSettings.from_env(
            {
                "HTTP_MAX_CONCURRENT_REQUESTS": "6",
                "HTTP_MAX_CONNECTIONS": "10",
                "HTTP_MAX_KEEPALIVE_CONNECTIONS": "4",
                "HTTP_KEEPALIVE_EXPIRY_SECONDS": "1.25",
                "PEOPLE_MANIFEST_CONCURRENCY": "2",
                "PEOPLE_PORTRAIT_CONCURRENCY": "3",
                "JIKAN_CACHE_MAX_ENTRIES": "12",
                "ANISEARCH_CACHE_MAX_ENTRIES": "14",
                "ANISEARCH_INTERACTIVE_WAIT_SECONDS": "0.2",
                "TELEGRAM_MAX_SCOPE_SESSIONS": "7",
                "TELEGRAM_MAX_PENDING_INPUTS": "8",
                "TELEGRAM_MAX_IMAGE_INPUTS": "9",
                "TELEGRAM_IMAGE_DOWNLOAD_TIMEOUT_SECONDS": "14",
                "TELEGRAM_IMAGE_MAX_PIXELS": "2000000",
                "TELEGRAM_IMAGE_MAX_DIMENSION": "1024",
                "TELEGRAM_SEARCH_EXPANSION_TIMEOUT_SECONDS": "4",
                "TELEGRAM_INITIAL_TIMECODE_WAIT_SECONDS": "0.1",
                "TELEGRAM_ADMIN_PENDING_SESSIONS": "13",
                "TELEGRAM_ADMIN_PENDING_TTL_SECONDS": "60",
                "TELEGRAM_DENIED_MESSAGE": "Закрытый бот",
                "RICH_MEDIA_FAILED_URL_TTL_SECONDS": "2.5",
                "RICH_MEDIA_FAILED_METHOD_TTL_SECONDS": "4.5",
                "RICH_MEDIA_FAILURE_CACHE_ENTRIES": "7",
                "RICH_MEDIA_RAM_CACHE_ENTRIES": "5",
                "RICH_MEDIA_MAX_REDIRECTS": "2",
                "RICH_MEDIA_DISK_PRUNE_INTERVAL_SECONDS": "8",
                "USER_LOCALE_CACHE_MAX_ENTRIES": "10",
                "USER_LOCALE_CACHE_TTL_SECONDS": "3",
                "USER_SEEN_WRITE_INTERVAL_SECONDS": "6",
                "REDIS_MAX_CONNECTIONS": "5",
                "REDIS_FAILURE_COOLDOWN_SECONDS": "9",
            }
        )
        self.assertEqual(config.http.max_concurrent_requests, 6)
        self.assertEqual(config.http.max_connections, 10)
        self.assertEqual(config.http.max_keepalive_connections, 4)
        self.assertEqual(config.http.keepalive_expiry_seconds, 1.25)
        self.assertEqual(config.sources.people_manifest_concurrency, 2)
        self.assertEqual(config.sources.people_portrait_concurrency, 3)
        self.assertEqual(config.sources.jikan_cache_max_entries, 12)
        self.assertEqual(config.sources.anisearch_cache_max_entries, 14)
        self.assertEqual(config.telegram.max_scope_sessions, 7)
        self.assertEqual(config.telegram.image_download_timeout_seconds, 14.0)
        self.assertEqual(config.telegram.image_max_pixels, 2000000)
        self.assertEqual(config.telegram.image_max_dimension, 1024)
        self.assertEqual(config.telegram.search_expansion_timeout_seconds, 4.0)
        self.assertEqual(config.telegram.denied_message, "Закрытый бот")
        self.assertEqual(config.telegram.admin_pending_ttl_seconds, 60)
        self.assertEqual(config.telegram.rich_media.failure_url_ttl_seconds, 2.5)
        self.assertEqual(config.telegram.rich_media.max_redirects, 2)
        self.assertEqual(config.storage.user_locale_cache_max_entries, 10)
        self.assertEqual(config.cache.redis_max_connections, 5)
        self.assertEqual(config.cache.redis_failure_cooldown_seconds, 9.0)

    def test_invalid_resource_limits_fail_fast(self) -> None:
        invalid = {
            "HTTP_MAX_CONCURRENT_REQUESTS": "0",
            "HTTP_MAX_CONNECTIONS": "NaN",
            "HTTP_KEEPALIVE_EXPIRY_SECONDS": "inf",
            "HTTP_MAX_KEEPALIVE_CONNECTIONS": "-1",
            "PEOPLE_MANIFEST_CONCURRENCY": "500",
            "TELEGRAM_IMAGE_MAX_PIXELS": "100",
            "RICH_MEDIA_MAX_REDIRECTS": "0",
            "REDIS_MAX_CONNECTIONS": "-4",
            "RICH_MEDIA_FAILED_URL_TTL_SECONDS": "nan",
            "TELEGRAM_DENIED_MESSAGE": "x" * 257,
        }
        for key, raw in invalid.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    AppSettings.from_env({key: raw})

    def test_keepalive_connections_must_fit_http_connection_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP_MAX_KEEPALIVE_CONNECTIONS"):
            AppSettings.from_env(
                {"HTTP_MAX_CONNECTIONS": "5", "HTTP_MAX_KEEPALIVE_CONNECTIONS": "6"}
            )

    def test_session_cache_limits_reach_telegram_stores(self) -> None:
        state = SearchUiState.create(
            150,
            max_sessions=3,
            max_scope_sessions=4,
            max_pending_inputs=5,
            max_image_inputs=6,
        )
        self.assertEqual(state.sessions._max_entries, 3)
        self.assertEqual(state.scope_sessions._max_entries, 4)
        self.assertEqual(state.pending_inputs._max_entries, 5)
        self.assertEqual(state.image_inputs._max_entries, 6)

    def test_people_and_source_caches_use_configured_limits(self) -> None:
        sources = AppSettings.from_env(
            {
                "PEOPLE_MANIFEST_CONCURRENCY": "2",
                "PEOPLE_PORTRAIT_CONCURRENCY": "3",
                "JIKAN_CACHE_MAX_ENTRIES": "11",
            }
        ).sources
        service = PeopleProgressService(
            [],
            MagicMock(),
            manifest_concurrency=sources.people_manifest_concurrency,
            portrait_concurrency=sources.people_portrait_concurrency,
        )
        self.assertEqual(service._manifest_slots._value, 2)
        self.assertEqual(service._remote_portrait_slots._value, 3)
        jikan = JikanSource(sources)
        self.assertEqual(jikan._people_cache.max_entries, 11)
        self.assertEqual(jikan._statistics_cache.max_entries, 11)

    def test_user_store_cache_and_default_locale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserSettingsStore(
                Path(directory) / "users.sqlite3",
                cache_max_entries=2,
                cache_ttl_seconds=1.0,
                touch_interval_seconds=0.0,
                default_locale="ru",
            )
            self.assertEqual(store.get(None).locale, "ru")
            self.assertEqual(store.get(101).locale, "ru")
            self.assertEqual(store.get(102).locale, "ru")
            self.assertEqual(store.get(103).locale, "ru")
            self.assertLessEqual(len(store._locales), 2)
            store.set_locale(103, "ja")
            self.assertEqual(store.get(103).locale, "ja")

    def test_rich_media_cache_retention_options(self) -> None:
        media = RichMediaDelivery(
            RichMediaSettings(
                failure_url_ttl_seconds=2.5,
                failure_method_ttl_seconds=6.5,
                failure_cache_max_entries=19,
                ram_cache_max_entries=13,
                max_redirects=3,
                disk_prune_interval_seconds=30.0,
            )
        )
        self.assertEqual(media._bad_urls.ttl_seconds, 2.5)
        self.assertEqual(media._bad_strategies.ttl_seconds, 6.5)
        self.assertEqual(media._bad_urls.max_entries, 19)
        self.assertEqual(media._memory.max_entries, 13)


if __name__ == "__main__":
    unittest.main()
