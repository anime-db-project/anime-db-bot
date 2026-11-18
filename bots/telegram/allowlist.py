from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update


class TelegramAllowlistMiddleware(BaseMiddleware):
    """Allow all updates when unset, otherwise only specified Telegram users.

    Register on the *update* observer so the gate runs before all message and
    callback handlers, including admin and plugin/extension routes. Anonymous
    senders and updates without a user cannot bypass an active allowlist.
    """

    def __init__(self, allowed_user_ids: tuple[int, ...]) -> None:
        self._allowed = frozenset(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._allowed:
            return await handler(event, data)

        inner = event.event if isinstance(event, Update) else event
        user = getattr(inner, "from_user", None)
        if user is not None and int(user.id) in self._allowed:
            return await handler(event, data)

        # Acknowledge blocked callbacks so Telegram does not keep showing a
        # loading spinner, without triggering database or external API calls.
        if isinstance(inner, CallbackQuery):
            await inner.answer("Доступ к боту ограничен.", show_alert=True)
        return None
