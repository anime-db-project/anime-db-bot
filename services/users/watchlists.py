from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, replace
from functools import lru_cache

from core.models import SearchCandidate
from core.identity import AnimeIdentity
from core.sqlite import managed_sqlite_connection
from services.users.settings import get_user_settings_store

STATUSES = ("watching", "completed", "dropped", "planned", "paused", "rewatching")
BUILTIN_LISTS = (*STATUSES, "favorites")


@dataclass(frozen=True, slots=True)
class WatchEntry:
    key: str
    candidate: SearchCandidate
    progress: int = 0
    score: float | None = None


def entry_for(candidate, resolver=None, *, progress=0, score=None):
    identity = candidate.identity
    if identity.is_empty():
        raise ValueError("Cannot store a title without a source ID")
    anchor = getattr(candidate, "amdb_id", None)
    if not anchor and resolver is not None:
        anchor = resolver.anchor_for(identity)
    # AMDB is authoritative when available; otherwise persist exact source IDs.
    key = (
        anchor
        or "src:"
        + hashlib.sha256(repr(identity.fingerprint()).encode()).hexdigest()[:32]
    )
    return WatchEntry(
        key,
        SearchCandidate(
            title=candidate.title[:512],
            external_ids=dict(identity.external_ids),
            year=getattr(candidate, "year", None),
        ),
        max(0, int(progress or 0)),
        score,
    )


class WatchListStore:
    def __init__(self, repository):
        self.repository = repository
        self._lock = threading.RLock()
        with managed_sqlite_connection(repository._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS anime_watch_lists (
                owner BIGINT NOT NULL, list_id TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(owner,list_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS anime_watch_entries (
                owner BIGINT NOT NULL, list_id TEXT NOT NULL, title_key TEXT NOT NULL,
                title TEXT NOT NULL, source_ids TEXT NOT NULL, year INTEGER,
                progress INTEGER NOT NULL DEFAULT 0, score DOUBLE PRECISION,
                PRIMARY KEY(owner,list_id,title_key))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS anime_watch_entries_owner_key ON anime_watch_entries(owner,title_key)"
            )

    def lists(self, owner, defaults):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            # Persist operator-created lists on first use; removing an ENV key
            # cannot make a user's saved titles disappear.
            # Older deployments may have an ENV list called favorites.
            # Merge it into the permanent list without overwriting saved entries.
            db.execute(
                """INSERT INTO anime_watch_entries(owner,list_id,title_key,title,source_ids,year,progress,score)
                SELECT owner,'favorites',title_key,title,source_ids,year,progress,score
                FROM anime_watch_entries WHERE owner=? AND list_id='env_favorites'
                ON CONFLICT(owner,list_id,title_key) DO NOTHING""",
                (owner,),
            )
            db.execute(
                "DELETE FROM anime_watch_entries WHERE owner=? AND list_id='env_favorites'",
                (owner,),
            )
            db.execute(
                "DELETE FROM anime_watch_lists WHERE owner=? AND list_id='env_favorites'",
                (owner,),
            )
            for key, label in defaults.items():
                if key == "favorites":
                    continue
                db.execute(
                    "INSERT INTO anime_watch_lists(owner,list_id,name) VALUES (?,?,?) ON CONFLICT(owner,list_id) DO NOTHING",
                    (owner, "env_" + key, label),
                )
            rows = db.execute(
                "SELECT list_id,name FROM anime_watch_lists WHERE owner=? ORDER BY name,list_id",
                (owner,),
            ).fetchall()
            counts = db.execute(
                "SELECT list_id,COUNT(*) FROM anime_watch_entries WHERE owner=? GROUP BY list_id",
                (owner,),
            ).fetchall()
        count = {row[0]: row[1] for row in counts}
        return [(key, key, count.get(key, 0)) for key in BUILTIN_LISTS] + [
            (r[0], r[1], count.get(r[0], 0)) for r in rows
        ]

    def create(self, owner, name, limit):
        name = " ".join(name.split())
        if not name or len(name) > 64:
            raise ValueError("Invalid list name")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            rows = db.execute(
                "SELECT list_id,name FROM anime_watch_lists WHERE owner=?", (owner,)
            ).fetchall()
            if any(row[1].casefold() == name.casefold() for row in rows):
                raise ValueError("List already exists")
            if sum(row[0].startswith("u_") for row in rows) >= limit:
                raise ValueError("Custom list limit reached")
            key = "u_" + secrets.token_hex(6)
            db.execute(
                "INSERT INTO anime_watch_lists(owner,list_id,name) VALUES (?,?,?)",
                (owner, key, name),
            )
            return key

    def _check(self, db, owner, list_id):
        if (
            list_id not in BUILTIN_LISTS
            and db.execute(
                "SELECT 1 FROM anime_watch_lists WHERE owner=? AND list_id=?",
                (owner, list_id),
            ).fetchone()
            is None
        ):
            raise ValueError("Unknown list")

    def entries(self, owner, list_id, page=0, size=10):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            self._check(db, owner, list_id)
            count = db.execute(
                "SELECT COUNT(*) FROM anime_watch_entries WHERE owner=? AND list_id=?",
                (owner, list_id),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT title_key,title,source_ids,year,progress,score FROM anime_watch_entries WHERE owner=? AND list_id=? ORDER BY LOWER(title),title_key LIMIT ? OFFSET ?",
                (owner, list_id, size, page * size),
            ).fetchall()
        return [
            WatchEntry(
                r[0],
                SearchCandidate(title=r[1], external_ids=json.loads(r[2]), year=r[3]),
                r[4],
                r[5],
            )
            for r in rows
        ], count

    def add_many(self, owner, values, limit, *, importing=False):
        # All changes are atomic. Status lists are exclusive; custom lists are independent.
        # A local per-repository lock serializes the count/cap and writes.
        changed = 0
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            stored = db.execute(
                "SELECT title_key,source_ids FROM anime_watch_entries WHERE owner=?",
                (owner,),
            ).fetchall()
            count = len(stored)
            identities = {}
            index = {}

            def remember(key, ids):
                current = identities.setdefault(key, AnimeIdentity({}))
                current.merge_missing(ids)
                for pair in current.external_ids.items():
                    index.setdefault(pair, set()).add(key)

            for row in stored:
                remember(row[0], json.loads(row[1]))
            for list_id, entry in values:
                identity = entry.candidate.identity
                matches = set().union(
                    *(index.get(pair, set()) for pair in identity.external_ids.items())
                )
                compatible = [
                    key for key in matches if identities[key].matches(identity)
                ]
                if entry.key not in identities and len(compatible) == 1:
                    key = compatible[0]
                    merged = identities[key].copy()
                    merged.merge_missing(identity)
                    entry = replace(
                        entry,
                        key=key,
                        candidate=replace(
                            entry.candidate, external_ids=merged.external_ids
                        ),
                    )
                remember(entry.key, entry.candidate.external_ids)
                self._check(db, owner, list_id)
                # Preserve the user's own status when importing a title already tracked.
                current = db.execute(
                    "SELECT list_id,progress,score FROM anime_watch_entries WHERE owner=? AND title_key=?",
                    (owner, entry.key),
                ).fetchall()
                existing = next((r for r in current if r[0] == list_id), None)
                old_status = [r for r in current if r[0] in STATUSES]
                if importing and list_id in STATUSES and old_status:
                    continue
                if importing and existing:
                    continue
                removed = 0
                if list_id in STATUSES:
                    for row in old_status:
                        if row[0] != list_id:
                            db.execute(
                                "DELETE FROM anime_watch_entries WHERE owner=? AND list_id=? AND title_key=?",
                                (owner, row[0], entry.key),
                            )
                            removed += 1
                count -= removed
                if existing is None and count >= limit:
                    raise ValueError("Watchlist capacity exceeded")
                previous = existing or (
                    old_status[0] if list_id in STATUSES and old_status else None
                )
                progress = (
                    entry.progress if importing or previous is None else previous[1]
                )
                score = entry.score if importing or previous is None else previous[2]
                db.execute(
                    """INSERT INTO anime_watch_entries(owner,list_id,title_key,title,source_ids,year,progress,score)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner,list_id,title_key) DO UPDATE SET
                    title=excluded.title,source_ids=excluded.source_ids,year=excluded.year,
                    progress=excluded.progress,score=excluded.score""",
                    (
                        owner,
                        list_id,
                        entry.key,
                        entry.candidate.title,
                        json.dumps(entry.candidate.external_ids),
                        entry.candidate.year,
                        progress,
                        score,
                    ),
                )
                count += int(existing is None)
                changed += 1
        return changed

    def set_progress(self, owner, list_id, key, progress):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "UPDATE anime_watch_entries SET progress=? WHERE owner=? AND list_id=? AND title_key=?",
                (progress, owner, list_id, key),
            )

    def remove(self, owner, list_id, key):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "DELETE FROM anime_watch_entries WHERE owner=? AND list_id=? AND title_key=?",
                (owner, list_id, key),
            )

    def delete(self, owner, list_id):
        if not list_id.startswith("u_"):
            raise ValueError("Built-in list")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            self._check(db, owner, list_id)
            db.execute(
                "DELETE FROM anime_watch_entries WHERE owner=? AND list_id=?",
                (owner, list_id),
            )
            db.execute(
                "DELETE FROM anime_watch_lists WHERE owner=? AND list_id=?",
                (owner, list_id),
            )


_factory_lock = threading.Lock()


@lru_cache(maxsize=8)
def _store(repository):
    return WatchListStore(repository)


def watchlists():
    with _factory_lock:
        return _store(get_user_settings_store())
