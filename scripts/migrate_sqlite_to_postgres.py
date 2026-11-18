#!/usr/bin/env python3
"""Explicit SQLite -> PostgreSQL migration for mutable application tables.

This script never removes or modifies its source SQLite files. Run while the
bot is stopped so user preferences, moderation state and quotas remain
consistent across backends. `--apply` is required for any PostgreSQL write.
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from core.settings import get_settings
from services.storage.postgres import PostgresPool
from services.storage.postgres_repositories import (
    PostgresBanStore,
    PostgresImageSearchQuotaStore,
    PostgresUserSettingsStore,
)


@contextmanager
def read_source(path: Path) -> Iterator[sqlite3.Connection | None]:
    if not path.is_file():
        yield None
        return
    # Read-only mode ensures a typo cannot create an empty SQLite DB.
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def count_rows(path: Path, table: str) -> int:
    with read_source(path) as conn:
        if conn is None or not table_exists(conn, table):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _utc_stamp(value: object) -> datetime:
    raw = str(value).replace(" ", "T")
    moment = datetime.fromisoformat(raw)
    return (
        moment.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None
        else moment.astimezone(timezone.utc)
    )


def migrate(
    pool: PostgresPool,
    user_path: Path,
    bans_path: Path,
    quota_path: Path,
) -> dict[str, int]:
    """Migrate all mutable tables in one transaction, preserving old files."""
    PostgresUserSettingsStore(pool)._ensure_schema()
    PostgresBanStore(pool)._ensure_schema()
    PostgresImageSearchQuotaStore(pool)

    counts = {
        "user_settings": 0,
        "known_users": 0,
        "bans": 0,
        "image_search_quota": 0,
    }
    # All inserts/updates commit together; if any row fails, none of this
    # invocation's data migration is committed.
    with pool.connection() as target:
        with read_source(user_path) as users:
            if users is not None:
                if table_exists(users, "user_settings"):
                    fields = {
                        row[1]
                        for row in users.execute("PRAGMA table_info(user_settings)")
                    }
                    if "platform" not in fields:
                        raise RuntimeError(
                            "Legacy user_settings schema needs SQLite in-place "
                            "upgrade before PostgreSQL migration"
                        )
                    rows = users.execute(
                        "SELECT platform,user_id,locale,updated_at FROM user_settings"
                    )
                    for platform, user_id, locale, updated_at in rows:
                        target.execute(
                            """
                            INSERT INTO user_settings (
                                platform,user_id,locale,updated_at
                            ) VALUES (?,?,?,?)
                            ON CONFLICT (platform,user_id) DO UPDATE SET
                                locale=excluded.locale,
                                updated_at=excluded.updated_at
                            """,
                            (platform, user_id, locale, _utc_stamp(updated_at)),
                        )
                        counts["user_settings"] += 1
                if table_exists(users, "known_users"):
                    rows = users.execute(
                        """
                        SELECT platform,user_id,first_seen_at,last_seen_at
                        FROM known_users
                        """
                    )
                    for platform, user_id, first_seen, last_seen in rows:
                        target.execute(
                            """
                            INSERT INTO known_users (
                                platform,user_id,first_seen_at,last_seen_at
                            ) VALUES (?,?,?,?)
                            ON CONFLICT (platform,user_id) DO UPDATE SET
                                first_seen_at=excluded.first_seen_at,
                                last_seen_at=excluded.last_seen_at
                            """,
                            (
                                platform, user_id,
                                _utc_stamp(first_seen), _utc_stamp(last_seen),
                            ),
                        )
                        counts["known_users"] += 1

        # Preserve older settings DBs that did not yet have known_users rows.
        target.execute(
            """
            INSERT INTO known_users (platform,user_id,first_seen_at,last_seen_at)
            SELECT platform,user_id,updated_at,updated_at
            FROM user_settings
            ON CONFLICT (platform,user_id) DO NOTHING
            """
        )
        with read_source(bans_path) as bans:
            if bans is not None and table_exists(bans, "bans"):
                rows = bans.execute(
                    """
                    SELECT platform,user_id,reason,banned_by,created_at,expires_at
                    FROM bans
                    """
                )
                for row in rows:
                    target.execute(
                        """
                        INSERT INTO bans (
                            platform,user_id,reason,banned_by,created_at,expires_at
                        ) VALUES (?,?,?,?,?,?)
                        ON CONFLICT (platform,user_id) DO UPDATE SET
                            reason=excluded.reason,
                            banned_by=excluded.banned_by,
                            created_at=excluded.created_at,
                            expires_at=excluded.expires_at
                        """,
                        row,
                    )
                    counts["bans"] += 1

        with read_source(quota_path) as quota:
            if quota is not None and table_exists(quota, "image_search_quota"):
                rows = quota.execute(
                    """
                    SELECT provider,period,used,exhausted
                    FROM image_search_quota
                    """
                )
                for row in rows:
                    target.execute(
                        """
                        INSERT INTO image_search_quota (
                            provider,period,used,exhausted
                        ) VALUES (?,?,?,?)
                        ON CONFLICT (provider,period) DO UPDATE SET
                            used=excluded.used,
                            exhausted=excluded.exhausted
                        """,
                        row,
                    )
                    counts["image_search_quota"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write to the PostgreSQL database; default is read-only summary",
    )
    args = parser.parse_args()
    settings = get_settings().storage
    paths = {
        "user_settings": settings.user_settings_db_path,
        "known_users": settings.user_settings_db_path,
        "bans": settings.bans_db_path,
        "image_search_quota": settings.image_search_db_path,
    }
    print("Source SQLite record counts (source files are never modified):")
    for table, path in paths.items():
        print(f"  {table}: {count_rows(path, table)}")

    if not args.apply:
        print("DRY RUN: add --apply to migrate (stop the bot and back up SQLite first).")
        return
    if not settings.postgres_dsn:
        parser.error("DATABASE_URL must be configured to use --apply")
    pool = PostgresPool(settings.postgres_dsn, max_size=settings.postgres_pool_size)
    try:
        moved = migrate(
            pool,
            settings.user_settings_db_path,
            settings.bans_db_path,
            settings.image_search_db_path,
        )
        print("PostgreSQL migration committed:")
        for table, count in moved.items():
            print(f"  {table}: {count}")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
