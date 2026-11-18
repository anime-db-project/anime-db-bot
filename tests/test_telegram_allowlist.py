from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bots.telegram.allowlist import TelegramAllowlistMiddleware


def _message(user_id: int | None, *, text: str = "/start") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=42, type="private"),
        from_user=(
            User(id=user_id, is_bot=False, first_name="Person")
            if user_id is not None
            else None
        ),
        text=text,
    )


def _callback(user_id: int) -> CallbackQuery:
    return CallbackQuery(
        id="callback-test",
        from_user=User(id=user_id, is_bot=False, first_name="Person"),
        chat_instance="test",
        data="av:token:main:0",
        message=_message(user_id),
    )


class TelegramAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_list_allows_any_message(self) -> None:
        gate = TelegramAllowlistMiddleware(())
        handler = AsyncMock(return_value="handled")
        update = Update(update_id=1, message=_message(1234))
        self.assertEqual(await gate(handler, update, {}), "handled")
        handler.assert_awaited_once_with(update, {})

    async def test_configured_id_allows_message(self) -> None:
        gate = TelegramAllowlistMiddleware((101, 202))
        handler = AsyncMock(return_value="ok")
        update = Update(update_id=1, message=_message(202))
        self.assertEqual(await gate(handler, update, {}), "ok")
        handler.assert_awaited_once()

    async def test_blocked_user_does_not_reach_handlers(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        update = Update(update_id=2, message=_message(202))
        self.assertIsNone(await gate(handler, update, {}))
        handler.assert_not_awaited()

    async def test_anonymous_message_is_blocked_when_restricted(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        update = Update(update_id=3, message=_message(None))
        self.assertIsNone(await gate(handler, update, {}))
        handler.assert_not_awaited()

    async def test_callback_from_blocked_user_is_acknowledged(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        query = _callback(202)
        update = Update(update_id=4, callback_query=query)
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
            self.assertIsNone(await gate(handler, update, {}))
        answer.assert_awaited_once_with(
            "Доступ к боту ограничен.", show_alert=True
        )
        handler.assert_not_awaited()

    async def test_callback_from_allowed_user_is_processed(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock(return_value="callback")
        update = Update(update_id=5, callback_query=_callback(101))
        self.assertEqual(await gate(handler, update, {}), "callback")
        handler.assert_awaited_once()

    async def test_update_without_user_is_rejected_if_restricted(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        update = Update(update_id=6, my_chat_member={
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 202, "is_bot": False, "first_name": "Person"},
            "date": int(datetime.now(timezone.utc).timestamp()),
            "old_chat_member": {"status": "member", "user": {"id": 101, "is_bot": True, "first_name": "Bot"}},
            "new_chat_member": {"status": "member", "user": {"id": 101, "is_bot": True, "first_name": "Bot"}},
        })
        self.assertIsNone(await gate(handler, update, {}))
        handler.assert_not_awaited()
