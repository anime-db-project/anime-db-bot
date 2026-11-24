from __future__ import annotations

import logging
from aiogram import BaseMiddleware
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, BotCommandScopeChatMember, Message
from core.cache import BoundedTTLCache
from localization import text, available_locales
from services.users.access_control import is_telegram_admin
from services.users.settings import get_user_locale

PUBLIC = ("start", "anime", "settings", "about", "cancel", "ping")
ADMIN = ("status", "source_health", "users", "ban", "unban", "mapping_overrides", "mapping_history", "mapping_set", "mapping_delete", "mapping_confirm", "mapping_cancel")


def commands_for(locale: str, *, administrator: bool = False) -> list[BotCommand]:
    return [BotCommand(command=key, description=text("commands", key, locale=locale)) for key in PUBLIC + (ADMIN if administrator else ())]


async def configure_command_menu(bot, settings) -> None:
    allowed = set(settings.allowed_user_ids)
    # Set default language scopes explicitly; do not advertise commands to
    # unapproved accounts when whitelist is active.
    for language in ("", *available_locales()):
        try:
            await bot.set_my_commands(
                [] if allowed else commands_for(language or "en"),
                scope=BotCommandScopeDefault(), language_code=language, request_timeout=4,
            )
        except Exception:
            logging.warning("Could not synchronize default command menu")
    for user_id in sorted(allowed | set(settings.admin_user_ids)):
        if allowed and user_id not in allowed:
            continue
        try:
            await bot.set_my_commands(commands_for(get_user_locale(user_id), administrator=is_telegram_admin(user_id)), scope=BotCommandScopeChat(chat_id=user_id), request_timeout=4)
        except Exception:
            logging.debug("Could not synchronize private command menu", exc_info=True)


class CommandMenuMiddleware(BaseMiddleware):
    def __init__(self):
        self._synced = BoundedTTLCache(max_entries=2048, ttl_seconds=3600, name="command_menu")

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        if isinstance(event, Message) and event.from_user and (event.text or "").startswith("/"):
            user_id = event.from_user.id
            locale = get_user_locale(user_id)
            private = event.chat.type == "private"
            key = (event.chat.id, user_id, locale)
            if not self._synced.get(key):
                try:
                    scope = BotCommandScopeChat(chat_id=event.chat.id) if private else BotCommandScopeChatMember(chat_id=event.chat.id, user_id=user_id)
                    await data["bot"].set_my_commands(commands_for(locale, administrator=private and is_telegram_admin(user_id)), scope=scope, request_timeout=3)
                    self._synced.set(key, True)
                except Exception:
                    logging.debug("Could not synchronize member command menu", exc_info=True)
        return result
