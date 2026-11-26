from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.settings import (
    ENV_CONFIG_FILES,
    ROOT_DIR,
    AppSettings,
    ConfigFileError,
    load_project_environment,
    project_env_files,
    project_env_values,
)


class AppSettingsTests(unittest.TestCase):
    def test_telegram_section_sizes_inherit_and_override_independently(self) -> None:
        from core.settings import TELEGRAM_PAGINATION_BLOCKS

        variables = {
            "BOT_TOKEN": "token",
            "TELEGRAM_PAGINATION_PAGE_SIZE": "8",
            "TELEGRAM_PAGINATION_EPISODES_PAGE_SIZE": "3",
            "TELEGRAM_PAGINATION_WIKIPEDIA_PAGE_SIZE": "",
            "TELEGRAM_PAGINATION_SETTINGS_PAGE_SIZE": "20",
            "TELEGRAM_PAGINATION_STATISTICS_PAGE_SIZE": "4",
        }
        settings = AppSettings.from_env(variables).telegram
        self.assertEqual(settings.pagination_page_size, 8)
        self.assertEqual(
            settings.pagination_block_page_sizes,
            {
                "episodes": 3,
                "settings": 20,
                "statistics": 4,
            },
        )
        self.assertIn("search", TELEGRAM_PAGINATION_BLOCKS)
        for invalid in ("0", "31", "wrong"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError, "TELEGRAM_PAGINATION_EPISODES_PAGE_SIZE"
                ):
                    AppSettings.from_env(
                        {**variables, "TELEGRAM_PAGINATION_EPISODES_PAGE_SIZE": invalid}
                    )

    def test_long_text_chunk_size_is_bounded_and_configurable(self) -> None:
        self.assertEqual(AppSettings.from_env({}).telegram.long_text_chunk_size, 4000)
        self.assertEqual(
            AppSettings.from_env(
                {"TELEGRAM_LONG_TEXT_CHUNK_SIZE": "1200"}
            ).telegram.long_text_chunk_size,
            1200,
        )
        for invalid in ("199", "30001", "invalid"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError, "TELEGRAM_LONG_TEXT_CHUNK_SIZE"
                ):
                    AppSettings.from_env({"TELEGRAM_LONG_TEXT_CHUNK_SIZE": invalid})

    def test_telegram_selection_page_size_has_bounded_configuration(self) -> None:
        self.assertEqual(
            AppSettings.from_env({"BOT_TOKEN": "token"}).telegram.pagination_page_size,
            10,
        )
        for size in (1, 3, 10, 30):
            with self.subTest(size=size):
                result = AppSettings.from_env(
                    {"BOT_TOKEN": "token", "TELEGRAM_PAGINATION_PAGE_SIZE": str(size)}
                )
                self.assertEqual(result.telegram.pagination_page_size, size)
        for invalid in ("0", "31", "-1", "not-an-integer"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError, "TELEGRAM_PAGINATION_PAGE_SIZE"
                ):
                    AppSettings.from_env(
                        {"BOT_TOKEN": "token", "TELEGRAM_PAGINATION_PAGE_SIZE": invalid}
                    )

    def test_related_navigation_depth_setting(self):
        self.assertEqual(
            AppSettings.from_env(
                {"BOT_TOKEN": "token"}
            ).telegram.related_navigation_max_depth,
            10,
        )
        for value in (0, 3, 50):
            self.assertEqual(
                AppSettings.from_env(
                    {
                        "BOT_TOKEN": "token",
                        "TELEGRAM_RELATED_NAVIGATION_MAX_DEPTH": str(value),
                    }
                ).telegram.related_navigation_max_depth,
                value,
            )
        for value in ("-1", "51", "x"):
            with self.assertRaisesRegex(
                ValueError, "TELEGRAM_RELATED_NAVIGATION_MAX_DEPTH"
            ):
                AppSettings.from_env(
                    {
                        "BOT_TOKEN": "token",
                        "TELEGRAM_RELATED_NAVIGATION_MAX_DEPTH": value,
                    }
                )

    def test_typed_environment_parsing(self) -> None:
        settings = AppSettings.from_env(
            {
                "BOT_TOKEN": "token",
                "LOG_LEVEL": "debug",
                "LOG_STRUCTURED": "0",
                "LOG_REDACT_SECRETS": "0",
                "LOG_DIR": "logs-test",
                "LOG_MAX_BYTES": "12345",
                "LOG_BACKUPS": "2",
                "TELEGRAM_BOT_API_LOCAL": "true",
                "TELEGRAM_BOT_API_LOCAL_FILE_ROOT": "telegram-local-files",
                "TELEGRAM_SESSION_TTL_SECONDS": "90",
                "ADMIN_TELEGRAM_USER_IDS": "1, 2;3\n2",
                "TELEGRAM_ALLOWED_USER_IDS": "5, 7;8\n7",
                "TELEGRAM_DENIED_REPLY_COOLDOWN_SECONDS": "15.5",
                "TELEGRAM_DENIED_GLOBAL_REPLIES_PER_MINUTE": "12",
                "TELEGRAM_DENIED_MAX_TRACKED_USERS": "100",
                "MAPPINGS_AUTO_UPDATE": "0",
                "MAPPINGS_CHECK_INTERVAL_SECONDS": "600",
                "MAPPINGS_RELEASE_REPOSITORY": "example/mappings",
                "MAL_CLIENT_ID": "mal-client",
                "UPSTREAM_RATE_LIMIT_ANILIST_PER_MINUTE": "77",
                "UPSTREAM_RATE_LIMIT_ANILIST_PEOPLE_PER_MINUTE": "13",
                "KITSU_API_URL": "https://kitsu.example/api/edge/",
                "KITSU_REQUEST_TIMEOUT_SECONDS": "14.5",
                "KITSU_CACHE_TTL_SECONDS": "5432",
                "TRACE_MOE_API_KEY": "trace-key",
                "TRACE_MOE_REQUEST_TIMEOUT_SECONDS": "18.5",
                "WHATANIMEISTHIS_API_KEY": "what-key",
                "WHATANIMEISTHIS_API_DAILY_LIMIT": "0",
                "IMAGE_SEARCH_MAX_BYTES": "123456",
                "UPSTREAM_RATE_LIMIT_KITSU_PER_MINUTE": "44",
                "UPSTREAM_RATE_LIMIT_KITSU_EPISODES_PER_MINUTE": "33",
                "ANISEARCH_API_URL": "https://anisearch.example/api/",
                "ANISEARCH_APP_ID": "anisearch-app-id",
                "ANISEARCH_APP_SECRET": "anisearch-app-secret",
                "ANISEARCH_REQUEST_TIMEOUT_SECONDS": "17.5",
                "ANISEARCH_CACHE_TTL_SECONDS": "4321",
                "UPSTREAM_RATE_LIMIT_ANISEARCH_PER_MINUTE": "5",
                "SKIPDB_API_URL": "https://skipdb.example/api/",
                "SKIPDB_SITE_URL": "https://skipdb.example/",
                "SKIPDB_REQUEST_TIMEOUT_SECONDS": "12.5",
                "SKIPDB_CACHE_TTL_SECONDS": "1234",
                "UPSTREAM_RATE_LIMIT_SKIPDB_PER_MINUTE": "29",
                "ANIME_SKIP_API_URL": "https://anime-skip.example/api/",
                "ANIME_SKIP_CLIENT_ID": "anime-skip-client",
                "ANIME_SKIP_REQUEST_TIMEOUT_SECONDS": "11.5",
                "ANIME_SKIP_CACHE_TTL_SECONDS": "2345",
                "UPSTREAM_RATE_LIMIT_ANIME_SKIP_PER_MINUTE": "41",
                "ANIMETHEMES_API_URL": "https://themes.example/api/",
                "ANIMETHEMES_REQUEST_TIMEOUT_SECONDS": "8.5",
                "ANIMETHEMES_CACHE_TTL_SECONDS": "3456",
                "UPSTREAM_RATE_LIMIT_ANIMETHEMES_PER_MINUTE": "17",
                "UPSTREAM_RATE_LIMIT_ANIMETHEMES_THEMES_PER_MINUTE": "11",
                "BAHAMUT_API_URL": "https://bahamut.example/api/",
                "BAHAMUT_REQUEST_TIMEOUT_SECONDS": "9.5",
                "BAHA_ANIME_SKIP_DATA_URL": "https://baha.example/data.json",
                "BAHA_ANIME_SKIP_DB_PATH": "data/test-baha.sqlite3",
                "SOURCE_HEALTH_FAILURE_THRESHOLD": "5",
                "SOURCE_HEALTH_COOLDOWN_SECONDS": "42.5",
            }
        )
        self.assertEqual(settings.logging.level, "DEBUG")
        self.assertFalse(settings.logging.structured)
        self.assertFalse(settings.logging.redact_secrets)
        self.assertEqual(settings.logging.directory, ROOT_DIR / Path("logs-test"))
        self.assertEqual(settings.logging.max_bytes, 12345)
        self.assertEqual(settings.logging.backups, 2)
        self.assertEqual(settings.telegram.bot_token, "token")
        self.assertTrue(settings.telegram.bot_api_local)
        self.assertEqual(
            settings.telegram.bot_api_local_file_root,
            ROOT_DIR / Path("telegram-local-files"),
        )
        self.assertEqual(settings.telegram.session_ttl_seconds, 90)
        self.assertEqual(settings.telegram.admin_user_ids, (1, 2, 3))
        self.assertEqual(settings.telegram.allowed_user_ids, (5, 7, 8))
        self.assertEqual(settings.telegram.denied_reply_cooldown_seconds, 15.5)
        self.assertEqual(settings.telegram.denied_global_replies_per_minute, 12)
        self.assertEqual(settings.telegram.denied_max_tracked_users, 100)
        self.assertFalse(settings.mappings.auto_update)
        self.assertEqual(settings.mappings.check_interval_seconds, 600.0)
        self.assertEqual(settings.mappings.release_repository, "example/mappings")
        self.assertEqual(settings.sources.mal_client_id, "mal-client")
        self.assertEqual(settings.rate_limits.source_limit("anilist"), 77)
        self.assertEqual(settings.rate_limits.operation_limit("anilist", "people"), 13)
        self.assertEqual(
            settings.sources.kitsu_api_url, "https://kitsu.example/api/edge"
        )
        self.assertEqual(settings.sources.kitsu_request_timeout_seconds, 14.5)
        self.assertEqual(settings.sources.kitsu_cache_ttl_seconds, 5432.0)
        self.assertEqual(settings.sources.trace_moe_api_key, "trace-key")
        self.assertEqual(settings.sources.trace_moe_request_timeout_seconds, 18.5)
        self.assertEqual(settings.sources.whatanimeisthis_api_key, "what-key")
        self.assertEqual(settings.sources.whatanimeisthis_api_daily_limit, 0)
        self.assertEqual(settings.sources.image_search_max_bytes, 123456)
        self.assertEqual(settings.rate_limits.source_limit("kitsu"), 44)
        self.assertEqual(settings.rate_limits.operation_limit("kitsu", "episodes"), 33)
        self.assertEqual(
            settings.sources.anisearch_api_url, "https://anisearch.example/api"
        )
        self.assertEqual(settings.sources.anisearch_app_id, "anisearch-app-id")
        self.assertEqual(settings.sources.anisearch_app_secret, "anisearch-app-secret")
        self.assertEqual(settings.sources.anisearch_request_timeout_seconds, 17.5)
        self.assertEqual(settings.sources.anisearch_cache_ttl_seconds, 4321.0)
        self.assertEqual(settings.rate_limits.source_limit("anisearch"), 5)
        self.assertEqual(settings.sources.skipdb_api_url, "https://skipdb.example/api")
        self.assertEqual(settings.sources.skipdb_site_url, "https://skipdb.example")
        self.assertEqual(settings.sources.skipdb_request_timeout_seconds, 12.5)
        self.assertEqual(settings.sources.skipdb_cache_ttl_seconds, 1234.0)
        self.assertEqual(settings.rate_limits.source_limit("skipdb"), 29)
        self.assertEqual(
            settings.sources.anime_skip_api_url, "https://anime-skip.example/api"
        )
        self.assertEqual(settings.sources.anime_skip_client_id, "anime-skip-client")
        self.assertEqual(settings.sources.anime_skip_request_timeout_seconds, 11.5)
        self.assertEqual(settings.sources.anime_skip_cache_ttl_seconds, 2345.0)
        self.assertEqual(settings.rate_limits.source_limit("anime_skip"), 41)
        self.assertEqual(
            settings.sources.animethemes_api_url, "https://themes.example/api"
        )
        self.assertEqual(settings.sources.animethemes_request_timeout_seconds, 8.5)
        self.assertEqual(settings.sources.animethemes_cache_ttl_seconds, 3456.0)
        self.assertEqual(settings.rate_limits.source_limit("animethemes"), 17)
        self.assertEqual(
            settings.rate_limits.operation_limit("animethemes", "themes"), 11
        )
        self.assertEqual(
            settings.sources.bahamut_api_url, "https://bahamut.example/api"
        )
        self.assertEqual(settings.sources.bahamut_request_timeout_seconds, 9.5)
        self.assertEqual(
            settings.sources.baha_anime_skip_data_url, "https://baha.example/data.json"
        )
        self.assertEqual(
            settings.sources.baha_anime_skip_db_path,
            ROOT_DIR / Path("data/test-baha.sqlite3"),
        )
        self.assertEqual(settings.source_health.failure_threshold, 5)
        self.assertEqual(settings.source_health.cooldown_seconds, 42.5)

    def test_split_env_uses_fixed_files_and_process_environment_wins(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            split = root / ".env.d"
            split.mkdir()
            (split / "telegram.env").write_text(
                "BOT_TOKEN=file-token\nTELEGRAM_SESSION_TTL_SECONDS=91\n",
                encoding="utf-8",
            )
            (split / "runtime.env").write_text(
                "SEARCH_CACHE_MAX_ENTRIES=321\n",
                encoding="utf-8",
            )
            (split / "backup.env").write_text(
                "UNEXPECTED_BACKUP_KEY=must-not-load\n",
                encoding="utf-8",
            )
            environ = {"BOT_TOKEN": "process-token"}

            loaded = load_project_environment(root, environ)

            self.assertEqual(loaded["BOT_TOKEN"], "process-token")
            self.assertEqual(loaded["TELEGRAM_SESSION_TTL_SECONDS"], "91")
            self.assertEqual(loaded["SEARCH_CACHE_MAX_ENTRIES"], "321")
            self.assertNotIn("UNEXPECTED_BACKUP_KEY", loaded)
            self.assertEqual(
                [path.name for path in project_env_files(root)],
                ["telegram.env", "runtime.env"],
            )

    def test_duplicate_keys_across_project_env_files_fail_fast(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            split = root / ".env.d"
            split.mkdir()
            (split / "telegram.env").write_text("BOT_TOKEN=first\n", encoding="utf-8")
            (split / "sources.env").write_text("BOT_TOKEN=second\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigFileError, "Duplicate configuration key"):
                project_env_values(root)

    def test_duplicate_key_inside_one_project_env_file_fails_fast(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            split = root / ".env.d"
            split.mkdir()
            (split / "telegram.env").write_text(
                "BOT_TOKEN=first\nBOT_TOKEN=second\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigFileError, "inside"):
                project_env_values(root)

    def test_legacy_env_remains_supported_during_migration(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(
                "BOT_TOKEN=legacy-token\nLOG_LEVEL=warning\n", encoding="utf-8"
            )

            self.assertEqual(
                project_env_values(root),
                {"BOT_TOKEN": "legacy-token", "LOG_LEVEL": "warning"},
            )

    def test_legacy_and_split_files_cannot_repeat_the_same_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text("BOT_TOKEN=legacy\n", encoding="utf-8")
            split = root / ".env.d"
            split.mkdir()
            (split / "telegram.env").write_text("BOT_TOKEN=split\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigFileError, "Duplicate configuration key"):
                project_env_values(root)

    def test_split_example_directory_has_every_fixed_file_and_unique_keys(self) -> None:
        example_dir = ROOT_DIR / ".env.example.d"
        self.assertEqual(
            {path.name for path in example_dir.glob("*.env")},
            set(ENV_CONFIG_FILES),
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            split = root / ".env.d"
            split.mkdir()
            for name in ENV_CONFIG_FILES:
                (split / name).write_text(
                    (example_dir / name).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            values = project_env_values(root)
        from core.mapping_links import DEFAULT_MAPPING_LINKS

        self.assertEqual(
            {
                key.removeprefix("TELEGRAM_ID_URL_").lower()
                for key in values
                if key.startswith("TELEGRAM_ID_URL_")
            },
            set(DEFAULT_MAPPING_LINKS),
        )
        self.assertEqual(len(values), 274 + len(DEFAULT_MAPPING_LINKS))
        for key in (
            "BOT_ON_FAILURE",
            "BOT_ON_CLEAN_EXIT",
            "BOT_RESTART_MAX_ATTEMPTS",
            "BOT_RESTART_DELAY_SECONDS",
            "BOT_RESTART_MAX_DELAY_SECONDS",
            "BOT_RESTART_RESET_AFTER_SECONDS",
            "BOT_SHUTDOWN_TIMEOUT_SECONDS",
            "API_ENABLED",
            "API_AUTH_MODE",
            "API_READ_KEY",
            "API_ADMIN_KEY",
        ):
            self.assertIn(key, values)
        self.assertIn("RICH_MEDIA_MAX_LOCAL_DELIVERIES", values)
        self.assertIn("BOT_TOKEN", values)
        self.assertIn("TELEGRAM_MAX_CARD_SESSIONS", values)
        self.assertIn("TELEGRAM_MAX_PEOPLE_SESSIONS", values)
        self.assertIn("TELEGRAM_MAX_SEARCH_SESSIONS", values)
        self.assertIn("DATABASE_BACKEND", values)
        self.assertIn("DATABASE_URL", values)
        self.assertIn("CACHE_BACKEND", values)
        self.assertIn("REDIS_URL", values)
        self.assertIn("TELEGRAM_ALLOWED_USER_IDS", values)
        self.assertIn("TELEGRAM_DENIED_REPLY_COOLDOWN_SECONDS", values)
        self.assertIn("TELEGRAM_DENIED_GLOBAL_REPLIES_PER_MINUTE", values)
        self.assertIn("TELEGRAM_DENIED_MAX_TRACKED_USERS", values)
        self.assertIn("TELEGRAM_BOT_API_LOCAL_FILE_ROOT", values)
        self.assertIn("KITSU_API_URL", values)
        self.assertIn("TRACE_MOE_API_KEY", values)
        self.assertIn("WHATANIMEISTHIS_API_KEY", values)
        self.assertIn("IMAGE_SEARCH_DB_PATH", values)
        self.assertIn("ANIME_SKIP_CLIENT_ID", values)
        self.assertIn("ANIMETHEMES_API_URL", values)
        self.assertIn("IMDB_RATINGS_BACKEND", values)
        self.assertIn("IMDB_RATINGS_DB_PATH", values)
        self.assertIn("IMDB_API_ENDPOINT", values)
        self.assertIn("MAPPINGS_MANIFEST_PATH", values)
        self.assertIn("MAPPINGS_RELEASE_REPOSITORY", values)
        self.assertIn("BAHA_ANIME_SKIP_DB_PATH", values)

    def test_relative_paths_are_rooted_at_project(self) -> None:
        settings = AppSettings.from_env(
            {
                "USER_SETTINGS_DB_PATH": "data/test-users.sqlite3",
                "BANS_DB_PATH": "data/test-bans.sqlite3",
                "MAPPINGS_DB_PATH": "data/test-mappings.sqlite3",
                "MAPPINGS_MANIFEST_PATH": "data/test-mappings-manifest.json",
            }
        )
        self.assertEqual(
            settings.storage.user_settings_db_path,
            ROOT_DIR / Path("data/test-users.sqlite3"),
        )
        self.assertEqual(
            settings.storage.bans_db_path,
            ROOT_DIR / Path("data/test-bans.sqlite3"),
        )
        self.assertEqual(
            settings.mappings.db_path,
            ROOT_DIR / Path("data/test-mappings.sqlite3"),
        )
        self.assertEqual(
            settings.mappings.manifest_path,
            ROOT_DIR / Path("data/test-mappings-manifest.json"),
        )

    def test_log_secret_redaction_defaults_to_enabled(self) -> None:
        settings = AppSettings.from_env({"BOT_TOKEN": "token"})
        self.assertTrue(settings.logging.redact_secrets)

    def test_anime_skip_rate_limit_cannot_exceed_server_ceiling(self) -> None:
        settings = AppSettings.from_env(
            {"UPSTREAM_RATE_LIMIT_ANIME_SKIP_PER_MINUTE": "999"}
        )
        self.assertEqual(settings.rate_limits.source_limit("anime_skip"), 60)

    def test_user_default_locale_config(self) -> None:
        self.assertEqual(
            AppSettings.from_env(
                {"USER_DEFAULT_LOCALE": "ru"}
            ).storage.default_user_locale,
            "ru",
        )
        with self.assertRaisesRegex(ValueError, "USER_DEFAULT_LOCALE"):
            AppSettings.from_env({"USER_DEFAULT_LOCALE": "invalid"})

    def test_search_default_providers_from_env(self) -> None:
        values = AppSettings.from_env(
            {
                "SEARCH_DEFAULT_PROVIDERS": "mal,anilist",
                "SEARCH_EXPANSION_ENABLED": "false",
            }
        ).search
        self.assertEqual(values.default_provider_keys, ("mal", "anilist"))
        self.assertFalse(values.expansion_enabled)
        for invalid in ("", "mal,mal", "kitsu,unknown"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "SEARCH_DEFAULT_PROVIDERS"):
                    AppSettings.from_env({"SEARCH_DEFAULT_PROVIDERS": invalid})

    def test_database_backend_selection_validation(self) -> None:
        defaults = AppSettings.from_env({})
        self.assertEqual(defaults.storage.backend, "sqlite")
        self.assertEqual(defaults.cache.backend, "memory")
        self.assertEqual(defaults.storage.postgres_pool_size, 5)
        self.assertEqual(defaults.cache.redis_key_prefix, "anime-db-bot")
        selected = AppSettings.from_env(
            {
                "DATABASE_BACKEND": "postgres",
                "DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/testdb",
                "DATABASE_POOL_SIZE": "8",
                "CACHE_BACKEND": "redis",
                "REDIS_URL": "redis://127.0.0.1:6379/1",
                "REDIS_KEY_PREFIX": "test-cache",
                "REDIS_TIMEOUT_SECONDS": "0.15",
            }
        )
        self.assertEqual(selected.storage.backend, "postgres")
        self.assertEqual(selected.storage.postgres_pool_size, 8)
        self.assertEqual(selected.cache.backend, "redis")
        self.assertEqual(selected.cache.redis_timeout_seconds, 0.15)
        with self.assertRaisesRegex(ValueError, "DATABASE_BACKEND"):
            AppSettings.from_env({"DATABASE_BACKEND": "mysql"})
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            AppSettings.from_env({"DATABASE_BACKEND": "postgres"})
        with self.assertRaisesRegex(ValueError, "CACHE_BACKEND"):
            AppSettings.from_env({"CACHE_BACKEND": "memcached"})
        with self.assertRaisesRegex(ValueError, "REDIS_URL"):
            AppSettings.from_env({"CACHE_BACKEND": "redis"})

    def test_invalid_admin_id_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            AppSettings.from_env({"ADMIN_TELEGRAM_USER_IDS": "1,nope"})

    def test_user_allowlist_is_optional_and_validated(self) -> None:
        self.assertEqual(AppSettings.from_env({}).telegram.allowed_user_ids, ())
        self.assertEqual(
            AppSettings.from_env(
                {"TELEGRAM_ALLOWED_USER_IDS": ""}
            ).telegram.allowed_user_ids,
            (),
        )
        with self.assertRaisesRegex(ValueError, "TELEGRAM_ALLOWED_USER_IDS"):
            AppSettings.from_env({"TELEGRAM_ALLOWED_USER_IDS": "1,nope"})
        with self.assertRaisesRegex(ValueError, "TELEGRAM_ALLOWED_USER_IDS"):
            AppSettings.from_env({"TELEGRAM_ALLOWED_USER_IDS": "-2"})

    def test_required_bot_token_validation(self) -> None:
        with self.assertRaises(RuntimeError):
            AppSettings.from_env({}).require_bot_token()
        self.assertEqual(
            AppSettings.from_env({"BOT_TOKEN": "x"}).require_bot_token(), "x"
        )

    def test_only_settings_module_reads_environment_in_production(self) -> None:
        forbidden = (
            "AppSettings.from_env(",
            "os.getenv(",
            "os.environ",
            "load_dotenv(",
        )
        violations: list[str] = []
        settings_module = ROOT_DIR / "core" / "settings.py"
        for root_name in ("bots", "core", "mappings", "search", "services", "sources"):
            for path in (ROOT_DIR / root_name).rglob("*.py"):
                if path == settings_module:
                    continue
                source = path.read_text(encoding="utf-8")
                for token in forbidden:
                    if token in source:
                        violations.append(f"{path.relative_to(ROOT_DIR)}: {token}")

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
