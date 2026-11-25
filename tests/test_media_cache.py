from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendPhoto
from aiogram.types import InputRichMessage
from bots.telegram.media_cache import MediaIndex, TelegramMediaCache
from bots.telegram.rich_media import deliver_rich_media
from core.settings import get_settings
from services.users.settings import (
    SQLiteUserSettingsStore,
    activate_user_settings_store,
)


def photo(file_id, message_id):
    return SimpleNamespace(
        message_id=message_id,
        photo=[SimpleNamespace(width=100, height=100, file_id=file_id)],
    )


class TelegramMediaCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_cache_bot_scope_and_alias_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteUserSettingsStore(Path(tmp) / "test.db")
            settings = replace(
                get_settings().telegram,
                media_cache_enabled=True,
                media_cache_persistent=True,
                media_cache_chat_id=0,
                media_cache_ttl_seconds=0,
            )
            with activate_user_settings_store(repo):
                cache = TelegramMediaCache(settings)
                await cache.remember(
                    1, "https://example.org/a", "file-one", digest="hash"
                )
                await cache.remember(
                    1, "https://example.org/b", "file-one", digest="hash"
                )
                restarted = TelegramMediaCache(settings)
                self.assertEqual(
                    await restarted.lookup(1, "https://example.org/a"), "file-one"
                )
                self.assertIsNone(await restarted.lookup(2, "https://example.org/a"))
                self.assertEqual((await restarted.summary(1))["files"], 1)
                await restarted.forget(1, "https://example.org/a")
                self.assertIsNone(await restarted.lookup(1, "https://example.org/b"))
                self.assertEqual((await restarted.summary(1))["entries"], 0)

    async def test_album_dedup_and_restart_without_reupload(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteUserSettingsStore(Path(tmp) / "test.db")
            settings = replace(
                get_settings().telegram,
                media_cache_enabled=True,
                media_cache_persistent=True,
                media_cache_chat_id=-100123,
                media_cache_background_upload=True,
                media_cache_keep_messages=True,
                media_cache_deduplication=True,
                media_cache_interval_seconds=0.01,
                media_cache_batch_size=10,
            )
            urls = [
                "https://example.org/a",
                "https://example.org/b",
                "https://example.org/c",
            ]
            manager = SimpleNamespace(
                settings=settings.rich_media,
                _fetch=AsyncMock(
                    side_effect=[
                        b"\xff\xd8\xffone",
                        b"\xff\xd8\xffone",
                        b"\xff\xd8\xfftwo",
                    ]
                ),
            )
            bot = SimpleNamespace(
                id=77,
                send_media_group=AsyncMock(
                    return_value=[photo("one", 11), photo("two", 12)]
                ),
                send_photo=AsyncMock(),
                delete_message=AsyncMock(),
            )
            with (
                activate_user_settings_store(repo),
                patch(
                    "bots.telegram.rich_media.get_rich_media_delivery",
                    return_value=manager,
                ),
            ):
                cache = TelegramMediaCache(settings)
                cache.enqueue(bot, urls + urls)
                await asyncio.wait_for(cache.queue.join(), 3)
                bot.send_media_group.assert_awaited_once()
                self.assertEqual(len(bot.send_media_group.await_args.args[1]), 2)
                bot.send_photo.assert_not_awaited()
                bot.delete_message.assert_not_awaited()
                restarted = TelegramMediaCache(settings)
                self.assertEqual(
                    [await restarted.lookup(77, url) for url in urls],
                    ["one", "one", "two"],
                )
                self.assertEqual((await restarted.summary(77))["files"], 2)
                await cache.close()

    async def test_retry_after_pacing_and_disabled_channel(self):
        settings = replace(
            get_settings().telegram,
            media_cache_interval_seconds=0.01,
            media_cache_chat_id=0,
        )
        cache = TelegramMediaCache(settings)
        retry = TelegramRetryAfter(
            method=SendPhoto(chat_id=-100123, photo="file"),
            message="rate limit",
            retry_after=0,
        )
        send = AsyncMock(side_effect=[retry, "ok"])
        self.assertEqual(
            await cache._paced(send, asyncio.get_running_loop().time() + 2), "ok"
        )
        self.assertEqual(cache.stats["rate_limited"], 1)
        bot = SimpleNamespace(id=7, send_photo=AsyncMock())
        self.assertIsNone(
            await cache.stage(
                bot, "https://example.org/a", asyncio.get_running_loop().time() + 1
            )
        )
        cache.enqueue(bot, ["https://example.org/a"])
        bot.send_photo.assert_not_awaited()
        self.assertTrue(cache.queue.empty())

    async def test_regular_rich_uses_persistent_file_ids_and_preserves_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SQLiteUserSettingsStore(Path(tmp) / "test.db")
            settings = replace(
                get_settings().telegram,
                media_cache_enabled=True,
                media_cache_persistent=True,
                media_cache_chat_id=0,
            )
            with activate_user_settings_store(repo):
                cache = TelegramMediaCache(settings)
                url = "https://example.org/a.jpg"
                await cache.remember(12, url, "known")
                bot = SimpleNamespace(id=12)
                send = AsyncMock(return_value=True)
                rich = InputRichMessage(
                    html='<img src="'
                    + url
                    + '"/><table><tr><td>Title</td></tr></table><tg-button-row><tg-button>Open</tg-button></tg-button-row>'
                )
                with patch(
                    "bots.telegram.media_cache.get_media_cache",
                    return_value=TelegramMediaCache(settings),
                ):
                    await deliver_rich_media(send, rich, bot=bot)
                content = send.await_args.args[0]
                self.assertEqual(content.media[0].media.media, "known")
                self.assertIn("<tg-button-row>", content.html)
                self.assertIn("<table>", content.html)

    def test_index_capacity_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = MediaIndex(SQLiteUserSettingsStore(Path(tmp) / "test.db"))
            with patch("bots.telegram.media_cache.time.time", return_value=10):
                index.save(1, "https://example.org/a", "one", 1, 0)
            with patch("bots.telegram.media_cache.time.time", return_value=20):
                self.assertIsNone(index.lookup(1, "https://example.org/a", 5))
                index.save(1, "https://example.org/b", "two", 1, 0)
                self.assertIsNone(index.lookup(1, "https://example.org/a", 0))
                self.assertEqual(index.lookup(1, "https://example.org/b", 0), "two")
