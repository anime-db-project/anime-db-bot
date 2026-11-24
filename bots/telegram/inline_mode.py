from __future__ import annotations

import asyncio
import secrets
import logging
from dataclasses import dataclass
from typing import ClassVar
from aiogram import BaseMiddleware
from aiogram.methods.base import TelegramMethod
from aiogram.types import CallbackQuery, InputRichMessage
from aiogram.exceptions import TelegramBadRequest
from bots.session_store import ExpiringSessionStore
from core.settings import get_settings
from core.models import SearchCandidate
import html
import re
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from localization import text
from services.users.settings import get_user_locale

SAFE_IDS = ("mal", "anilist", "shikimori", "kitsu", "anidb", "anisearch")


def deep_link_payload(candidate) -> str | None:
    for source in SAFE_IDS:
        value = str(candidate.external_ids.get(source) or "")
        if value.isascii() and value.isdigit() and 0 < len(value) <= 30:
            return "anime_" + source + "_" + value
    return None


def parse_start_payload(payload: str):
    match = re.fullmatch(r"anime_(mal|anilist|shikimori|kitsu|anidb|anisearch)_([0-9]{1,30})", payload or "")
    return match.groups() if match else None


class SetInlineSettings(TelegramMethod[bool]):
    __returning__: ClassVar[type] = bool
    __api_method__: ClassVar[str] = "setMySettings"
    supports_inline_queries: bool
    inline_query_placeholder: str = "Anime title / ID"


async def synchronize_inline_setting(bot, settings):
    enabled = settings.inline_enabled and not settings.allowed_group_ids
    try:
        me = await bot.get_me(request_timeout=5)
        if bool(me.supports_inline_queries) != enabled:
            await bot(SetInlineSettings(supports_inline_queries=enabled), request_timeout=5)
    except Exception:
        # The local authorization gate remains authoritative even with older Bot APIs.
        logging.warning("Could not synchronize Telegram inline setting")


@dataclass(slots=True)
class InlineChoice:
    candidate: SearchCandidate
    owner: int


class InlineMessage:
    """Message-like adapter for the card renderer. No uploads to inline messages."""

    def __init__(self, bot, inline_message_id: str):
        self.bot = bot
        self.inline_message_id = inline_message_id
        self.retry_markup = None

    async def edit_text(self, text=None, *, rich_message=None, reply_markup=None, **kwargs):
        if rich_message is not None:
            # Inline edits cannot upload new files. Replace remote photos by
            # clickable original links instead of triggering the RAM/disk cascade.
            body = rich_message.html or ""
            def image_link(match):
                url = html.unescape(match.group(1))
                if url.startswith(("https://", "http://")):
                    return '<p><a href="' + html.escape(url, quote=True) + '">↗</a></p>'
                return ""
            body = re.sub(r'<img[^>]*?src="([^"]+)"[^>]*?/?>', image_link, body, flags=re.I)
            body = re.sub(r"</?tg-slideshow[^>]*>", "", body, flags=re.I)
            if reply_markup and any((b.callback_data or "").startswith("ci:") and (b.callback_data or "").endswith(":staff") for row in reply_markup.inline_keyboard for b in row):
                body = re.sub(r"<tg-button-row[^>]*>.*?</tg-button-row>", "", body, flags=re.I | re.S)
            rich_message = InputRichMessage(html=body)
        try:
            return await self.bot.edit_message_text(
                text=text, rich_message=rich_message, reply_markup=reply_markup,
                inline_message_id=self.inline_message_id, **kwargs,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            raise

    async def edit_reply_markup(self, *, reply_markup=None):
        return await self.bot.edit_message_reply_markup(inline_message_id=self.inline_message_id, reply_markup=reply_markup)

    async def answer(self, text=None, **kwargs):
        # Separate outgoing messages are not possible in inline mode.
        kwargs.setdefault("reply_markup", self.retry_markup)
        return await self.edit_text(text, **kwargs)

    async def answer_rich(self, content, **kwargs):
        await self.edit_text(rich_message=content, **kwargs)


# Only ordinary anime-card navigation is adapted; settings and admin actions
# are never routed through an inline message.
CARD_PREFIXES = frozenset({"av", "ci", "cp", "ad", "as", "ap", "ae", "ac", "at", "ar", "ari", "wk", "pv", "pr", "ps"})


class InlineCardMiddleware(BaseMiddleware):
    def __init__(self, choices, card_state):
        self.choices = choices
        self.card_state = card_state
        self.locks = ExpiringSessionStore(max_entries=512, ttl_seconds=3600, name="inline_locks")

    async def __call__(self, handler, event, data):
        if not isinstance(event, CallbackQuery) or not event.inline_message_id:
            # Inline card tokens must never be usable as normal-chat callbacks.
            if isinstance(event, CallbackQuery):
                parts = (event.data or "").split(":")
                if len(parts) > 1 and parts[0] in CARD_PREFIXES:
                    session = self.card_state.anime_sessions.get(parts[1])
                    if session is not None and session.inline_message_id is not None:
                        await event.answer()
                        return None
            return await handler(event, data)
        locale = get_user_locale(event.from_user.id)
        settings = get_settings().telegram
        if not settings.inline_enabled or settings.allowed_group_ids:
            await event.answer(settings.denied_message, show_alert=True)
            return None
        parts = (event.data or "").split(":")
        if len(parts) < 2:
            await event.answer()
            return None
        message_id = event.inline_message_id
        lock = self.locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[message_id] = lock
        async with lock:
            message = InlineMessage(event.bot, message_id)
            if parts[0] == "il" and len(parts) == 2:
                choice = self.choices.get(parts[1])
                if choice is None or choice.owner != event.from_user.id:
                    await event.answer(text("card", "expired" if choice is None else "not_owner", locale=locale), show_alert=True)
                    return None
                await event.answer()
                from bots.telegram.card.runtime.service import aggregate_and_send
                message.retry_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text("project", "details", locale=locale), callback_data=event.data)]])
                await message.edit_text(text("search", "loading", locale=locale, title=choice.candidate.title), reply_markup=message.retry_markup)
                await aggregate_and_send(message, choice.candidate, choice.owner, self.card_state)
                return None
            session = self.card_state.anime_sessions.get(parts[1])
            if parts[0] not in CARD_PREFIXES or session is None or session.inline_message_id != message_id or session.owner_user_id != event.from_user.id:
                await event.answer(text("card", "expired" if session is None else "not_owner", locale=locale), show_alert=True)
                return None
            # Use the same aiogram callback handlers, including all ownership
            # checks and page-selection logic, without fabricating a chat ID.
            return await handler(event.model_copy(update={"message": message}), data)


def register_inline_handlers(dp, search_service, direct_lookup, card_state):
    choices = ExpiringSessionStore(max_entries=1024, ttl_seconds=1800, name="inline_choices")

    @dp.callback_query(lambda query: bool(query.data and query.data.startswith("il:")))
    async def inline_open(query: CallbackQuery):
        # InlineCardMiddleware consumes valid inline-card callbacks.
        await query.answer()

    @dp.inline_query()
    async def inline_search(query: InlineQuery):
        locale = get_user_locale(query.from_user.id)
        value = query.query.strip()
        settings = get_settings().telegram
        if not settings.inline_enabled or settings.allowed_group_ids or len(value) < 2 or len(value) > 140 or query.offset:
            await query.answer([], cache_time=0, is_personal=True)
            return
        try:
            partial = None

            async def remember(result):
                nonlocal partial
                partial = result

            from bots.telegram.search.handlers import _parse_direct_query
            direct = _parse_direct_query(value, direct_lookup.source_keys)
            try:
                if direct:
                    source, external_id = direct
                    result = await asyncio.wait_for(direct_lookup.lookup(external_id, source_keys=(source,) if source else None), timeout=5)
                else:
                    result = await asyncio.wait_for(search_service.collect(value, page_size=10, max_pages=1, on_partial=remember), timeout=5)
            except TimeoutError:
                result = partial
            if result is None:
                await query.answer([], cache_time=0, is_personal=True)
                return
            items = []
            for candidate in result.items[:20]:
                if candidate.identity.is_empty():
                    continue
                token = secrets.token_urlsafe(9)
                choices[token] = InlineChoice(candidate, query.from_user.id)
                label = text("project", "details", locale=locale)
                name = candidate.title[:180]
                year = str(candidate.year) if candidate.year else ""
                content = "<b>" + html.escape(name) + "</b>" + ("\n" + year if year else "")
                poster = next((p for p in candidate.posters if p.startswith("https://")), None)
                items.append(InlineQueryResultArticle(
                    id=token, title=name, description=year or None, thumbnail_url=poster,
                    input_message_content=InputTextMessageContent(message_text=content, parse_mode="HTML"),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="il:" + token)]])
                ))
            await query.answer(items, cache_time=0, is_personal=True)
        except Exception:
            logging.debug("Inline search failed", exc_info=True)
            await query.answer([], cache_time=0, is_personal=True)
    return InlineCardMiddleware(choices, card_state)
