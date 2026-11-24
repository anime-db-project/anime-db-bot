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

    async def test_inline_edits_use_no_new_media_upload(self):
        bot = SimpleNamespace(id=123, edit_message_text=AsyncMock(return_value=True))
        message = InlineMessage(bot, "inline")
        await message.edit_text(
            rich_message=InputRichMessage(
                html='<img src="https://example.org/a.jpg"/><p>Body</p>'
            )
        )
        args = bot.edit_message_text.await_args.kwargs
        self.assertEqual(args["inline_message_id"], "inline")
        self.assertNotIn("<img", args["rich_message"].html)
        self.assertIn("https://example.org/a.jpg", args["rich_message"].html)
        from bots.telegram.inline_media import remember_photo

        remember_photo(bot.id, "https://example.org/a.jpg", "telegram-file-id")
        await message.edit_text(
            rich_message=InputRichMessage(
                html='<img src="https://example.org/a.jpg"/><p>Body</p>'
            )
        )
        media = bot.edit_message_text.await_args.kwargs["rich_message"].media
        self.assertEqual(media[0].media.media, "telegram-file-id")

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
