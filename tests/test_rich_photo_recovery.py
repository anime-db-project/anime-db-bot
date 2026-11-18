from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendRichMessage
from aiogram.types import InputRichMessage

from bots.telegram.card.views.renderer import (
    _POSTER_FAILURES,
    edit_anime_view,
    send_anime_view,
)
from core.models import AggregatedAnime


class RichPhotoRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _POSTER_FAILURES.clear()
        self.anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            external_ids={"anilist": "1"},
            amdb_id="amdb:123",
            posters=["https://example.invalid/cover.jpg"],
        )

    def tearDown(self) -> None:
        _POSTER_FAILURES.clear()

    @staticmethod
    def _missing_photo() -> TelegramBadRequest:
        return TelegramBadRequest(
            method=SendRichMessage(
                chat_id=1,
                rich_message=InputRichMessage(html="<p>Test</p>"),
            ),
            message="Bad Request: RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND",
        )

    async def test_send_photo_error_retries_rich_tables_not_standard_text(self) -> None:
        message = MagicMock()
        message.answer_rich = AsyncMock(side_effect=[self._missing_photo(), None])
        message.answer = AsyncMock()
        await send_anime_view(message, "token", self.anime, locale="ru")
        self.assertEqual(message.answer_rich.await_count, 2)
        first, second = message.answer_rich.await_args_list
        self.assertIn('src="tg://photo?id=adb_photo_0"', first.args[0].html)
        self.assertEqual(
            first.args[0].media[0].media.media,
            "https://example.invalid/cover.jpg",
        )
        self.assertNotIn("<img", second.args[0].html)
        self.assertIn("<table", second.args[0].html)
        self.assertIn("<tg-button-row", second.args[0].html)
        self.assertIn('data="av:token:all_ids:0"', second.args[0].html)
        message.answer.assert_not_awaited()

        # The same broken poster combination should not trigger a second
        # failing request, even on another callback message instance.
        next_message = MagicMock()
        next_message.answer_rich = AsyncMock(return_value=None)
        await send_anime_view(next_message, "token", self.anime, locale="ru")
        next_message.answer_rich.assert_awaited_once()
        self.assertNotIn("<img", next_message.answer_rich.await_args.args[0].html)

    async def test_edit_photo_error_keeps_rich_layout(self) -> None:
        message = MagicMock()
        message.edit_text = AsyncMock(side_effect=[self._missing_photo(), None])
        await edit_anime_view(message, "token", self.anime, "main", 0, locale="ru")
        self.assertEqual(message.edit_text.await_count, 2)
        second = message.edit_text.await_args_list[1].kwargs["rich_message"]
        self.assertIn("<table", second.html)
        self.assertIn("<tg-button-row", second.html)
        self.assertNotIn("<img", second.html)

    async def test_unrelated_bad_request_keeps_existing_fallback(self) -> None:
        message = MagicMock()
        error = TelegramBadRequest(
            method=SendRichMessage(
                chat_id=1, rich_message=InputRichMessage(html="<p>Test</p>")
            ),
            message="Bad Request: RICH_MESSAGE_TABLE_COLS_TOO_MANY",
        )
        message.answer_rich = AsyncMock(side_effect=error)
        message.answer = AsyncMock(return_value=None)
        await send_anime_view(message, "token", self.anime, locale="ru")
        message.answer.assert_awaited()
