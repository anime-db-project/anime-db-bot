from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineQuery, Update, User

from bots.telegram.allowlist import TelegramAllowlistMiddleware
from bots.telegram.card.text_expansion import record_pages, records_rich_html, long_text_parts
from bots.telegram.card.features.mapping_ids import mapping_ids_pages, mapping_ids_rich
from bots.telegram.command_menu import commands_for
from bots.telegram.inline_mode import deep_link_payload, parse_start_payload
from bots.telegram.card.runtime.picker_entries import picker_prompt
from core.models import PersonCredit, SearchCandidate, SearchPage
from core.people import voice_credits_for_locale
from services.search.service import SearchService


class ProgressiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_provider_is_published_without_waiting_for_second(self):
        release = asyncio.Event()
        published = asyncio.Event()

        class Provider:
            def __init__(self, name, slow=False):
                self.name, self.slow, self.calls = name, slow, 0

            async def search(self, query, *, page, limit):
                self.calls += 1
                if self.slow:
                    await release.wait()
                return SearchPage([SearchCandidate(title="Naruto", external_ids={self.name: "20"})], page, limit, False)

        first, slow = Provider("mal"), Provider("anilist", True)
        service = SearchService((first, slow))
        snapshots = []

        async def observe(result):
            snapshots.append(result)
            published.set()

        one = asyncio.create_task(service.collect("Naruto", on_partial=observe))
        two = asyncio.create_task(service.collect("Naruto"))
        try:
            await asyncio.wait_for(published.wait(), 1)
            self.assertFalse(one.done())
            self.assertFalse(snapshots[0].complete)
            self.assertTrue(snapshots[0].items)
        finally:
            release.set()
            results = await asyncio.gather(one, two)
        self.assertEqual(first.calls, 1)
        self.assertEqual(slow.calls, 1)
        self.assertTrue(results[0].complete)
        self.assertEqual(results[0].items, results[1].items)

    async def test_denied_inline_query_never_reaches_search(self):
        query = InlineQuery(id="test", from_user=User(id=200, is_bot=False, first_name="Test"), query="Naruto", offset="")
        handler = AsyncMock()
        with patch.object(InlineQuery, "answer", new_callable=AsyncMock) as answer:
            await TelegramAllowlistMiddleware((100,))(handler, Update(update_id=1, inline_query=query), {})
        handler.assert_not_awaited()
        answer.assert_awaited_once_with([], cache_time=0, is_personal=True)


class NewLayoutTests(unittest.TestCase):
    def test_records_never_split_and_heading_budget_is_optional(self):
        records = ["X" * 150, "Ken Hagino — Producer", "Y" * 85]
        setting = SimpleNamespace(telegram=SimpleNamespace(long_text_chunk_size=200, long_text_include_ui=False))
        with patch("bots.telegram.card.text_expansion.get_settings", return_value=setting):
            self.assertEqual(record_pages(records, heading="Title"), [records[:2], records[2:]])
            setting.telegram.long_text_include_ui = True
            self.assertEqual(record_pages(records, heading="Heading" * 7), [[records[0]], records[1:]])
            self.assertEqual(record_pages(["A" * 300]), [["A" * 300]])

    def test_voice_language_filters_per_role(self):
        people = [
            PersonCredit("JP", roles=["voice_of::Hero"]),
            PersonCredit("RU", roles=["voice_of::Hero [RU]"]),
            PersonCredit("EN", roles=["voice_of::Hero [EN]"]),
            PersonCredit("Unknown", roles=["voice_of::Hero [UND]"]),
            PersonCredit("Mixed", roles=["voice_of::Hero", "voice_of::Other [FR]"]),
        ]
        result = voice_credits_for_locale(people, "ru")
        self.assertEqual([p.name for p in result], ["JP", "RU", "Mixed"])
        self.assertEqual(result[-1].roles, ["voice_of::Hero"])
        self.assertEqual(len(people[-1].roles), 2)

    def test_mapping_link_and_table(self):
        page = mapping_ids_pages("amdb:1", {"anidb": ("239",), "tmdb_movie": ("2",)}, locale="ru")[0]
        value = mapping_ids_rich(page).html
        self.assertIn("https://anidb.net/anime/239", value)
        self.assertIn("TMDB MOVIE", value)
        self.assertNotIn("Anime Mapping DB", value)
        self.assertIn("<table", value)

    def test_inline_payload_and_scoped_commands(self):
        candidate = SearchCandidate(title="Naruto", external_ids={"mal": "20"})
        self.assertEqual(parse_start_payload(deep_link_payload(candidate)), ("mal", "20"))
        self.assertIsNone(parse_start_payload("anime_mal_20_invalid"))
        self.assertNotIn("ban", [c.command for c in commands_for("ru")])
        self.assertIn("ban", [c.command for c in commands_for("ru", administrator=True)])
        self.assertEqual(picker_prompt("ap~jikan", "ru"), "Выберите серию:")
