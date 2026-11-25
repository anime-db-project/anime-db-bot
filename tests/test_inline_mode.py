from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    InputRichMessage,
    Message,
    Update,
    User,
)

from bots.session_store import ExpiringSessionStore
from bots.telegram.allowlist import TelegramAllowlistMiddleware
from bots.telegram.card.runtime.state import TelegramCardState
from bots.telegram.command_menu import commands_for
from bots.telegram.inline_mode import (
    InlineCardMiddleware,
    InlineMessage,
    deep_link_payload,
    parse_start_payload,
    register_inline_handlers,
)
from bots.telegram.project_info import show_about
from core.models import AggregatedAnime, SearchCandidate
from core.mapping_links import mapping_id_url, mapping_link_templates

SETTINGS = SimpleNamespace(
    telegram=SimpleNamespace(
        inline_enabled=True, allowed_group_ids=(), denied_message="Denied"
    )
)


class InlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_gate_respects_group_and_inline_configuration(self):
        user = User(id=10, is_bot=False, first_name="Test")
        query = InlineQuery(id="q", from_user=user, query="Naruto", offset="")
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            from_user=user,
            chat=Chat(id=-100123, type="supergroup"),
            text="/anime",
        )
        handler = AsyncMock()
        with (
            patch.object(InlineQuery, "answer", new_callable=AsyncMock) as answer,
            patch.object(Message, "answer", new_callable=AsyncMock),
        ):
            for gate in (
                TelegramAllowlistMiddleware((), inline_enabled=False),
                TelegramAllowlistMiddleware((), allowed_group_ids=(-100123,)),
            ):
                await gate(handler, Update(update_id=1, inline_query=query), {})
            answer.assert_awaited()
            gate = TelegramAllowlistMiddleware((), allowed_group_ids=(-100999,))
            await gate(handler, Update(update_id=2, message=message), {})
            handler.assert_not_awaited()
            await TelegramAllowlistMiddleware((), allowed_group_ids=(-100123,))(
                handler, Update(update_id=3, message=message), {}
            )
            handler.assert_awaited_once()

    async def test_inline_callback_ownership_and_message_binding(self):
        state = TelegramCardState.create(60)
        token = state.remember_anime(AggregatedAnime(title="Naruto"), 10)
        state.anime_sessions[token].inline_message_id = "inline-one"
        choices = ExpiringSessionStore(max_entries=10, ttl_seconds=60)
        middleware = InlineCardMiddleware(choices, state)
        handler = AsyncMock()
        bot = Bot("123456:TEST_TOKEN")
        try:
            for uid, mid in (
                (11, "inline-one"),
                (10, "inline-two"),
                (10, "inline-one"),
            ):
                callback = CallbackQuery(
                    id="c",
                    from_user=User(id=uid, is_bot=False, first_name="Test"),
                    chat_instance="chat",
                    inline_message_id=mid,
                    data=f"av:{token}:main:0",
                ).as_(bot)
                with (
                    patch(
                        "bots.telegram.inline_mode.get_settings", return_value=SETTINGS
                    ),
                    patch(
                        "bots.telegram.inline_mode.get_user_locale", return_value="en"
                    ),
                    patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
                ):
                    await middleware(handler, callback, {})
            handler.assert_awaited_once()
            adapted = handler.await_args.args[0]
            self.assertIsInstance(adapted.message, InlineMessage)
            self.assertEqual(adapted.message.inline_message_id, "inline-one")
        finally:
            await bot.session.close()

    async def test_inline_edits_keep_buttons_and_use_cached_or_local_files(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        from bots.telegram.inline_media import remember_photo

        bot = SimpleNamespace(id=123, edit_message_text=AsyncMock())
        error = TelegramBadRequest(
            method=EditMessageText(inline_message_id="inline", text="test"),
            message="RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND",
        )
        bot.edit_message_text.side_effect = [error, True]
        message = InlineMessage(bot, "inline")
        body = '<img src="https://example.org/a.jpg"/><p>Body</p><tg-button-row><tg-button type="callback_data" data="ci:token:staff">Staff</tg-button></tg-button-row>'
        await message.edit_text(rich_message=InputRichMessage(html=body))
        args = bot.edit_message_text.await_args.kwargs
        self.assertEqual(args["inline_message_id"], "inline")
        self.assertNotIn("<img", args["rich_message"].html)
        self.assertIn("https://example.org/a.jpg", args["rich_message"].html)
        self.assertIn("<tg-button-row>", args["rich_message"].html)
        remember_photo(bot.id, "https://example.org/a.jpg", "telegram-file-id")
        bot.edit_message_text.side_effect = None
        bot.edit_message_text.return_value = True
        await message.edit_text(rich_message=InputRichMessage(html=body))
        rich = bot.edit_message_text.await_args.kwargs["rich_message"]
        self.assertEqual(rich.media[0].media.media, "telegram-file-id")
        self.assertIn("<tg-button-row>", rich.html)

    async def test_staging_uses_shared_ram_disk_and_ephemeral_callback(self):
        import asyncio
        import time
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendPhoto
        from aiogram.types import BufferedInputFile, FSInputFile
        from bots.telegram.inline_media import (
            InlineUploadContext,
            SendTemporaryPhoto,
            photo_id,
        )

        class FakeBot:
            id = 991

            def __init__(self):
                self.calls = []
                self.delete_ephemeral_message = AsyncMock()
                self.send_photo = AsyncMock()

            async def __call__(self, method, **kwargs):
                self.calls.append(method)
                if len(self.calls) < 3:
                    raise TelegramBadRequest(method=method, message="photo rejected")
                return SimpleNamespace(
                    photo=[SimpleNamespace(width=100, height=100, file_id="cached")],
                    ephemeral_message_id=19,
                    chat=SimpleNamespace(id=-10042),
                )

        bot = FakeBot()
        manager = SimpleNamespace(
            settings=SimpleNamespace(
                order=("direct", "url", "ram", "disk"),
                ram_enabled=True,
                disk_enabled=True,
            ),
            prepare_photo_file=AsyncMock(
                side_effect=[
                    BufferedInputFile(b"image", filename="ram.jpg"),
                    FSInputFile("/tmp/test.jpg"),
                ]
            ),
        )
        settings = SimpleNamespace(
            telegram=SimpleNamespace(
                inline_media_cache_chat_id=0, inline_media_ephemeral_enabled=True
            )
        )
        from dataclasses import replace
        from core.settings import get_settings
        from bots.telegram.media_cache import TelegramMediaCache

        isolated_cache = TelegramMediaCache(
            replace(
                get_settings().telegram,
                media_cache_persistent=False,
                media_cache_chat_id=0,
            )
        )
        context = InlineUploadContext("callback", 10, time.monotonic())
        url = "https://example.org/disk-fallback.jpg"
        deadline = asyncio.get_running_loop().time() + 5
        with (
            patch(
                "bots.telegram.inline_media.get_media_cache",
                return_value=isolated_cache,
            ),
            patch("bots.telegram.inline_media.get_settings", return_value=settings),
            patch(
                "bots.telegram.inline_media.get_rich_media_delivery",
                return_value=manager,
            ),
        ):
            result = await photo_id(bot, url, deadline, upload=True, context=context)
            self.assertEqual(result, "cached")
            self.assertEqual(
                await photo_id(bot, url, deadline, upload=True, context=context),
                "cached",
            )
        self.assertEqual(len(bot.calls), 3)
        self.assertTrue(all(isinstance(call, SendTemporaryPhoto) for call in bot.calls))
        self.assertEqual(bot.calls[0].photo, url)
        self.assertIsInstance(bot.calls[1].photo, BufferedInputFile)
        self.assertIsInstance(bot.calls[2].photo, FSInputFile)
        self.assertEqual(
            [call.args[1] for call in manager.prepare_photo_file.await_args_list],
            ["ram", "disk"],
        )
        bot.send_photo.assert_not_awaited()
        bot.delete_ephemeral_message.assert_awaited_once_with(
            -10042, 10, 19, request_timeout=3
        )

    async def test_main_rich_inline_card_does_not_force_fallback_keyboard(self):
        from bots.telegram.card.views.renderer import send_anime_view
        from bots.telegram.rich_media import inline_delivery_context

        bot = SimpleNamespace(id=999, edit_message_text=AsyncMock(return_value=True))
        message = InlineMessage(bot, "inline")
        anime = AggregatedAnime(title="Naruto", external_ids={"mal": "20"})
        with inline_delivery_context():
            sent, fallback = await send_anime_view(message, "token", anime, locale="ru")
        self.assertFalse(fallback)
        self.assertIs(sent, message)
        args = bot.edit_message_text.await_args.kwargs
        self.assertIn('data="ci:token:staff"', args["rich_message"].html)
        buttons = [
            b.callback_data for row in args["reply_markup"].inline_keyboard for b in row
        ]
        self.assertNotIn("ci:token:staff", buttons)

    async def test_about_uses_same_rich_content_for_send_and_edit(self):
        message = SimpleNamespace(
            answer_rich=AsyncMock(), answer=AsyncMock(), edit_text=AsyncMock()
        )
        with patch(
            "bots.telegram.project_info.project_repositories",
            new=AsyncMock(
                return_value=[
                    {"name": "anime-db-bot", "stars": 1, "license": "Apache-2.0"}
                ]
            ),
        ):
            await show_about(message, "ru")
            await show_about(message, "ru", edit=True)
        sent = message.answer_rich.await_args
        edited = message.edit_text.await_args
        self.assertEqual(sent.args[0], edited.kwargs["rich_message"])
        self.assertEqual(sent.kwargs["reply_markup"], edited.kwargs["reply_markup"])
        message.answer.assert_not_awaited()

    def test_payload_commands_and_mapping_url_validation(self):
        candidate = SearchCandidate(title="Naruto", external_ids={"mal": "20"})
        self.assertEqual(
            parse_start_payload(deep_link_payload(candidate)), ("mal", "20")
        )
        self.assertIsNone(parse_start_payload("anime_mal_20_invalid"))
        self.assertNotIn("ban", [c.command for c in commands_for("ru")])
        self.assertIn(
            "ban", [c.command for c in commands_for("ru", administrator=True)]
        )
        templates = mapping_link_templates(
            {
                "TELEGRAM_ID_URL_MAL": "https://example.org/{id}",
                "TELEGRAM_ID_URL_ANIDB": "",
            }
        )
        self.assertEqual(
            mapping_id_url("MAL", "test_id", templates), "https://example.org/test_id"
        )
        self.assertIsNone(mapping_id_url("ANIDB", "1", templates))
        self.assertIsNone(mapping_id_url("AMDB", "1", templates))
        with self.assertRaises(ValueError):
            mapping_link_templates({"TELEGRAM_ID_URL_MAL": "https://{id}.example.org/"})
