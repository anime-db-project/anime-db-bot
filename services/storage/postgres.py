from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from core.settings import get_settings


class PostgresRow:
    """Small row adapter supporting sqlite-style ordinal and column access."""

    def __init__(self, values: Sequence[Any], columns: Sequence[str]) -> None:
        self._values = values
        self._columns = {name: index for index, name in enumerate(columns)}

    def __getitem__(self, index: int | str) -> Any:
        if isinstance(index, str):
            return self._values[self._columns[index]]
        return self._values[index]


class PostgresCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> PostgresRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return PostgresRow(row, [item.name for item in self._cursor.description])

    def fetchall(self) -> list[PostgresRow]:
        columns = [item.name for item in self._cursor.description]
        return [PostgresRow(row, columns) for row in self._cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)


class PostgresConnection:
    """SQL parameter dialect adapter with explicit transaction and pool release.

    Repositories use only trusted, hardcoded SQL statements; placeholders are
    translated from sqlite's '?' to psycopg's '%s' for shared DML methods.
    """

    def __init__(self, pool: Any, connection: Any) -> None:
        self._pool = pool
        self._connection = connection
        self._closed = False

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, typ: Any, exc: Any, tb: Any) -> None:
        try:
            if typ is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self.close()

    def execute(
        self, statement: str, params: Sequence[object] = ()
    ) -> PostgresCursor:
        cursor = self._connection.execute(statement.replace("?", "%s"), params)
        return PostgresCursor(cursor)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pool.putconn(self._connection)


class PostgresPool:
    """A bounded shared PostgreSQL pool, activated only when selected in env."""

    def __init__(self, dsn: str, *, max_size: int = 5) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL requires optional dependencies: "
                "pip install -r requirements-backends.txt"
            ) from exc
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=max(1, max_size),
            timeout=5.0,
            open=True,
        )

    def connection(self) -> PostgresConnection:
        conn = self._pool.getconn()
        return PostgresConnection(self._pool, conn)

    def close(self) -> None:
        self._pool.close()


@lru_cache(maxsize=1)
def default_postgres_pool() -> PostgresPool:
    storage = get_settings().storage
    if storage.backend != "postgres":
        raise RuntimeError("PostgreSQL is not configured")
    return PostgresPool(storage.postgres_dsn, max_size=storage.postgres_pool_size)
