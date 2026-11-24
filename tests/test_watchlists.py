from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from core.models import SearchCandidate
from services.users.settings import SQLiteUserSettingsStore
from services.users.watchlists import WatchListStore, entry_for, STATUSES
from services.users.watchlist_import import parse_mal_xml, imported
from bots.telegram.inline_mode import InlineChoice, preview
from bots.telegram.search.handlers import SearchSession


class WatchListsTests(unittest.TestCase):
    def test_status_move_custom_lists_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.db"
            store = WatchListStore(SQLiteUserSettingsStore(path))
            default = {"favorites": "Favorites"}
            self.assertEqual(
                [v[0] for v in store.lists(1, default)],
                list(STATUSES) + ["env_favorites"],
            )
            custom = store.create(1, "Weekend", 30)
            entry = entry_for(
                SearchCandidate("Naruto", external_ids={"mal": "20"}), progress=7
            )
            store.add_many(1, [("watching", entry), (custom, entry)], 10)
            store.set_progress(1, "watching", entry.key, 12)
            updated = entry_for(
                SearchCandidate("Naruto", external_ids={"mal": "20", "anilist": "20"})
            )
            store.add_many(1, [("completed", updated)], 10)
            self.assertEqual(store.entries(1, "watching")[1], 0)
            self.assertEqual(store.entries(1, "completed")[0][0].progress, 12)
            self.assertEqual(store.entries(1, custom)[1], 1)
            self.assertEqual(store.entries(2, "completed")[1], 0)
            with self.assertRaises(ValueError):
                store.entries(2, custom)
            # Imported status does not overwrite the user's chosen status.
            self.assertEqual(
                store.add_many(1, [("dropped", updated)], 10, importing=True), 0
            )
            second = WatchListStore(SQLiteUserSettingsStore(path))
            self.assertEqual(
                second.entries(1, "completed")[0][0].candidate.external_ids["mal"], "20"
            )
            second.delete(1, custom)
            with self.assertRaises(ValueError):
                second.entries(1, custom)

    def test_import_is_atomic_on_capacity_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchListStore(SQLiteUserSettingsStore(Path(tmp) / "users.db"))
            first = entry_for(SearchCandidate("A", external_ids={"mal": "1"}))
            second = entry_for(SearchCandidate("B", external_ids={"mal": "2"}))
            with self.assertRaises(ValueError):
                store.add_many(1, [("planned", first), ("planned", second)], 1)
            self.assertEqual(store.entries(1, "planned")[1], 0)

    def test_mal_export_status_progress_and_security(self):
        xml = b"<myanimelist><anime><series_animedb_id>20</series_animedb_id><series_title>Naruto</series_title><my_status>Watching</my_status><my_watched_episodes>7</my_watched_episodes><my_score>9</my_score></anime></myanimelist>"
        for data in (xml, gzip.compress(xml)):
            row = parse_mal_xml(data, 10)[0]
            self.assertEqual(
                (row[0], row[1].external_ids, row[2], row[3]),
                ("watching", {"mal": "20"}, 7, 9.0),
            )
        with self.assertRaises(ValueError):
            parse_mal_xml(b"<!DOCTYPE a [<!ENTITY x 'hello'>]><myanimelist/>", 10)
        self.assertEqual(
            imported("shikimori", "20", "Naruto", "on_hold", 4)[0], "paused"
        )

    def test_inline_preview_retains_navigation_and_ids(self):
        session = SearchSession(
            "Naruto",
            10,
            [
                SearchCandidate("Naruto", external_ids={"mal": "20"}),
                SearchCandidate("Naruto Shippuden", external_ids={"mal": "1735"}),
            ],
        )
        plain, rich, keyboard = preview(InlineChoice(session, 10), "token", 1, "ru")
        self.assertIn("1735", plain)
        self.assertIn("<table", rich.html)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("isd:token:1", callbacks)
        self.assertIn("isr:token:0", callbacks)
        self.assertTrue(any(v.startswith("isp:") for v in callbacks))


class PublicImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_public_sources(self):
        from unittest.mock import AsyncMock, patch
        from services.users.watchlist_import import fetch_public

        fixtures = {
            "anilist": (
                "Test",
                {
                    "data": {
                        "MediaListCollection": {
                            "hasNextChunk": False,
                            "lists": [
                                {
                                    "entries": [
                                        {
                                            "mediaId": 20,
                                            "status": "CURRENT",
                                            "progress": 12,
                                            "score": 8,
                                            "media": {
                                                "idMal": 20,
                                                "title": {"romaji": "Naruto"},
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                },
            ),
            "shikimori": (
                "123",
                [
                    {
                        "anime": {"id": 20, "name": "Naruto"},
                        "status": "watching",
                        "episodes": 12,
                        "score": 8,
                    }
                ],
            ),
            "kitsu": (
                "123",
                {
                    "data": [
                        {
                            "attributes": {
                                "status": "current",
                                "progress": 12,
                                "ratingTwenty": 16,
                            },
                            "relationships": {"anime": {"data": {"id": "20"}}},
                        }
                    ],
                    "included": [
                        {
                            "type": "anime",
                            "id": "20",
                            "attributes": {"canonicalTitle": "Naruto"},
                        }
                    ],
                    "links": {},
                },
            ),
        }
        for source, (account, payload) in fixtures.items():
            with (
                self.subTest(source=source),
                patch(
                    "services.users.watchlist_import._json",
                    new=AsyncMock(return_value=payload),
                ),
            ):
                rows = await fetch_public(source, account, 1000)
                self.assertEqual(len(rows), 1)
                status, candidate, progress, score = rows[0]
                self.assertEqual(
                    (
                        status,
                        candidate.title,
                        candidate.external_ids[source],
                        progress,
                        score,
                    ),
                    ("watching", "Naruto", "20", 12, 8.0),
                )
