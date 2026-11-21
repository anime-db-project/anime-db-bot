from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bots.telegram.card.runtime.handlers import build_dispatcher
from bots.telegram.card.runtime.state import TelegramCardState
from core.models import AggregatedAnime
from sources.providers.wikipedia import (
    WikipediaArticle, WikipediaHit, WikipediaSection,
)


class _Session:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def __call__(self, bot: Bot, method: object, timeout: int | None = None):
        self.calls.append(method)
        return True

    async def close(self) -> None:
        pass


class WikipediaCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = _Session()
        self.bot = Bot("123456:TESTTOKEN", session=self.session)
        self.state = TelegramCardState.create(300)
        self.token = self.state.remember_anime(
            AggregatedAnime(
                title="Naruto",
                titles={"english": "Naruto", "russian": "Наруто"},
            ),
            42,
        )
        self.dispatcher = build_dispatcher("test", self.state)
        self.counter = 0
        self.hit = WikipediaHit(123, "Наруто", "Японское название ナルト", "ru")
        self.article = WikipediaArticle(
            123, "Наруто", "ru", "https://ru.wikipedia.org/wiki/Наруто",
            (WikipediaSection(
                "Сюжет",
                (('Ниндзя и <a href="https://ru.wikipedia.org/wiki/Коноха">Коноха</a>.',),),
            ),),
        )
        self.fake_client = unittest.mock.Mock()
        self.fake_client.search = AsyncMock(return_value=(self.hit,))
        self.fake_client.article = AsyncMock(return_value=self.article)
        self.client_patch = patch(
            "bots.telegram.card.features.wikipedia.wikipedia_client",
            return_value=self.fake_client,
        )
        self.locale_patch = patch(
            "bots.telegram.card.features.wikipedia.get_user_locale",
            return_value="ru",
        )
        self.mode_patch = patch(
            "bots.telegram.card.features.wikipedia.get_user_wikipedia_mode",
            return_value="clean",
        )
        self.client_patch.start()
        self.locale_patch.start()
        self.card_locale_patch = patch(
            "bots.telegram.card.runtime.handlers.get_user_locale",
            return_value="ru",
        )
        self.card_locale_patch.start()
        self.mode_patch.start()

    async def asyncTearDown(self) -> None:
        self.client_patch.stop()
        self.locale_patch.stop()
        self.card_locale_patch.stop()
        self.mode_patch.stop()
        await self.bot.session.close()

    async def click(self, data: str, user_id: int = 42) -> list[object]:
        self.counter += 1
        message = Message(
            message_id=1000, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=123456, is_bot=True, first_name="Bot"),
            text="Anime",
        )
        callback = CallbackQuery(
            id=f"callback{self.counter}",
            from_user=User(id=user_id, is_bot=False, first_name="User"),
            chat_instance="test",
            message=message,
            data=data,
        )
        prior = len(self.session.calls)
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=self.counter, callback_query=callback)
        )
        return self.session.calls[prior:]

    @staticmethod
    def text_of(messages: list[object]) -> str:
        return "\n".join(
            str(
                getattr(item, "text", None)
                or getattr(getattr(item, "rich_message", None), "html", "")
            )
            for item in messages
        )

    async def test_user_selects_article_and_reads_parent_section(self) -> None:
        first = await self.click(f"av:{self.token}:wikipedia:0")
        self.assertIn("Наруто", str(
            next(item for item in first
                 if type(item).__name__ == "EditMessageText").reply_markup
        ))
        self.fake_client.search.assert_awaited_once()
        self.fake_client.article.assert_not_awaited()
        # A search hit is not auto-promoted into a confirmed article.
        await self.click(f"wk:{self.token}:a:0")
        self.fake_client.article.assert_awaited_once()
        third = await self.click(f"wk:{self.token}:r:0:0")
        self.assertIn("<h3>Сюжет</h3>", self.text_of(third))
        self.assertNotIn("1/1", self.text_of(third))
        self.assertIn('href="https://ru.wikipedia.org/wiki/Коноха"', self.text_of(third))
        self.assertNotIn("Отображение не удалось", self.text_of(third))

    async def test_search_omits_results_without_japanese_text(self) -> None:
        self.fake_client.search.return_value = (
            WikipediaHit(501, "General article", "No Japanese writing", "ru"),
            WikipediaHit(123, "Наруто", "ナルト — 日本の漫画", "ru"),
        )
        response = await self.click(f"av:{self.token}:wikipedia:0")
        self.assertEqual(
            [hit.pageid for hit in self.state.anime_sessions[self.token].wikipedia_hits],
            [123],
        )
        self.assertNotIn("General article", str(response))

    async def test_callback_is_bound_to_owning_anime_card(self) -> None:
        response = await self.click(f"av:{self.token}:wikipedia:0", user_id=55)
        self.assertEqual(len(response), 1)
        self.assertEqual(type(response[0]).__name__, "AnswerCallbackQuery")
        self.fake_client.search.assert_not_awaited()
        unauthorized = await self.click(f"wk:{self.token}:search", user_id=55)
        self.assertEqual(len(unauthorized), 1)
        self.assertEqual(type(unauthorized[0]).__name__, "AnswerCallbackQuery")
        self.fake_client.search.assert_not_awaited()

    async def test_global_settings_language_switch_invalidates_article(self) -> None:
        await self.click(f"av:{self.token}:wikipedia:0")
        await self.click(f"wk:{self.token}:a:0")
        self.assertEqual(self.state.anime_sessions[self.token].wikipedia_pageid, 123)
        with (
            patch(
                "bots.telegram.card.features.wikipedia.get_user_locale",
                return_value="en",
            ),
            patch(
                "bots.telegram.card.runtime.handlers.get_user_locale",
                return_value="en",
            ),
        ):
            await self.click(f"av:{self.token}:wikipedia:0")
        self.assertIsNone(self.state.anime_sessions[self.token].wikipedia_pageid)
        self.assertEqual(self.state.anime_sessions[self.token].wikipedia_language, "en")
        self.assertEqual(self.fake_client.search.await_count, 2)
        self.assertEqual(
            self.fake_client.search.await_args.kwargs["language"], "en"
        )

    async def test_search_menu_contains_only_article_buttons_not_descriptions(self) -> None:
        first = await self.click(f"av:{self.token}:wikipedia:0")
        body = self.text_of(first)
        self.assertIn("Выберите статью:", body)
        self.assertNotIn("Японское название", body)
        self.assertNotIn("Русский", body)
        self.assertNotIn("Очищенный", body)
        self.assertNotIn("Подразделы", body)
        self.assertEqual(
            len([method for method in first if type(method).__name__ == "EditMessageText"]),
            1,
        )
        edited = next(
            method for method in first if type(method).__name__ == "EditMessageText"
        )
        labels = [
            button.text for row in edited.reply_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("1. Наруто", labels)
        self.assertFalse(any("Русский" in label for label in labels))


if __name__ == "__main__":
    unittest.main()
