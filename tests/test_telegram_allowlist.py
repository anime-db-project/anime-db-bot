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

    async def test_blocked_user_gets_message_without_reaching_handlers(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        update = Update(update_id=2, message=_message(202))
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            self.assertIsNone(await gate(handler, update, {}))
        answer.assert_awaited_once_with("Доступ к боту ограничен.")
        handler.assert_not_awaited()

    async def test_repeated_denied_messages_are_rate_limited(self) -> None:
        now = [100.0]
        gate = TelegramAllowlistMiddleware(
            (101,), reply_cooldown_seconds=60.0, clock=lambda: now[0]
        )
        handler = AsyncMock()
        update = Update(update_id=2, message=_message(202, text="/anime"))
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            for _ in range(40):
                self.assertIsNone(await gate(handler, update, {}))
            answer.assert_awaited_once()
            now[0] = 159.9
            await gate(handler, update, {})
            answer.assert_awaited_once()
            now[0] = 160.0
            await gate(handler, update, {})
            self.assertEqual(answer.await_count, 2)
        handler.assert_not_awaited()

    async def test_global_reply_limit_applies_to_different_users(self) -> None:
        now = [10.0]
        gate = TelegramAllowlistMiddleware(
            (101,),
            reply_cooldown_seconds=0.0,
            global_replies_per_minute=2,
            clock=lambda: now[0],
        )
        handler = AsyncMock()
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            for user_id in range(200, 260):
                await gate(
                    handler, Update(update_id=user_id, message=_message(user_id)), {}
                )
            self.assertEqual(answer.await_count, 2)
            now[0] = 70.0
            await gate(handler, Update(update_id=501, message=_message(500)), {})
            self.assertEqual(answer.await_count, 3)
        handler.assert_not_awaited()

    async def test_denied_user_cache_is_bounded(self) -> None:
        gate = TelegramAllowlistMiddleware(
            (101,), max_tracked_users=3, global_replies_per_minute=100
        )
        handler = AsyncMock()
        with patch.object(Message, "answer", new_callable=AsyncMock):
            for user_id in range(200, 220):
                await gate(
                    handler, Update(update_id=user_id, message=_message(user_id)), {}
                )
        self.assertEqual(len(gate._last_notice), 3)
        self.assertLessEqual(len(gate._global_notices), 100)
        handler.assert_not_awaited()

    async def test_blocked_message_delivery_errors_are_not_retried(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        update = Update(update_id=2, message=_message(202))
        with patch.object(
            Message, "answer", new_callable=AsyncMock, side_effect=RuntimeError("API")
        ) as answer:
            self.assertIsNone(await gate(handler, update, {}))
            self.assertIsNone(await gate(handler, update, {}))
        answer.assert_awaited_once()
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

    async def test_callback_uses_same_per_user_cooldown_as_messages(self) -> None:
        gate = TelegramAllowlistMiddleware((101,))
        handler = AsyncMock()
        with (
            patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as callback_answer,
            patch.object(Message, "answer", new_callable=AsyncMock) as message_answer,
        ):
            await gate(handler, Update(update_id=2, message=_message(202)), {})
            await gate(handler, Update(update_id=3, callback_query=_callback(202)), {})
        message_answer.assert_awaited_once()
        callback_answer.assert_not_awaited()
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
