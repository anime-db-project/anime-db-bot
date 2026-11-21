from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import unittest

from aiogram import Bot, Dispatcher
from aiogram.types import (
    CallbackQuery, Chat, Message, Update, User,
)

from bots.telegram.admin_config import (
    SearchResults,
    _find_key, _group_page, _groups_page, _key_page, _search_page,
    catalog, description_for, search_keys,
)
from bots.telegram.settings import _settings_keyboard, register_settings_handlers
from scripts.config_report import example_groups
from scripts.console_config import validate_key_value
from core.settings import ConfigFileError

SECRET = "TEST_secret_access_token_dont_echo_123456"


class AdminConfigCatalogTests(unittest.TestCase):
    def test_every_documented_env_key_appears_in_admin_catalog(self) -> None:
        expected = {
            key for group, keys in example_groups().items()
            if group.endswith(".env") and group != ".env.example"
            for key in keys
        }
        actual = {key for _, keys in catalog() for key in keys}
        self.assertEqual(actual, expected)
        self.assertGreater(len(actual), 180)
        self.assertIsNotNone(_find_key("BOT_TOKEN"))
        self.assertIsNotNone(_find_key("MAPPINGS_AUTO_UPDATE"))
        self.assertIsNotNone(_find_key("API_ENABLED"))

    def test_env_metadata_is_cached_but_values_are_not(self) -> None:
        from scripts.console_config import documented_config_groups

        self.assertIs(documented_config_groups(), documented_config_groups())
        self.assertEqual(catalog(), documented_config_groups())

    def test_admin_paginated_rows_use_settings_override(self) -> None:
        from types import SimpleNamespace

        config = SimpleNamespace(
            telegram=SimpleNamespace(
                pagination_page_size=10,
                pagination_block_page_sizes={"settings": 3},
            )
        )
        with patch("bots.telegram.pagination.get_settings", return_value=config):
            _header, keyboard = _group_page("ru", 0)
        choices = [
            button for row in keyboard.inline_keyboard for button in row
            if button.callback_data.startswith("cfg:k:")
        ]
        self.assertEqual(len(choices), 3)

    def test_admin_pages_have_one_navigation_counter_and_localized_headings(self) -> None:
        heading, keys_markup = _group_page("ru", 0)
        self.assertIn("параметров", heading)
        self.assertNotIn(" options", heading)
        self.assertNotIn("1/", heading)
        self.assertTrue(
            any(
                "/" in button.text
                for row in keys_markup.inline_keyboard
                for button in row
            )
        )
        search_heading, search_markup = _search_page(
            "ru", "t", SearchResults(owner_id=1, keys=tuple(
                f"TEST_KEY_{n}" for n in range(15)
            )), 0
        )
        self.assertIn("Найдено параметров: 15.", search_heading)
        self.assertNotIn("Страница", search_heading)
        self.assertTrue(any(
            button.text == "1/2"
            for row in search_markup.inline_keyboard
            for button in row
        ))

    def test_group_pages_are_bounded_and_search_works(self) -> None:
        for page in range(10):
            txt, keys = _groups_page("ru", page)
            self.assertLess(len(txt), 1500)
            self.assertLessEqual(len(keys.inline_keyboard), 13)
        self.assertIn("MAPPINGS_AUTO_UPDATE", search_keys("mappings"))
        self.assertEqual(search_keys(""), ())
        self.assertLessEqual(len(description_for("BOT_TOKEN", "telegram.env")), 300)

    def test_only_admin_in_private_chat_gets_button(self) -> None:
        with patch(
            "bots.telegram.settings.is_telegram_admin",
            side_effect=lambda value: value == 42,
        ):
            authorized = _settings_keyboard("ru", user_id=42, private=True)
            ordinary = _settings_keyboard("ru", user_id=43, private=True)
            group = _settings_keyboard("ru", user_id=42, private=False)
        self.assertIn("cfg:h:0", str(authorized.model_dump()))
        self.assertNotIn("cfg:h:0", str(ordinary.model_dump()))
        self.assertNotIn("cfg:h:0", str(group.model_dump()))

    def test_strict_validation_rejects_invalid_input_without_writing(self) -> None:
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(ValueError):
                validate_key_value("MAPPINGS_AUTO_UPDATE", "invalid", root=folder)
            with self.assertRaises(ValueError):
                validate_key_value("API_PORT", "not-an-integer", root=folder)
            with self.assertRaises(ConfigFileError):
                validate_key_value("MADE_UP_UNDOCUMENTED_KEY", "xxx", root=folder)
            self.assertEqual(list(folder.iterdir()), [])


class _FakeTelegramSession:
    def __init__(self) -> None:
        self.methods: list[object] = []

    async def __call__(self, bot: Bot, method: object, timeout: int | None = None):
        del bot, timeout
        self.methods.append(method)
        return True

    async def close(self) -> None:
        return None


class AdminConfigIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = Bot("123456:TESTTOKEN", session=_FakeTelegramSession())
        self.session = self.bot.session
        self.dp = Dispatcher()
        self.admin_patch = patch(
            "bots.telegram.admin_config.is_telegram_admin",
            side_effect=lambda user_id: user_id == 42,
        )
        self.admin_patch.start()
        self.settings_admin_patch = patch(
            "bots.telegram.settings.is_telegram_admin",
            side_effect=lambda user_id: user_id == 42,
        )
        self.settings_admin_patch.start()
        self.locale_patch = patch(
            "bots.telegram.admin_config.get_user_locale", return_value="ru"
        )
        self.locale_patch.start()
        self.settings_locale_patch = patch(
            "bots.telegram.settings.get_user_locale", return_value="ru"
        )
        self.settings_locale_patch.start()
        register_settings_handlers(self.dp)
        self.counter = 0

    async def asyncTearDown(self) -> None:
        self.admin_patch.stop()
        self.settings_admin_patch.stop()
        self.locale_patch.stop()
        self.settings_locale_patch.stop()
        await self.bot.session.close()

    def user(self, user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name="Test")

    def chat(self, user_id: int, group: bool = False) -> Chat:
        return Chat(id=-200 if group else user_id, type="supergroup" if group else "private")

    async def message(
        self, content: str, user_id: int = 42, group: bool = False
    ) -> list[object]:
        self.counter += 1
        msg = Message(
            message_id=self.counter,
            date=datetime.now(timezone.utc),
            chat=self.chat(user_id, group),
            from_user=self.user(user_id),
            text=content,
        )
        start = len(self.session.methods)
        await self.dp.feed_update(
            self.bot, Update(update_id=self.counter, message=msg)
        )
        return self.session.methods[start:]

    async def callback(
        self, data: str, user_id: int = 42, group: bool = False
    ) -> list[object]:
        self.counter += 1
        cb = CallbackQuery(
            id=f"c{self.counter}",
            from_user=self.user(user_id),
            chat_instance="test",
            data=data,
            message=Message(
                message_id=max(1, self.counter - 1),
                date=datetime.now(timezone.utc),
                chat=self.chat(user_id, group),
                from_user=User(id=123456, is_bot=True, first_name="Bot"),
                text="Admin configuration",
            ),
        )
        start = len(self.session.methods)
        await self.dp.feed_update(
            self.bot, Update(update_id=self.counter, callback_query=cb)
        )
        return self.session.methods[start:]

    @staticmethod
    def body(messages: list[object]) -> str:
        return "\n".join(str(getattr(m, "text", "")) for m in messages)

    async def test_command_and_callback_denied_to_nonadmin(self) -> None:
        response = await self.message("/admin_config", user_id=99)
        self.assertIn("Administrator", self.body(response))
        cb = await self.callback("cfg:h:0", user_id=99)
        self.assertNotIn("Bot configuration:", self.body(cb))

    async def test_admin_settings_are_private_only(self) -> None:
        response = await self.message("/admin_config", user_id=42, group=True)
        self.assertIn("private chat", self.body(response))
        cb = await self.callback("cfg:h:0", user_id=42, group=True)
        self.assertTrue(any(getattr(m, "show_alert", False) for m in cb))

    async def test_secret_can_be_changed_but_never_echoed(self) -> None:
        loc = _find_key("BOT_TOKEN")
        self.assertIsNotNone(loc)
        gi, ki = loc
        with patch(
            "bots.telegram.admin_config.inspect_key",
            return_value={
                "key": "BOT_TOKEN",
                "source": "test.env",
                "configured": True,
                "value": "<redacted>",
                "masked": True,
            },
        ), patch(
            "bots.telegram.admin_config.validate_key_value",
        ) as validate, patch(
            "bots.telegram.admin_config.update_key",
            return_value={"changed": True},
        ) as update:
            await self.message("/admin_config")
            preview = await self.callback(f"cfg:k:{gi}:{ki}")
            self.assertIn("<hidden>", self.body(preview))
            await self.callback(f"cfg:e:{gi}:{ki}")
            sent = await self.message(SECRET)
            self.assertIn("DeleteMessage", [type(m).__name__ for m in sent])
            self.assertNotIn(SECRET, self.body(sent))
            confirm_msg = next(
                m for m in sent if type(m).__name__ == "SendMessage"
            )
            confirm_data = confirm_msg.reply_markup.inline_keyboard[0][0].callback_data
            self.assertTrue(confirm_data.startswith("cfg:yes:"))
            confirmation = await self.callback(confirm_data)
            self.assertIn("сохранено", self.body(confirmation))
            update.assert_called_once_with("BOT_TOKEN", SECRET)
            validate.assert_called_once_with("BOT_TOKEN", SECRET)
            self.assertNotIn(SECRET, self.body(confirmation))

    async def test_other_admin_cannot_approve_pending_edit(self) -> None:
        loc = _find_key("MAPPINGS_AUTO_UPDATE")
        gi, ki = loc
        with patch(
            "bots.telegram.admin_config.inspect_key",
            return_value={
                "key": "MAPPINGS_AUTO_UPDATE",
                "source": "test.env",
                "configured": True,
                "value": "1",
                "masked": False,
            },
        ), patch("bots.telegram.admin_config.validate_key_value"), patch(
            "bots.telegram.admin_config.update_key"
        ) as update:
            await self.callback(f"cfg:e:{gi}:{ki}", user_id=42)
            response = await self.message("0", user_id=42)
            confirm_msg = next(
                m for m in response if type(m).__name__ == "SendMessage"
            )
            confirm = confirm_msg.reply_markup.inline_keyboard[0][0].callback_data
            result = await self.callback(confirm, user_id=99)
            self.assertTrue(any(getattr(m, "show_alert", False) for m in result))
            update.assert_not_called()

    async def test_search_by_name_and_paginated_results(self) -> None:
        await self.message("/admin_config")
        await self.callback("cfg:search")
        response = await self.message("MAPPINGS")
        self.assertIn("MAPPINGS", str(
            next(m for m in response if type(m).__name__ == "SendMessage").reply_markup
        ))
        self.assertNotIn("secret", self.body(response).lower())

    async def test_no_plaintext_secret_in_key_details(self) -> None:
        loc = _find_key("API_ADMIN_KEY")
        with patch(
            "bots.telegram.admin_config.inspect_key",
            return_value={
                "key": "API_ADMIN_KEY",
                "source": "test.env",
                "configured": True,
                "value": SECRET,
                "masked": True,
            },
        ):
            body, _ = await _key_page("ru", *loc)
        self.assertNotIn(SECRET, body)


if __name__ == "__main__":
    unittest.main()
