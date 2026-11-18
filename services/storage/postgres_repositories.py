from __future__ import annotations

import threading
from pathlib import Path

from core.sqlite import managed_sqlite_connection
from services.search.image_quota import SQLiteImageSearchQuotaStore
from services.users.bans import SQLiteBanStore
from services.users.settings import SQLiteUserSettingsStore

from .postgres import PostgresConnection, PostgresPool


class PostgresUserSettingsStore(SQLiteUserSettingsStore):
    """PostgreSQL-backed users and settings; keeps same caching semantics."""

    def __init__(
        self,
        pool: PostgresPool,
        *,
        cache_max_entries: int = 20000,
        cache_ttl_seconds: float = 120.0,
        touch_interval_seconds: float = 300.0,
    ) -> None:
        super().__init__(
            ":memory:",
            cache_max_entries=cache_max_entries,
            cache_ttl_seconds=cache_ttl_seconds,
            touch_interval_seconds=touch_interval_seconds,
        )
        self._pool = pool

    def _connect(self) -> PostgresConnection:
        return self._pool.connection()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with managed_sqlite_connection(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_settings (
                        platform TEXT NOT NULL,
                        user_id BIGINT NOT NULL,
                        locale TEXT NOT NULL DEFAULT 'en',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (platform, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS known_users (
                        platform TEXT NOT NULL,
                        user_id BIGINT NOT NULL,
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (platform, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO known_users (
                        platform, user_id, first_seen_at, last_seen_at
                    )
                    SELECT platform, user_id, updated_at, updated_at
                    FROM user_settings
                    ON CONFLICT (platform, user_id) DO NOTHING
                    """
                )
            self._initialized = True


class PostgresBanStore(SQLiteBanStore):
    def __init__(self, pool: PostgresPool) -> None:
        super().__init__(":memory:")
        self._pool = pool

    def _connect(self) -> PostgresConnection:
        return self._pool.connection()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with managed_sqlite_connection(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bans (
                        platform TEXT NOT NULL,
                        user_id BIGINT NOT NULL,
                        reason TEXT,
                        banned_by BIGINT NOT NULL,
                        created_at BIGINT NOT NULL,
                        expires_at BIGINT,
                        PRIMARY KEY (platform, user_id)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_bans_expires_at ON bans(expires_at)"
                )
            self._initialized = True


class PostgresImageSearchQuotaStore(SQLiteImageSearchQuotaStore):
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool
        self.path = Path(":postgres:")
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return self._pool.connection()

    def _init_schema(self) -> None:
        with self._lock, managed_sqlite_connection(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS image_search_quota (
                    provider TEXT NOT NULL,
                    period TEXT NOT NULL,
                    used BIGINT NOT NULL DEFAULT 0,
                    exhausted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider, period)
                )
                """
            )
