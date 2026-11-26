from __future__ import annotations
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.types import CallbackQuery
from bots.telegram.bot import _CallbackRoutingMiddleware
from bots.telegram.card.runtime.state import TelegramCardState


class CardConcurrentEditsTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_page_coalesces_and_different_page_serializes(self):
        state = TelegramCardState.create(120)
        middleware = _CallbackRoutingMiddleware(state)
        active = 0
        maximum = 0
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(event, data):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append(event.data)
            started.set()
            await release.wait()
            active -= 1

        def event(raw):
            return CallbackQuery.model_construct(
                data=raw,
                message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=2),
                inline_message_id=None,
            )

        first = event("av:token:characters:1")
        duplicate = event(first.data)
        other = event("av:token:characters:2")
        with (
            patch(
                "bots.telegram.bot.handle_people_callback",
                new=AsyncMock(return_value=False),
            ),
            patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer,
        ):
            a = asyncio.create_task(middleware(handler, first, {}))
            await started.wait()
            b = asyncio.create_task(middleware(handler, duplicate, {}))
            c = asyncio.create_task(middleware(handler, other, {}))
            await asyncio.sleep(0)
            self.assertEqual(calls, [first.data])
            release.set()
            await asyncio.gather(a, b, c)
            answer.assert_awaited_once()
        self.assertEqual(calls, [first.data, other.data])
        self.assertEqual(maximum, 1)
        self.assertFalse(middleware._active)
