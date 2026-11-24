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
    """A user's anime record is independent of its list memberships."""

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
            db.execute("""CREATE TABLE IF NOT EXISTS anime_watch_titles (
                owner BIGINT NOT NULL, title_key TEXT NOT NULL, title TEXT NOT NULL,
                source_ids TEXT NOT NULL, year INTEGER, progress INTEGER NOT NULL DEFAULT 0,
                score DOUBLE PRECISION, PRIMARY KEY(owner,title_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS anime_watch_memberships (
                owner BIGINT NOT NULL, list_id TEXT NOT NULL, title_key TEXT NOT NULL,
                PRIMARY KEY(owner,list_id,title_key))""")
            db.execute("""CREATE INDEX IF NOT EXISTS anime_watch_memberships_title
                ON anime_watch_memberships(owner,title_key)""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS anime_watch_schema (version INTEGER PRIMARY KEY)"
            )
            # Keep the original table untouched as a recovery snapshot.
            if (
                db.execute(
                    "SELECT 1 FROM anime_watch_schema WHERE version=2"
                ).fetchone()
                is None
            ):
                # Conflicting legacy progress: retain the maximum. Prefer an
                # explicit score from the most progressed legacy record.
                rows = db.execute("""SELECT owner,list_id,title_key,title,source_ids,year,progress,score
                    FROM anime_watch_entries ORDER BY owner,title_key,progress DESC,list_id""").fetchall()
                for owner, group, key, title, ids, year, progress, score in rows:
                    existing = db.execute(
                        "SELECT source_ids,year,score FROM anime_watch_titles WHERE owner=? AND title_key=?",
                        (owner, key),
                    ).fetchone()
                    if existing is None:
                        db.execute(
                            "INSERT INTO anime_watch_titles VALUES (?,?,?,?,?,?,?)",
                            (owner, key, title, ids, year, progress, score),
                        )
                    else:
                        identity = AnimeIdentity(json.loads(existing[0]))
                        identity.merge_missing(json.loads(ids))
                        db.execute(
                            "UPDATE anime_watch_titles SET source_ids=?,year=?,score=? WHERE owner=? AND title_key=?",
                            (
                                json.dumps(identity.external_ids),
                                existing[1] if existing[1] is not None else year,
                                existing[2] if existing[2] is not None else score,
                                owner,
                                key,
                            ),
                        )
                    db.execute(
                        "INSERT INTO anime_watch_memberships VALUES (?,?,?) ON CONFLICT DO NOTHING",
                        (
                            owner,
                            "favorites" if group == "env_favorites" else group,
                            key,
                        ),
                    )
                db.execute(
                    "DELETE FROM anime_watch_lists WHERE list_id='env_favorites'"
                )
                db.execute("INSERT INTO anime_watch_schema VALUES (2)")

    def lists(self, owner, defaults):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            for key, label in defaults.items():
                if key != "favorites":
                    db.execute(
                        "INSERT INTO anime_watch_lists VALUES (?,?,?) ON CONFLICT DO NOTHING",
                        (owner, "env_" + key, label),
                    )
            rows = db.execute(
                "SELECT list_id,name FROM anime_watch_lists WHERE owner=? ORDER BY name,list_id",
                (owner,),
            ).fetchall()
            counts = dict(
                db.execute(
                    "SELECT list_id,COUNT(*) FROM anime_watch_memberships WHERE owner=? GROUP BY list_id",
                    (owner,),
                ).fetchall()
            )
            return [(key, key, counts.get(key, 0)) for key in BUILTIN_LISTS] + [
                (key, name, counts.get(key, 0)) for key, name in rows
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
                "INSERT INTO anime_watch_lists VALUES (?,?,?)", (owner, key, name)
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

    @staticmethod
    def _entry(row):
        return WatchEntry(
            row[0],
            SearchCandidate(title=row[1], external_ids=json.loads(row[2]), year=row[3]),
            row[4],
            row[5],
        )

    def entries(self, owner, list_id, page=0, size=10, *, query="", sort="title"):
        ordering = {
            "title": "LOWER(t.title),t.title_key",
            "progress": "t.progress DESC,LOWER(t.title),t.title_key",
            "score": "CASE WHEN t.score IS NULL THEN 1 ELSE 0 END,t.score DESC,LOWER(t.title),t.title_key",
        }[sort]
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            self._check(db, owner, list_id)
            where = "m.owner=? AND m.list_id=?"
            params = [owner, list_id]
            base = (
                " FROM anime_watch_memberships m JOIN anime_watch_titles t ON t.owner=m.owner AND t.title_key=m.title_key WHERE "
                + where
            )
            if query:
                # SQLite LOWER is ASCII-only; Unicode casefold works on both backends.
                matched = [
                    row
                    for row in db.execute(
                        "SELECT t.title_key,t.title,t.source_ids,t.year,t.progress,t.score"
                        + base
                        + " ORDER BY "
                        + ordering,
                        tuple(params),
                    ).fetchall()
                    if query.casefold() in row[1].casefold()
                ]
                return [
                    self._entry(row) for row in matched[page * size : (page + 1) * size]
                ], len(matched)
            count = db.execute("SELECT COUNT(*)" + base, tuple(params)).fetchone()[0]
            rows = db.execute(
                "SELECT t.title_key,t.title,t.source_ids,t.year,t.progress,t.score"
                + base
                + " ORDER BY "
                + ordering
                + " LIMIT ? OFFSET ?",
                (*params, size, page * size),
            ).fetchall()
        return [self._entry(row) for row in rows], count

    def _resolve(self, db, owner, entry):
        row = db.execute(
            "SELECT title_key,title,source_ids,year,progress,score FROM anime_watch_titles WHERE owner=? AND title_key=?",
            (owner, entry.key),
        ).fetchone()
        if row:
            return self._entry(row)
        candidates = []
        for row in db.execute(
            "SELECT title_key,title,source_ids,year,progress,score FROM anime_watch_titles WHERE owner=?",
            (owner,),
        ).fetchall():
            saved = self._entry(row)
            if saved.candidate.identity.matches(entry.candidate.identity):
                candidates.append(saved)
        return candidates[0] if len(candidates) == 1 else None

    def memberships(self, owner, entry):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            saved = self._resolve(db, owner, entry)
            if saved is None:
                return entry, set()
            rows = db.execute(
                "SELECT list_id FROM anime_watch_memberships WHERE owner=? AND title_key=?",
                (owner, saved.key),
            ).fetchall()
            return saved, {r[0] for r in rows}

    def add_many(self, owner, values, limit, *, importing=False):
        # Existing personal progress and scores are authoritative, including zero.
        # Import only adds missing memberships and initializes new shared records.
        changed = 0
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            stored = db.execute(
                "SELECT title_key,title,source_ids,year,progress,score FROM anime_watch_titles WHERE owner=?",
                (owner,),
            ).fetchall()
            records = {row[0]: self._entry(row) for row in stored}
            index = {}

            def remember(entry):
                records[entry.key] = entry
                for pair in entry.candidate.identity.fingerprint():
                    index.setdefault(pair, set()).add(entry.key)

            for entry in tuple(records.values()):
                remember(entry)
            count = db.execute(
                "SELECT COUNT(*) FROM anime_watch_memberships WHERE owner=?", (owner,)
            ).fetchone()[0]
            for list_id, entry in values:
                self._check(db, owner, list_id)
                identity = entry.candidate.identity
                saved = records.get(entry.key)
                if saved is None:
                    matches = set().union(
                        *(index.get(pair, set()) for pair in identity.fingerprint())
                    )
                    matches = [
                        key
                        for key in matches
                        if records[key].candidate.identity.matches(identity)
                    ]
                    if len(matches) == 1:
                        saved = records[matches[0]]
                if saved:
                    merged = saved.candidate.identity
                    merged.merge_missing(identity)
                    entry = replace(
                        saved,
                        candidate=replace(
                            saved.candidate,
                            external_ids=merged.external_ids,
                            year=saved.candidate.year
                            if saved.candidate.year is not None
                            else entry.candidate.year,
                        ),
                    )
                exists = db.execute(
                    "SELECT 1 FROM anime_watch_memberships WHERE owner=? AND list_id=? AND title_key=?",
                    (owner, list_id, entry.key),
                ).fetchone()
                if saved is None and len(records) >= limit:
                    raise ValueError("Saved title capacity exceeded")
                if exists is None and count >= limit:
                    raise ValueError("Watchlist capacity exceeded")
                db.execute(
                    """INSERT INTO anime_watch_titles VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,title_key)
                    DO UPDATE SET source_ids=excluded.source_ids,year=excluded.year""",
                    (
                        owner,
                        entry.key,
                        entry.candidate.title,
                        json.dumps(entry.candidate.external_ids),
                        entry.candidate.year,
                        entry.progress,
                        entry.score,
                    ),
                )
                remember(entry)
                if exists is None:
                    db.execute(
                        "INSERT INTO anime_watch_memberships VALUES (?,?,?)",
                        (owner, list_id, entry.key),
                    )
                    count += 1
                    changed += 1
        return changed

    def set_progress(self, owner, list_id, key, progress):
        if not 0 <= progress <= 100000:
            raise ValueError("Invalid progress")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "UPDATE anime_watch_titles SET progress=? WHERE owner=? AND title_key=?",
                (progress, owner, key),
            )

    def adjust_progress(self, owner, key, delta):
        if delta not in {-1, 1}:
            raise ValueError("Invalid delta")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "UPDATE anime_watch_titles SET progress=CASE WHEN progress+?<0 THEN 0 WHEN progress+?>100000 THEN 100000 ELSE progress+? END WHERE owner=? AND title_key=?",
                (delta, delta, delta, owner, key),
            )

    def set_score(self, owner, key, score):
        if score is not None and not 0 <= score <= 10:
            raise ValueError("Invalid score")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "UPDATE anime_watch_titles SET score=? WHERE owner=? AND title_key=?",
                (score, owner, key),
            )

    def remove(self, owner, list_id, key):
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                "DELETE FROM anime_watch_memberships WHERE owner=? AND list_id=? AND title_key=?",
                (owner, list_id, key),
            )
            # Keep shared personal data even when the last membership is removed.

    def rename(self, owner, list_id, name):
        name = " ".join(name.split())
        if not list_id.startswith("u_") or not name or len(name) > 64:
            raise ValueError("Invalid list name")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            self._check(db, owner, list_id)
            rows = db.execute(
                "SELECT list_id,name FROM anime_watch_lists WHERE owner=?", (owner,)
            ).fetchall()
            if any(k != list_id and n.casefold() == name.casefold() for k, n in rows):
                raise ValueError("List already exists")
            db.execute(
                "UPDATE anime_watch_lists SET name=? WHERE owner=? AND list_id=?",
                (name, owner, list_id),
            )

    def delete(self, owner, list_id):
        if not list_id.startswith("u_"):
            raise ValueError("Built-in list")
        with self._lock, managed_sqlite_connection(self.repository._connect()) as db:
            self._check(db, owner, list_id)
            db.execute(
                "DELETE FROM anime_watch_memberships WHERE owner=? AND list_id=?",
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
