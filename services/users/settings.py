from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from core.settings import get_settings
from core.sqlite import managed_sqlite_connection
from localization import DEFAULT_LOCALE, normalize_locale

_DEFAULT_PLATFORM = "telegram"


@dataclass(frozen=True, slots=True)
class UserSettings:
    locale: str = DEFAULT_LOCALE


class SQLiteUserSettingsStore:
    """Persistent repository for platform-scoped user preferences and known IDs."""

    def __init__(
        self,
        path: str | Path,
        *,
        cache_max_entries: int = 20000,
        cache_ttl_seconds: float = 120.0,
        touch_interval_seconds: float = 300.0,
    ) -> None:
        self._path = str(path)
        self._cache_max_entries = max(1, int(cache_max_entries))
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._touch_interval_seconds = max(0.0, float(touch_interval_seconds))
        self._initialized = False
        self._init_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        self._locales: OrderedDict[tuple[str, int], tuple[float, UserSettings]] = (
            OrderedDict()
        )
        self._touches: OrderedDict[tuple[str, int], float] = OrderedDict()

    def _connect(self) -> sqlite3.Connection:
        if self._path != ":memory:":
            Path(self._path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _platform_key(platform: str) -> str:
        platform_key = (platform or "").strip().casefold()
        if not platform_key:
            raise ValueError("platform must not be empty")
        return platform_key

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }

    @staticmethod
    def _create_user_settings_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE user_settings (
                platform TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                locale TEXT NOT NULL DEFAULT 'en',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, user_id)
            )
            """
        )

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with managed_sqlite_connection(self._connect()) as connection:
                columns = self._table_columns(connection, "user_settings")
                if not columns:
                    self._create_user_settings_table(connection)
                elif "platform" not in columns:
                    # Legacy schema used only user_id as the primary key. Existing
                    # rows belong to Telegram because that was the only transport
                    # with persisted settings before platform scoping was added.
                    connection.execute(
                        "ALTER TABLE user_settings RENAME TO user_settings_legacy"
                    )
                    self._create_user_settings_table(connection)
                    connection.execute(
                        """
                        INSERT INTO user_settings (platform, user_id, locale, updated_at)
                        SELECT ?, user_id, locale, updated_at
                        FROM user_settings_legacy
                        """,
                        (_DEFAULT_PLATFORM,),
                    )
                    connection.execute("DROP TABLE user_settings_legacy")

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS known_users (
                        platform TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (platform, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO known_users (
                        platform, user_id, first_seen_at, last_seen_at
                    )
                    SELECT platform, user_id, updated_at, updated_at
                    FROM user_settings
                    """
                )
            self._initialized = True

    def get(
        self,
        user_id: int | None,
        *,
        platform: str = _DEFAULT_PLATFORM,
    ) -> UserSettings:
        if user_id is None:
            return UserSettings()
        platform_key = self._platform_key(platform)
        cache_key = (platform_key, int(user_id))
        now = time.monotonic()
        with self._cache_lock:
            cached = self._locales.get(cache_key)
            if cached is not None and cached[0] > now:
                self._locales.move_to_end(cache_key)
                return cached[1]
            self._locales.pop(cache_key, None)
        self._ensure_schema()
        with managed_sqlite_connection(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT locale FROM user_settings
                WHERE platform = ? AND user_id = ?
                """,
                (platform_key, int(user_id)),
            ).fetchone()
        result = (
            UserSettings(locale=normalize_locale(str(row[0])))
            if row is not None
            else UserSettings()
        )
        with self._cache_lock:
            self._locales[cache_key] = (time.monotonic() + self._cache_ttl_seconds, result)
            self._locales.move_to_end(cache_key)
            while len(self._locales) > self._cache_max_entries:
                self._locales.popitem(last=False)
        return result

    def set_locale(
        self,
        user_id: int,
        locale: str,
        *,
        platform: str = _DEFAULT_PLATFORM,
    ) -> UserSettings:
        platform_key = self._platform_key(platform)
        self._ensure_schema()
        normalized = normalize_locale(locale)
        with managed_sqlite_connection(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO user_settings (platform, user_id, locale, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    locale = excluded.locale,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (platform_key, int(user_id), normalized),
            )
        result = UserSettings(locale=normalized)
        with self._cache_lock:
            key = (platform_key, int(user_id))
            self._locales[key] = (time.monotonic() + self._cache_ttl_seconds, result)
            self._locales.move_to_end(key)
        return result

    def touch_user(self, platform: str, user_id: int) -> None:
        platform_key = self._platform_key(platform)
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("user_id must be positive")

        # Update last_seen at most once every five minutes for an active
        # user. This removes a disk transaction from most button presses.
        key = (platform_key, user_id)
        now = time.monotonic()
        with self._cache_lock:
            last_touch = self._touches.get(key)
            if last_touch is not None and now - last_touch < self._touch_interval_seconds:
                self._touches.move_to_end(key)
                return
        self._ensure_schema()
        with managed_sqlite_connection(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO known_users (platform, user_id, first_seen_at, last_seen_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (platform_key, user_id),
            )
        with self._cache_lock:
            self._touches[key] = now
            self._touches.move_to_end(key)
            while len(self._touches) > self._cache_max_entries:
                self._touches.popitem(last=False)

    def list_user_ids(self, platform: str) -> list[int]:
        platform_key = (platform or "").strip().casefold()
        if not platform_key:
            return []
        self._ensure_schema()
        with managed_sqlite_connection(self._connect()) as connection:
            rows = connection.execute(
                "SELECT user_id FROM known_users WHERE platform = ? ORDER BY user_id",
                (platform_key,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def clear(self) -> None:
        self._ensure_schema()
        with managed_sqlite_connection(self._connect()) as connection:
            connection.execute("DELETE FROM user_settings")
            connection.execute("DELETE FROM known_users")
        with self._cache_lock:
            self._locales.clear()
            self._touches.clear()


_CURRENT_USER_SETTINGS_STORE: ContextVar[SQLiteUserSettingsStore | None] = ContextVar(
    "anime_db_bot_user_settings_store",
    default=None,
)


@lru_cache(maxsize=1)
def _default_user_settings_store() -> SQLiteUserSettingsStore:
    """Compatibility store for standalone calls outside an active AppContext."""
    storage = get_settings().storage
    if storage.backend == "postgres":
        from services.storage.postgres import default_postgres_pool
        from services.storage.postgres_repositories import PostgresUserSettingsStore

        return PostgresUserSettingsStore(default_postgres_pool())
    return SQLiteUserSettingsStore(storage.user_settings_db_path)


def get_user_settings_store() -> SQLiteUserSettingsStore:
    return _CURRENT_USER_SETTINGS_STORE.get() or _default_user_settings_store()


@contextmanager
def activate_user_settings_store(
    store: SQLiteUserSettingsStore,
) -> Iterator[SQLiteUserSettingsStore]:
    token: Token[SQLiteUserSettingsStore | None] = _CURRENT_USER_SETTINGS_STORE.set(
        store
    )
    try:
        yield store
    finally:
        _CURRENT_USER_SETTINGS_STORE.reset(token)


def get_user_locale(
    user_id: int | None,
    platform: str = _DEFAULT_PLATFORM,
) -> str:
    return get_user_settings_store().get(user_id, platform=platform).locale


def set_user_locale(
    user_id: int,
    locale: str,
    platform: str = _DEFAULT_PLATFORM,
) -> str:
    return (
        get_user_settings_store()
        .set_locale(
            user_id,
            locale,
            platform=platform,
        )
        .locale
    )


def touch_platform_user(platform: str, user_id: int) -> None:
    get_user_settings_store().touch_user(platform, user_id)


def list_platform_user_ids(platform: str) -> list[int]:
    return get_user_settings_store().list_user_ids(platform)
