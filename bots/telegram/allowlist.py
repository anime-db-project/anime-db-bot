from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject, Update

_ACCESS_DENIED = "Доступ к боту ограничен."


class TelegramAllowlistMiddleware(BaseMiddleware):
    """Reject unauthorized users before all database and application work.

    The rejection notices have both a per-user cooldown and a global sliding
    one-minute budget. The per-user map is bounded to prevent memory growth
    when many distinct accounts send unwanted updates.
    """

    def __init__(
        self,
        allowed_user_ids: tuple[int, ...],
        *,
        reply_cooldown_seconds: float = 60.0,
        global_replies_per_minute: int = 20,
        max_tracked_users: int = 4096,
        denial_message: str = _ACCESS_DENIED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allowed = frozenset(allowed_user_ids)
        self._denial_message = denial_message or _ACCESS_DENIED
        self._cooldown = max(0.0, reply_cooldown_seconds)
        self._global_limit = max(0, global_replies_per_minute)
        self._max_tracked = max(1, max_tracked_users)
        self._clock = clock
        self._last_notice: OrderedDict[int, float] = OrderedDict()
        self._global_notices: deque[float] = deque()

    def _may_notify(self, user_id: int) -> bool:
        now = self._clock()
        while self._global_notices and self._global_notices[0] <= now - 60.0:
            self._global_notices.popleft()
        previous = self._last_notice.get(user_id)
        if previous is not None and now - previous < self._cooldown:
            return False
        if len(self._global_notices) >= self._global_limit:
            return False

        # Account for an outgoing request before awaiting Telegram, so updates
        # arriving concurrently cannot pass both limits simultaneously.
        self._global_notices.append(now)
        self._last_notice[user_id] = now
        self._last_notice.move_to_end(user_id)
        while len(self._last_notice) > self._max_tracked:
            self._last_notice.popitem(last=False)
        return True

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

        if isinstance(inner, Message) and inner.chat.type != "private" and not (inner.text or inner.caption or "").startswith("/"):
            return None

        if isinstance(inner, InlineQuery):
            await inner.answer([], cache_time=0, is_personal=True)
            return None

        # Anonymous/service updates do not have a Telegram user to notify.
        # They must not be forwarded to app handlers while restricted.
        if user is None or not self._may_notify(int(user.id)):
            return None

        try:
            if isinstance(inner, CallbackQuery):
                await inner.answer(self._denial_message, show_alert=True)
            elif isinstance(inner, Message):
                await inner.answer(self._denial_message)
        except Exception:
            # A blocked/invalid chat must not turn a denied update into a
            # dispatcher exception or an unlimited retry loop.
            logging.debug("Could not send Telegram access-denied notice", exc_info=True)
        return None
