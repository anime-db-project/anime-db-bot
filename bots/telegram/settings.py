from __future__ import annotations

import html
from aiogram.types import InputRichMessage
from bots.telegram.project_info import show_about

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bots.telegram.callbacks import SettingsCallback
from bots.telegram.admin_config import register_admin_config_handlers
from services.users.access_control import is_telegram_admin
from localization import available_locales, locale_name, text
from services.users.settings import (
    get_user_locale,
    set_user_locale,
    get_user_wikipedia_mode,
    set_user_wikipedia_mode,
)

SETTINGS_PREFIX = "settings:"


def _settings_keyboard(
    locale: str, *, user_id: int | None = None, private: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=text("settings", "language_button", locale=locale),
                callback_data=SettingsCallback("language").encode(),
            )
        ]
    ]
    rows.append([
        InlineKeyboardButton(
            text="Wikipedia · формат" if locale == "ru" else "Wikipedia · display mode",
            callback_data="settings:wiki",
        )
    ])
    if private and is_telegram_admin(user_id):
        rows.append([
            InlineKeyboardButton(
                text="Настройки бота (ENV)" if locale == "ru" else "Bot configuration (ENV)",
                callback_data="cfg:h:0",
            )
        ])
    rows.append([InlineKeyboardButton(text=text('project', 'title', locale=locale), callback_data='settings:about')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _language_keyboard(locale: str, *, owner: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for value, flag in (
        ("en", "🇬🇧"),
        ("ru", "🇷🇺"),
        ("ja", "🇯🇵"),
        ("de", "🇩🇪"),
        ("es", "🇪🇸"),
        ("fr", "🇫🇷"),
        ("it", "🇮🇹"),
    ):
        if value not in available_locales():
            continue
        label = f"{flag} {locale_name(value, display_locale=value)}"
        if value == locale:
            label = "✓ " + label
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"welcome:{owner}:{value}" if owner is not None else SettingsCallback("locale", value).encode(),
                )
            ]
        )
    if owner is not None:
        return InlineKeyboardMarkup(inline_keyboard=rows)
    rows.append(
        [
            InlineKeyboardButton(
                text=text("settings", "back", locale=locale),
                callback_data=SettingsCallback("back").encode(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_text(locale: str) -> str:
    language = locale_name(locale, display_locale=locale)
    return (
        f"{text('settings', 'title', locale=locale)}\n\n"
        f"{text('settings', 'current_language', locale=locale, language=language)}"
    )



def _wikipedia_mode_keyboard(locale: str, mode: str) -> InlineKeyboardMarkup:
    choices = (
        ("clean", "Очищенный" if locale == "ru" else "Clean"),
        ("raw", "Полный RAW" if locale == "ru" else "Full RAW"),
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✓ " if mode == value else "") + title,
            callback_data=f"settings:wiki:{value}",
        )]
        for value, title in choices
    ] + [[InlineKeyboardButton(
        text=text("settings", "back", locale=locale),
        callback_data="settings:back",
    )]])


def _wikipedia_mode_text(locale: str, mode: str) -> str:
    return "\n\n".join(text("project", key, locale=locale) for key in ("wiki_title", "wiki_clean", "wiki_raw", "wiki_note"))


def _wiki_rich(locale: str) -> InputRichMessage:
    title = html.escape(text("project", "wiki_title", locale=locale))
    clean = html.escape(text("project", "wiki_clean", locale=locale))
    raw = html.escape(text("project", "wiki_raw", locale=locale))
    note = html.escape(text("project", "wiki_note", locale=locale))
    return InputRichMessage(html=f"<h2>{title}</h2><p><br></p><table bordered striped compact><tr><td>{clean}</td></tr><tr><td>{raw}</td></tr></table><p><br></p><p><i>{note}</i></p>")


async def _edit_wiki_mode(message, locale: str, mode: str):
    keyboard = _wikipedia_mode_keyboard(locale, mode)
    try:
        await message.edit_text(rich_message=_wiki_rich(locale), reply_markup=keyboard)
    except Exception:
        await message.edit_text(_wikipedia_mode_text(locale, mode), reply_markup=keyboard)


def register_settings_handlers(dp: Dispatcher) -> None:
    @dp.callback_query(F.data == "settings:about")
    async def settings_about(callback: CallbackQuery):
        await callback.answer()
        if callback.message:
            await show_about(callback.message, get_user_locale(callback.from_user.id), edit=True)

    @dp.message(Command("settings"))
    async def settings_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        locale = get_user_locale(user_id)
        await message.answer(
            _settings_text(locale),
            reply_markup=_settings_keyboard(locale, user_id=user_id, private=message.chat.type == "private"),
        )

    @dp.callback_query(F.data == "settings:wiki")
    async def settings_wikipedia_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        if callback.message is None:
            await callback.answer()
            return
        mode = get_user_wikipedia_mode(callback.from_user.id)
        await callback.answer()
        await _edit_wiki_mode(callback.message, locale, mode)

    @dp.callback_query(F.data.in_({"settings:wiki:raw", "settings:wiki:clean"}))
    async def settings_wikipedia_mode_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        if callback.message is None:
            await callback.answer()
            return
        mode = callback.data.rsplit(":", 1)[-1]
        selected = set_user_wikipedia_mode(callback.from_user.id, mode)
        await callback.answer()
        await _edit_wiki_mode(callback.message, locale, selected)

    @dp.callback_query(F.data == "settings:language")
    async def settings_language_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        if callback.message is None:
            await callback.answer()
            return
        await callback.answer()
        await callback.message.edit_text(
            text("settings", "choose_language", locale=locale),
            reply_markup=_language_keyboard(locale),
        )

    @dp.callback_query(F.data.startswith("settings:locale:"))
    async def settings_locale_callback(callback: CallbackQuery) -> None:
        data = SettingsCallback.parse(callback.data)
        current_locale = get_user_locale(callback.from_user.id)
        if (
            data is None
            or data.action != "locale"
            or data.value not in available_locales()
        ):
            await callback.answer(
                text("settings", "unsupported_language", locale=current_locale),
                show_alert=True,
            )
            return

        locale = set_user_locale(callback.from_user.id, data.value)
        if callback.message is None:
            await callback.answer()
            return
        await callback.answer(
            text(
                "settings",
                "language_saved",
                locale=locale,
                language=locale_name(locale, display_locale=locale),
            )
        )
        await callback.message.edit_text(
            _settings_text(locale),
            reply_markup=_settings_keyboard(
                locale, user_id=callback.from_user.id,
                private=callback.message.chat.type == "private",
            ),
        )

    @dp.callback_query(F.data == "settings:back")
    async def settings_back_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        if callback.message is None:
            await callback.answer()
            return
        await callback.answer()
        await callback.message.edit_text(
            _settings_text(locale),
            reply_markup=_settings_keyboard(
                locale, user_id=callback.from_user.id,
                private=callback.message.chat.type == "private",
            ),
        )

    register_admin_config_handlers(dp)
