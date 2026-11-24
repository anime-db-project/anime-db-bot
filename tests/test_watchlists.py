from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from core.models import SearchCandidate
from services.users.settings import SQLiteUserSettingsStore
from services.users.watchlists import WatchListStore, entry_for, STATUSES, BUILTIN_LISTS
from services.users.watchlist_import import parse_mal_xml, imported
from bots.telegram.inline_mode import InlineChoice, preview
from bots.telegram.search.handlers import SearchSession


class WatchListsTests(unittest.TestCase):
    def test_shared_data_independent_memberships_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.db"
            store = WatchListStore(SQLiteUserSettingsStore(path))
            default = {"favorites": "Favorites"}
            self.assertEqual(
                [v[0] for v in store.lists(1, default)],
                list(BUILTIN_LISTS),
            )
            custom = store.create(1, "Weekend", 30)
            entry = entry_for(
                SearchCandidate("Naruto", external_ids={"mal": "20"}), progress=7
            )
            store.add_many(
                1, [("watching", entry), (custom, entry), ("favorites", entry)], 10
            )
            store.set_progress(1, "watching", entry.key, 12)
            updated = entry_for(
                SearchCandidate("Naruto", external_ids={"mal": "20", "anilist": "20"})
            )
            store.add_many(1, [("completed", updated)], 10)
            self.assertEqual(store.entries(1, "watching")[1], 1)
            self.assertEqual(store.entries(1, "completed")[0][0].progress, 12)
            self.assertEqual(store.entries(1, custom)[1], 1)
            self.assertEqual(store.entries(1, "favorites")[1], 1)
            self.assertEqual(store.entries(2, "favorites")[1], 0)
            self.assertEqual(store.entries(2, "completed")[1], 0)
            with self.assertRaises(ValueError):
                store.entries(2, custom)
            # Import adds missing memberships without overwriting shared data.
            self.assertEqual(
                store.add_many(1, [("dropped", updated)], 10, importing=True), 1
            )
            saved = store.entries(1, "completed")[0][0]
            store.set_score(1, saved.key, 9.5)
            store.adjust_progress(1, saved.key, 1)
            for list_id in ("watching", "completed", "favorites", "dropped", custom):
                current = store.entries(1, list_id)[0][0]
                self.assertEqual((current.progress, current.score), (13, 9.5))
                self.assertEqual(
                    current.candidate.external_ids, {"mal": "20", "anilist": "20"}
                )
            store.remove(1, "watching", saved.key)
            self.assertEqual(store.entries(1, "watching")[1], 0)
            self.assertEqual(store.entries(1, "completed")[1], 1)
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

    def test_legacy_migration_merges_progress_rating_and_memberships(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE anime_watch_lists(owner BIGINT,list_id TEXT,name TEXT,PRIMARY KEY(owner,list_id))"
                )
                db.execute(
                    "CREATE TABLE anime_watch_entries(owner BIGINT,list_id TEXT,title_key TEXT,title TEXT,source_ids TEXT,year INTEGER,progress INTEGER,score DOUBLE PRECISION,PRIMARY KEY(owner,list_id,title_key))"
                )
                db.execute(
                    "INSERT INTO anime_watch_lists VALUES (1,'env_favorites','Favorites')"
                )
                db.executemany(
                    "INSERT INTO anime_watch_entries VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            1,
                            "env_favorites",
                            "amdb:1",
                            "Naruto",
                            '{"mal":"20"}',
                            2002,
                            3,
                            9,
                        ),
                        (
                            1,
                            "watching",
                            "amdb:1",
                            "Naruto",
                            '{"anilist":"20","mal":"20"}',
                            2002,
                            15,
                            None,
                        ),
                    ],
                )
            store = WatchListStore(SQLiteUserSettingsStore(path))
            self.assertEqual(
                [x[0] for x in store.lists(1, {"favorites": "Favorites"})],
                list(BUILTIN_LISTS),
            )
            entry = store.entries(1, "favorites")[0][0]
            self.assertEqual(
                (entry.progress, entry.score, entry.candidate.external_ids),
                (15, 9, {"mal": "20", "anilist": "20"}),
            )
            self.assertEqual(store.entries(1, "watching")[0][0], entry)
            store.remove(1, "favorites", entry.key)
            store = WatchListStore(SQLiteUserSettingsStore(path))
            self.assertEqual(store.entries(1, "favorites")[1], 0)
            self.assertEqual(store.entries(1, "watching")[1], 1)

    def test_filter_sort_rename_and_retained_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchListStore(SQLiteUserSettingsStore(Path(tmp) / "users.db"))
            custom = store.create(1, "Weekend", 30)
            one = entry_for(
                SearchCandidate("Наруто", external_ids={"mal": "20"}),
                progress=7,
                score=8,
            )
            two = entry_for(
                SearchCandidate("Блич", external_ids={"mal": "269"}),
                progress=12,
                score=9,
            )
            store.add_many(1, [(custom, one), (custom, two)], 10)
            self.assertEqual(store.entries(1, custom, query="НАРУ")[1], 1)
            self.assertEqual(store.entries(1, custom, sort="score")[0][0].key, two.key)
            store.rename(1, custom, "New")
            self.assertIn((custom, "New", 2), store.lists(1, {}))
            store.remove(1, custom, one.key)
            store.add_many(1, [("planned", one)], 10)
            self.assertEqual(store.entries(1, "planned")[0][0].progress, 7)
            self.assertEqual(store.entries(1, "planned")[0][0].score, 8)

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


class WatchlistUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_select_and_shared_progress_through_handlers(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from aiogram import Dispatcher
        from bots.telegram.watchlists import register_watchlist_handlers

        with tempfile.TemporaryDirectory() as tmp:
            store = WatchListStore(SQLiteUserSettingsStore(Path(tmp) / "ui.db"))
            anime = SearchCandidate("Naruto", external_ids={"mal": "20"}, year=2002)
            state = SimpleNamespace(
                anime_sessions={"card": SimpleNamespace(owner_user_id=7, anime=anime)}
            )
            dp = Dispatcher()
            register_watchlist_handlers(dp, state)
            handlers = {
                h.callback.__name__: h.callback for h in dp.callback_query.handlers
            }
            message = SimpleNamespace(
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
                answer_rich=AsyncMock(),
                edit_text=AsyncMock(),
            )
            actor = SimpleNamespace(id=7)

            def callback(data):
                return SimpleNamespace(
                    from_user=actor, message=message, data=data, answer=AsyncMock()
                )

            def markup():
                calls = message.edit_text.await_args or message.answer_rich.await_args
                return calls.kwargs["reply_markup"]

            def find(label):
                return next(
                    b.callback_data
                    for row in markup().inline_keyboard
                    for b in row
                    if label in b.text
                )

            with (
                patch("bots.telegram.watchlists.watchlists", return_value=store),
                patch("bots.telegram.watchlists.get_user_locale", return_value="ru"),
                patch(
                    "bots.telegram.watchlists.get_app_context",
                    return_value=SimpleNamespace(
                        identity_resolver=SimpleNamespace(anchor_for=lambda _: None)
                    ),
                ),
            ):
                await handlers["card_add"](callback("wlcard:card"))
                await handlers["list_action"](callback(find("Смотрю")))
                await handlers["list_action"](callback(find("Избранное")))
                self.assertTrue(find("✓ Смотрю"))
                self.assertTrue(find("✓ Избранное"))
                await handlers["list_action"](callback(find("Готово")))
                await handlers["list_action"](callback(find("+1")))
                self.assertEqual(store.entries(7, "watching")[0][0].progress, 1)
                self.assertEqual(store.entries(7, "favorites")[0][0].progress, 1)
                await handlers["list_action"](callback(find("Управление списками")))
                await handlers["list_action"](callback(find("✓ Смотрю")))
                self.assertEqual(store.entries(7, "watching")[1], 0)
                self.assertEqual(store.entries(7, "favorites")[1], 1)
                self.assertIn(
                    "<table", message.edit_text.await_args.kwargs["rich_message"].html
                )

    async def test_list_add_uses_command_search_and_keeps_preview_available(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from aiogram import Dispatcher
        from aiogram.filters.command import CommandObject
        from bots.telegram.watchlists import register_watchlist_handlers
        from bots.telegram.search import handlers as search
        from core.models import SearchResultSet

        with tempfile.TemporaryDirectory() as tmp:
            store = WatchListStore(SQLiteUserSettingsStore(Path(tmp) / "ui.db"))
            anime = SearchCandidate("Naruto", external_ids={"mal": "20"}, year=2002)
            other = SearchCandidate("Bleach", external_ids={"mal": "269"})
            result = SearchResultSet([anime, other])
            actor = SimpleNamespace(id=7)
            chat = SimpleNamespace(id=7, type="private")
            sent = SimpleNamespace(chat=chat, edit_text=AsyncMock())
            message = SimpleNamespace(
                chat=chat,
                from_user=actor,
                text="Naruto",
                photo=None,
                document=None,
                reply_to_message=None,
                answer=AsyncMock(return_value=sent),
                answer_rich=AsyncMock(),
                edit_text=AsyncMock(),
            )
            dp = Dispatcher()

            async def start(message, owner, target):
                await search.open_search_scope(
                    message, state, owner, watchlist_target=target
                )

            chooser = register_watchlist_handlers(
                dp, SimpleNamespace(anime_sessions={}), start_search=start
            )

            async def collect(query, *, on_partial):
                await on_partial(result)
                return result

            service = SimpleNamespace(
                settings=SimpleNamespace(
                    default_provider_keys=search.ALL_PROVIDER_KEYS,
                    expansion_enabled=False,
                    collection_max_items=120,
                ),
                collect=AsyncMock(side_effect=collect),
            )
            lookup = SimpleNamespace(
                source_keys=("mal",),
                lookup=AsyncMock(return_value=SearchResultSet([anime])),
            )
            aggregate = AsyncMock()
            state = search.register_search_handlers(
                dp,
                aggregate_callback=aggregate,
                watchlist_callback=chooser,
                default_service=service,
                direct_lookup=lookup,
                provider_map={},
                image_search_service=object(),
                image_search_max_bytes=1024,
                input_ttl_seconds=300,
            )
            handlers = {
                h.callback.__name__: h.callback for h in dp.callback_query.handlers
            }
            commands = {h.callback.__name__: h.callback for h in dp.message.handlers}

            def callback(data, who=actor, where=message):
                return SimpleNamespace(
                    data=data, from_user=who, message=where, answer=AsyncMock()
                )

            def find(call, label):
                return next(
                    b.callback_data
                    for row in call.kwargs["reply_markup"].inline_keyboard
                    for b in row
                    if label in b.text
                )

            with (
                patch("bots.telegram.watchlists.watchlists", return_value=store),
                patch("bots.telegram.watchlists.get_user_locale", return_value="ru"),
                patch(
                    "bots.telegram.search.handlers.get_user_locale", return_value="ru"
                ),
                patch(
                    "bots.telegram.watchlists.get_app_context",
                    return_value=SimpleNamespace(
                        identity_resolver=SimpleNamespace(anchor_for=lambda _: None)
                    ),
                ),
            ):
                await commands["open_lists"](message)
                await handlers["list_action"](
                    callback(find(message.answer_rich.await_args, "В планах"))
                )
                await handlers["list_action"](
                    callback(find(message.edit_text.await_args, "Добавить аниме"))
                )
                source_keyboard = message.answer.await_args.kwargs["reply_markup"]
                all_sources = source_keyboard.inline_keyboard[0][0].callback_data
                await handlers["scope_callback"](callback(all_sources))
                self.assertEqual(
                    state.pending_inputs["7"].watchlist_target.list_id, "planned"
                )
                await commands["pending_anime_input_handler"](message)
                service.collect.assert_awaited_once()
                sw = find(sent.edit_text.await_args, "Добавить в список")
                self.assertIn(
                    "В планах", sent.edit_text.await_args.kwargs["rich_message"].html
                )
                await handlers["result_watchlist_callback"](
                    callback(sw, who=SimpleNamespace(id=8))
                )
                self.assertEqual(store.entries(7, "planned")[1], 0)
                await handlers["result_watchlist_callback"](callback(sw))
                self.assertEqual(store.entries(7, "planned")[1], 1)
                self.assertTrue(find(message.answer_rich.await_args, "✓ В планах"))
                token = sw.split(":")[1]
                self.assertTrue(state.sessions[token].preview_active)
                await handlers["result_page_callback"](
                    callback("sr:" + token + ":1", where=sent)
                )
                self.assertIn(
                    "Bleach", sent.edit_text.await_args.kwargs["rich_message"].html
                )
                await handlers["result_watchlist_callback"](
                    callback(find(sent.edit_text.await_args, "Добавить в список"))
                )
                self.assertEqual(store.entries(7, "planned")[1], 2)
                aggregate.assert_not_awaited()

                # Ordinary /anime ID search opens the selector without saving a
                # membership automatically, and does not inherit the earlier list.
                await commands["anime_handler"](
                    message, CommandObject(command="anime", args="mal:20")
                )
                sw = find(sent.edit_text.await_args, "Добавить в список")
                self.assertIsNone(state.sessions[sw.split(":")[1]].watchlist_target)
                await handlers["result_watchlist_callback"](callback(sw))
                self.assertTrue(find(message.answer_rich.await_args, "✓ В планах"))
                await handlers["list_action"](
                    callback(find(message.answer_rich.await_args, "Избранное"))
                )
                self.assertEqual(store.entries(7, "favorites")[1], 1)
                self.assertEqual(store.entries(7, "planned")[1], 2)
                aggregate.assert_not_awaited()
