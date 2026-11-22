"""Private, administrator-only Telegram UI for every documented ENV option.

No credentials are ever displayed or echoed in bot responses. Edits use the
same on-disk config implementation as the operator CLI, with a preview and
explicit confirmation. Changes take effect after a normal bot restart.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import logging
import secrets
from typing import Callable

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bots.session_store import ExpiringSessionStore
from bots.telegram.pagination import indexed_selection_rows, selection_page_size
from core.settings import ConfigFileError
from scripts.console_config import (
    documented_config_groups,
    inspect_key,
    update_key,
    validate_key_value,
)
from services.users.access_control import is_telegram_admin
from services.users.settings import get_user_locale

CONFIRM_TTL_SECONDS = 300
SENSITIVE_WARNING_KEYS = frozenset({
    "BOT_TOKEN", "ADMIN_TELEGRAM_USER_IDS", "TELEGRAM_ALLOWED_USER_IDS",
    "API_ADMIN_KEY", "API_READ_KEY", "API_AUTH_MODE", "LOG_REDACT_SECRETS",
    "TELEGRAM_BOT_API_URL", "STORAGE_BACKEND",
})


@dataclass(frozen=True, slots=True)
class PendingInput:
    action: str
    key: str = ""


@dataclass(frozen=True, slots=True)
class PendingChange:
    owner_id: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SearchResults:
    owner_id: int
    keys: tuple[str, ...]


def catalog() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Only metadata: never read active keys or return unmasked secrets."""
    return documented_config_groups()


def search_keys(query: str) -> tuple[str, ...]:
    query = query.casefold().strip()
    if not query or len(query) > 90:
        return ()
    return tuple(
        key
        for _group, keys in catalog()
        for key in keys
        if query in key.casefold()
    )


@lru_cache(maxsize=512)
def description_for(key: str, group: str) -> str:
    """Return nearby shipped comments, not remote/active environment values."""
    from core.settings import RESOURCE_DIR

    path = RESOURCE_DIR / ".env.example.d" / group
    if not path.is_file():
        return ""
    comments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            content = stripped.lstrip("#").strip()
            if content and not set(content) <= {"=", "-", "#"}:
                comments.append(content)
            continue
        if stripped.startswith(key + "="):
            return " ".join(comments[-3:])[:300]
        if stripped and not stripped.startswith("#"):
            comments = []
        else:
            comments = []
    return ""


def _is_private(message: Message | None) -> bool:
    return bool(message is not None and message.chat.type == "private")


def _tr(locale: str, english: str, russian: str) -> str:
    return russian if locale.startswith("ru") else english


def _button(title: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=title, callback_data="cfg:" + action)


def _markup(
    rows: list[list[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _groups_page(locale: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    groups = catalog()
    total = len(groups)
    rows, page, _ = indexed_selection_rows(
        [f"{name.removesuffix('.env')} ({len(keys)})" for name, keys in groups],
        window=page,
        block="settings",
        destination=lambda index: f"cfg:g:{index}:0",
        change_window=lambda index: f"cfg:h:{index}",
    )
    rows.append([
        _button(_tr(locale, "Search by name", "Поиск по названию"), "search")
    ])
    # Return to the original /settings message without issuing a new command.
    rows.append([InlineKeyboardButton(
        text=_tr(locale, "Back to settings", "Назад к настройкам"),
        callback_data="settings:back",
    )])
    count = sum(len(keys) for _group, keys in groups)
    header = _tr(
        locale,
        f"Bot configuration: {count} ENV options in {total} groups.",
        f"Настройки бота: {count} ENV-параметров в {total} категориях.",
    )
    return (
        header + "\n" + _tr(
            locale,
            "Admin-only. Saved edits require restart; secrets are always hidden.",
            "Только для администраторов. Изменения требуют перезапуска; секреты скрыты.",
        ),
        _markup(rows),
    )


def _group_page(locale: str, group_index: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    groups = catalog()
    if group_index < 0 or group_index >= len(groups):
        raise ValueError("Invalid configuration group")
    group, keys = groups[group_index]
    rows, _, _ = indexed_selection_rows(
        list(keys),
        window=page,
        block="settings",
        destination=lambda index: f"cfg:k:{group_index}:{index}",
        change_window=lambda index: f"cfg:g:{group_index}:{index}",
    )
    rows.append([_button(_tr(locale, "Back to groups", "К категориям"), "h:0")])
    return (
        f"{group} · "
        + _tr(locale, f"{len(keys)} options", f"{len(keys)} параметров"),
        _markup(rows),
    )


def _key_location(group_index: int, key_index: int) -> tuple[str, str]:
    groups = catalog()
    if group_index < 0 or group_index >= len(groups):
        raise ValueError("Unknown group")
    group, keys = groups[group_index]
    if key_index < 0 or key_index >= len(keys):
        raise ValueError("Unknown option")
    return group, keys[key_index]


def _find_key(key: str) -> tuple[int, int] | None:
    for gi, (_group, keys) in enumerate(catalog()):
        if key in keys:
            return gi, keys.index(key)
    return None


async def _key_page(
    locale: str, group_index: int, key_index: int
) -> tuple[str, InlineKeyboardMarkup]:
    group, key = _key_location(group_index, key_index)
    info = await asyncio.to_thread(inspect_key, key)
    raw = str(info.get("value") or "")
    if info.get("masked"):
        rendered = "<hidden>" if info.get("configured") else "<not configured>"
    else:
        rendered = (raw[:450] + "..." if len(raw) > 450 else raw) or "<empty>"
    description = description_for(key, group)
    body = (
        f"{key}\n"
        f"Group: {group}\n"
        f"Source: {info['source']}\n"
        f"Value: {rendered}\n"
        f"Configured: {'yes' if info['configured'] else 'no'}"
    )
    if description:
        body += f"\n\n{description}"
    if key in SENSITIVE_WARNING_KEYS:
        body += "\n\n" + _tr(
            locale,
            "Warning: modifying this option may affect access or security.",
            "Внимание: изменение параметра может повлиять на доступ или безопасность.",
        )
    rows = [
        [
            _button(_tr(locale, "Edit", "Изменить"), f"e:{group_index}:{key_index}"),
            _button(_tr(locale, "Reset", "Сбросить"), f"r:{group_index}:{key_index}"),
        ],
        [
            _button(
                _tr(locale, "Back", "Назад"),
                f"g:{group_index}:{key_index // selection_page_size('settings')}",
            ),
            _button(_tr(locale, "Categories", "Категории"), "h:0"),
        ],
    ]
    return body, _markup(rows)


def _search_page(
    locale: str, token: str, record: SearchResults, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    rows, _, _ = indexed_selection_rows(
        list(record.keys),
        window=page,
        block="settings",
        destination=lambda index: f"cfg:hit:{token}:{index}",
        change_window=lambda index: f"cfg:res:{token}:{index}",
    )
    rows.append([_button(_tr(locale, "Categories", "Категории"), "h:0")])
    return (
        _tr(
            locale,
            f"Found {len(record.keys)} options.",
            f"Найдено параметров: {len(record.keys)}.",
        ),
        _markup(rows),
    )


def register_admin_config_handlers(dp: Dispatcher) -> None:
    inputs = ExpiringSessionStore[PendingInput](
        max_entries=128, ttl_seconds=CONFIRM_TTL_SECONDS,
        name="admin_env_input",
    )
    changes = ExpiringSessionStore[PendingChange](
        max_entries=128, ttl_seconds=CONFIRM_TTL_SECONDS,
        name="admin_env_confirmation",
    )
    searches = ExpiringSessionStore[SearchResults](
        max_entries=128, ttl_seconds=CONFIRM_TTL_SECONDS,
        name="admin_env_search",
    )

    def _token() -> str:
        return secrets.token_hex(5)

    def _authorized(user_id: int, message: Message | None) -> bool:
        return is_telegram_admin(user_id) and _is_private(message)

    @dp.message(Command("admin_config", "bot_config"))
    async def open_admin_config(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if user_id is None or not _authorized(user_id, message):
            await message.answer("Administrator settings are available only in a private chat.")
            return
        inputs.pop(str(user_id), None)
        message_text, markup = _groups_page(get_user_locale(user_id))
        await message.answer(message_text, reply_markup=markup)

    @dp.callback_query(F.data.startswith("cfg:"))
    async def config_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        if not _authorized(user_id, callback.message):
            await callback.answer(
                "Administrator settings require a private chat.", show_alert=True
            )
            return
        parts = (callback.data or "").split(":")[1:]
        locale = get_user_locale(user_id)
        try:
            if not parts:
                raise ValueError("Empty action")
            action = parts[0]
            if action == "h" and len(parts) == 2:
                inputs.pop(str(user_id), None)
                message_text, markup = _groups_page(locale, int(parts[1]))
            elif action == "g" and len(parts) == 3:
                inputs.pop(str(user_id), None)
                message_text, markup = _group_page(
                    locale, int(parts[1]), int(parts[2])
                )
            elif action == "k" and len(parts) == 3:
                inputs.pop(str(user_id), None)
                message_text, markup = await _key_page(
                    locale, int(parts[1]), int(parts[2])
                )
            elif action == "search" and len(parts) == 1:
                inputs[str(user_id)] = PendingInput("search")
                await callback.answer()
                await callback.message.answer(_tr(
                    locale,
                    "Send part of an ENV option name. /admin_config cancels.",
                    "Отправьте часть имени ENV-параметра. /admin_config — отмена.",
                ))
                return
            elif action == "res" and len(parts) == 3:
                token = parts[1]
                record = searches.get(token)
                if record is None or record.owner_id != user_id:
                    raise ValueError("Search session expired")
                message_text, markup = _search_page(locale, token, record, int(parts[2]))
            elif action == "hit" and len(parts) == 3:
                record = searches.get(parts[1])
                if record is None or record.owner_id != user_id:
                    raise ValueError("Search session expired")
                key = record.keys[int(parts[2])]
                location = _find_key(key)
                if location is None:
                    raise ValueError("Unknown option")
                message_text, markup = await _key_page(locale, *location)
            elif action in {"e", "r"} and len(parts) == 3:
                group_index = int(parts[1])
                key_index = int(parts[2])
                _, key = _key_location(group_index, key_index)
                if action == "e":
                    inputs[str(user_id)] = PendingInput("value", key)
                    await callback.answer()
                    await callback.message.answer(
                        f"{key}\n" + _tr(
                            locale,
                            "Send a new value (single line). Your message will be deleted. "
                            "Use /admin_config to cancel.",
                            "Отправьте новое значение (одной строкой). "
                            "Ваше сообщение будет удалено. /admin_config — отмена.",
                        )
                    )
                    return
                token = _token()
                changes[token] = PendingChange(user_id, key, None)
                message_text = _tr(
                    locale,
                    f"Remove local override for {key}? Requires restart.",
                    f"Сбросить локальное значение {key}? Нужен перезапуск.",
                )
                markup = _markup([[
                    _button(_tr(locale, "Confirm", "Подтвердить"), f"yes:{token}"),
                    _button(_tr(locale, "Cancel", "Отмена"), f"no:{token}"),
                ]])
            elif action in {"yes", "no"} and len(parts) == 2:
                record = changes.get(parts[1])
                if record is None or record.owner_id != user_id:
                    raise ValueError("Confirmation expired or belongs to another admin")
                if action == "no":
                    changes.pop(parts[1], None)
                    message_text = _tr(locale, "Change canceled.", "Изменение отменено.")
                else:
                    try:
                        result = await asyncio.to_thread(
                            update_key, record.key, record.value
                        )
                    except (ValueError, ConfigFileError, OSError) as exc:
                        logging.warning(
                            "Telegram config write rejected for %s: %s",
                            record.key, type(exc).__name__,
                        )
                        await callback.answer(
                            _tr(
                                locale,
                                "Invalid configuration value or write failed.",
                                "Неверное значение или ошибка записи.",
                            ),
                            show_alert=True,
                        )
                        return
                    changes.pop(parts[1], None)
                    message_text = _tr(
                        locale,
                        f"{record.key}: saved. Restart the bot to apply changes."
                        if result["changed"]
                        else f"{record.key}: no changes.",
                        f"{record.key}: сохранено. Перезапустите бота для применения."
                        if result["changed"]
                        else f"{record.key}: изменений нет.",
                    )
                markup = _markup([[
                    _button(_tr(locale, "Categories", "Категории"), "h:0")
                ]])
            else:
                raise ValueError("Unknown action")
            await callback.answer()
            await callback.message.edit_text(message_text, reply_markup=markup)
        except (IndexError, KeyError, ValueError, OverflowError):
            await callback.answer(
                _tr(locale, "Invalid or expired settings menu.", "Неверное или устаревшее меню."),
                show_alert=True,
            )

    async def awaiting_text(message: Message) -> bool:
        user = message.from_user
        return bool(
            user and _authorized(user.id, message)
            and inputs.get(str(user.id)) is not None
        )

    @dp.message(F.text, awaiting_text)
    async def new_admin_setting_input(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if user_id is None or not _authorized(user_id, message):
            return
        pending = inputs.pop(str(user_id), None)
        if pending is None:
            return
        locale = get_user_locale(user_id)
        submitted = message.text or ""
        deleted = False
        try:
            await message.delete()
            deleted = True
        except Exception:
            logging.warning(
                "Could not delete admin config input message; administrator=%s",
                user_id,
            )
        if pending.action == "search":
            matches = search_keys(submitted)
            if not matches:
                await message.answer(
                    _tr(locale, "No matching ENV options.", "Параметры не найдены.")
                )
                return
            token = _token()
            record = SearchResults(user_id, matches)
            searches[token] = record
            message_text, markup = _search_page(locale, token, record, 0)
            await message.answer(message_text, reply_markup=markup)
            return
        if pending.action != "value":
            return

        info = await asyncio.to_thread(inspect_key, pending.key)
        if info["masked"] and not deleted:
            await message.answer(
                _tr(
                    locale,
                    "Could not remove the secret-bearing message. "
                    "Delete it manually and use the local CLI instead.",
                    "Не удалось удалить сообщение с секретом. "
                    "Удалите его вручную и используйте локальную консоль.",
                )
            )
            return
        if len(submitted) > 4096 or "\n" in submitted or "\r" in submitted:
            await message.answer(
                _tr(locale, "Value must be one line, up to 4096 chars.",
                    "Значение должно быть одной строкой до 4096 символов.")
            )
            return
        try:
            await asyncio.to_thread(validate_key_value, pending.key, submitted)
        except (ValueError, ConfigFileError, OSError):
            await message.answer(
                _tr(locale, f"{pending.key}: validation failed; nothing was saved.",
                    f"{pending.key}: проверка не пройдена, ничего не сохранено.")
            )
            return
        token = _token()
        changes[token] = PendingChange(user_id, pending.key, submitted)
        rendered = "<hidden>" if info["masked"] else (submitted[:140] or "<empty>")
        message_text = (
            f"{pending.key} = {rendered}\n"
            + _tr(locale, "Save this value? Requires restart.",
                  "Сохранить это значение? Нужен перезапуск.")
        )
        if pending.key in SENSITIVE_WARNING_KEYS:
            message_text += "\n" + _tr(
                locale,
                "Warning: this option may change administrator access or security.",
                "Внимание: параметр может изменить административный доступ или безопасность.",
            )
        await message.answer(
            message_text,
            reply_markup=_markup([[
                _button(_tr(locale, "Confirm", "Подтвердить"), f"yes:{token}"),
                _button(_tr(locale, "Cancel", "Отмена"), f"no:{token}"),
            ]]),
        )
