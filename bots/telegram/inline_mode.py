from __future__ import annotations

import asyncio
import secrets
import logging
from dataclasses import dataclass
from typing import ClassVar
from aiogram import BaseMiddleware
from aiogram.methods.base import TelegramMethod
from aiogram.types import CallbackQuery, ChosenInlineResult, InputRichMessage
from aiogram.exceptions import TelegramBadRequest
from bots.session_store import ExpiringSessionStore
from core.settings import get_settings
from core.models import SearchCandidate
import html
import re
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputRichMessageContent,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from bots.telegram.inline_media import prepare_inline_rich
from bots.telegram.callbacks import PagePickerCallback
from bots.telegram.pagination import search_picker_keyboard
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
    match = re.fullmatch(
        r"anime_(mal|anilist|shikimori|kitsu|anidb|anisearch)_([0-9]{1,30})",
        payload or "",
    )
    return match.groups() if match else None


class SetInlineSettings(TelegramMethod[bool]):
    __returning__: ClassVar[type] = bool
    __api_method__: ClassVar[str] = "setMySettings"
    supports_inline_queries: bool
    inline_query_placeholder: str = "Anime title / ID"
    inline_feedback_share_denominator: int = 1


async def synchronize_inline_setting(bot, settings):
    enabled = settings.inline_enabled and not settings.allowed_group_ids
    try:
        await bot(
            SetInlineSettings(
                supports_inline_queries=enabled,
                inline_feedback_share_denominator=1 if enabled else 0,
            ),
            request_timeout=5,
        )
    except Exception:
        # The local authorization gate remains authoritative even with older Bot APIs.
        logging.warning("Could not synchronize Telegram inline setting")


@dataclass(slots=True)
class InlineChoice:
    session: object
    owner: int


class InlineMessage:
    """Message-like adapter for the card renderer. No uploads to inline messages."""

    def __init__(self, bot, inline_message_id: str):
        self.bot = bot
        self.inline_message_id = inline_message_id
        self.retry_markup = None

    async def edit_text(
        self, text=None, *, rich_message=None, reply_markup=None, **kwargs
    ):
        original = rich_message
        if rich_message is not None:
            body = rich_message.html or ""
            if reply_markup and any(
                (b.callback_data or "").startswith("ci:")
                and (b.callback_data or "").endswith(":staff")
                for row in reply_markup.inline_keyboard
                for b in row
            ):
                body = re.sub(
                    r"<tg-button-row[^>]*>.*?</tg-button-row>",
                    "",
                    body,
                    flags=re.I | re.S,
                )
            original = rich_message.model_copy(update={"html": body})
            rich_message = await prepare_inline_rich(self.bot, original, upload=True)
        try:
            return await self.bot.edit_message_text(
                text=text,
                rich_message=rich_message,
                reply_markup=reply_markup,
                inline_message_id=self.inline_message_id,
                **kwargs,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            if original is not None and rich_message.media:
                # Invalid/stale file IDs must not destroy the rich message.
                without = await prepare_inline_rich(self.bot, original, use_cache=False)
                return await self.bot.edit_message_text(
                    text=text,
                    rich_message=without,
                    reply_markup=reply_markup,
                    inline_message_id=self.inline_message_id,
                    **kwargs,
                )
            raise

    async def edit_reply_markup(self, *, reply_markup=None):
        return await self.bot.edit_message_reply_markup(
            inline_message_id=self.inline_message_id, reply_markup=reply_markup
        )

    async def answer(self, text=None, **kwargs):
        # Separate outgoing messages are not possible in inline mode.
        kwargs.setdefault("reply_markup", self.retry_markup)
        return await self.edit_text(text, **kwargs)

    async def answer_rich(self, content, **kwargs):
        await self.edit_text(rich_message=content, **kwargs)


# Only ordinary anime-card navigation is adapted; settings and admin actions
# are never routed through an inline message.
CARD_PREFIXES = frozenset(
    {
        "av",
        "ci",
        "cp",
        "ad",
        "as",
        "ap",
        "ae",
        "ac",
        "at",
        "ar",
        "ari",
        "wk",
        "pv",
        "pr",
        "ps",
    }
)


def inline_keyboard(markup):
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for b in row:
            raw = b.callback_data or ""
            if raw.startswith(("sr:", "sd:", "sp:")):
                b = b.model_copy(update={"callback_data": "i" + raw})
            buttons.append(b)
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview(choice, token, index, locale):
    from bots.telegram.search.handlers import (
        _preview_keyboard,
        _preview_text,
        _preview_rich,
    )

    session = choice.session
    candidate = session.items[index]
    plain = _preview_text(session, candidate, locale=locale)
    keyboard = inline_keyboard(
        _preview_keyboard(
            token, index, len(session.items), complete=session.complete, locale=locale
        )
    )
    return plain, _preview_rich(plain, candidate), keyboard


class InlineCardMiddleware(BaseMiddleware):
    def __init__(self, choices, card_state):
        self.choices = choices
        self.card_state = card_state
        self.locks = ExpiringSessionStore(
            max_entries=512, ttl_seconds=3600, name="inline_locks"
        )
        self.advanced = ExpiringSessionStore(
            max_entries=1024, ttl_seconds=3600, name="inline_advanced"
        )

    def lock_for(self, message_id):
        lock = self.locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[message_id] = lock
        return lock

    async def selected(self, event):
        if not event.inline_message_id:
            return
        token, sep, index = event.result_id.rpartition("_")
        choice = self.choices.get(token)
        if (
            not sep
            or choice is None
            or choice.owner != event.from_user.id
            or not index.isdigit()
            or not 0 <= int(index) < len(choice.session.items)
        ):
            return
        mid = event.inline_message_id
        async with self.lock_for(mid):
            if self.advanced.get(mid):
                return
            message = InlineMessage(event.bot, mid)
            locale = get_user_locale(choice.owner)
            plain, rich, keyboard = preview(choice, token, int(index), locale)
            try:
                await message.edit_text(rich_message=rich, reply_markup=keyboard)
            except TelegramBadRequest:
                await message.edit_text(plain, reply_markup=keyboard)

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
        async with self.lock_for(message_id):
            self.advanced[message_id] = True
            message = InlineMessage(event.bot, message_id)
            if parts[0] in {"isr", "isd", "isp"}:
                choice = self.choices.get(parts[1])
                if choice is None or choice.owner != event.from_user.id:
                    await event.answer(
                        text(
                            "card",
                            "expired" if choice is None else "not_owner",
                            locale=locale,
                        ),
                        show_alert=True,
                    )
                    return None
                # Bind each preview message to the immutable query result set;
                # separate messages may reuse that same set independently.
                if parts[0] == "isp":
                    picker = PagePickerCallback.parse(event.data[1:], prefix="sp")
                    if not picker or picker.total != len(choice.session.items):
                        await event.answer()
                        return None
                    await event.answer()
                    await message.edit_reply_markup(
                        reply_markup=inline_keyboard(
                            search_picker_keyboard(picker, locale=locale)
                        )
                    )
                    return None
                if (
                    len(parts) != 3
                    or not parts[2].isdigit()
                    or not 0 <= int(parts[2]) < len(choice.session.items)
                ):
                    await event.answer()
                    return None
                index = int(parts[2])
                await event.answer()
                plain, rich, keyboard = preview(choice, parts[1], index, locale)
                if parts[0] == "isr":
                    try:
                        await message.edit_text(
                            rich_message=rich, reply_markup=keyboard
                        )
                    except TelegramBadRequest:
                        await message.edit_text(plain, reply_markup=keyboard)
                    return None
                from bots.telegram.card.runtime.service import aggregate_and_send

                candidate = choice.session.items[index]
                message.retry_markup = keyboard
                await message.edit_text(
                    text("search", "loading", locale=locale, title=candidate.title),
                    reply_markup=keyboard,
                )
                await aggregate_and_send(
                    message, candidate, choice.owner, self.card_state
                )
                return None
            if parts[0] == "wlcard":
                await event.answer(
                    text("watchlists", "private", locale=locale), show_alert=True
                )
                return None
            if event.data == "noop":
                await event.answer()
                return None
            session = self.card_state.anime_sessions.get(parts[1])
            if (
                parts[0] not in CARD_PREFIXES
                or session is None
                or session.inline_message_id != message_id
                or session.owner_user_id != event.from_user.id
            ):
                await event.answer(
                    text(
                        "card",
                        "expired" if session is None else "not_owner",
                        locale=locale,
                    ),
                    show_alert=True,
                )
                return None
            # Use the same aiogram callback handlers, including all ownership
            # checks and page-selection logic, without fabricating a chat ID.
            return await handler(event.model_copy(update={"message": message}), data)


def register_inline_handlers(dp, search_service, direct_lookup, card_state):
    choices = ExpiringSessionStore(
        max_entries=256, ttl_seconds=1800, name="inline_choices"
    )
    middleware = InlineCardMiddleware(choices, card_state)

    @dp.chosen_inline_result()
    async def selected_result(event: ChosenInlineResult):
        await middleware.selected(event)

    @dp.callback_query(
        lambda query: bool(
            query.data and query.data.startswith(("isr:", "isd:", "isp:"))
        )
    )
    async def inline_open(query: CallbackQuery):
        # InlineCardMiddleware consumes valid inline-card callbacks.
        await query.answer()

    @dp.inline_query()
    async def inline_search(query: InlineQuery):
        locale = get_user_locale(query.from_user.id)
        value = query.query.strip()
        settings = get_settings().telegram
        if (
            not settings.inline_enabled
            or settings.allowed_group_ids
            or len(value) < 2
            or len(value) > 140
            or query.offset
        ):
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
                    result = await asyncio.wait_for(
                        direct_lookup.lookup(
                            external_id, source_keys=(source,) if source else None
                        ),
                        timeout=5,
                    )
                else:
                    result = await asyncio.wait_for(
                        search_service.collect(
                            value, page_size=10, max_pages=1, on_partial=remember
                        ),
                        timeout=5,
                    )
            except TimeoutError:
                result = partial
            if result is None:
                await query.answer([], cache_time=0, is_personal=True)
                return
            from bots.telegram.search.handlers import SearchSession

            candidates = [c for c in result.items if not c.identity.is_empty()][:50]
            if not candidates:
                await query.answer([], cache_time=0, is_personal=True)
                return
            token = secrets.token_urlsafe(9)
            choice = InlineChoice(
                SearchSession(
                    query=value,
                    owner_user_id=query.from_user.id,
                    items=candidates,
                    complete=result.complete and len(result.items) <= 50,
                    errors=dict(result.errors),
                ),
                query.from_user.id,
            )
            choices[token] = choice
            items = []
            fallback = []
            for index, candidate in enumerate(candidates):
                plain, rich, keyboard = preview(choice, token, index, locale)
                # Do not upload dozens of photos during typing. Previously
                # uploaded file IDs are safe in initial inline results.
                prepared = await prepare_inline_rich(query.bot, rich)
                poster = next(
                    (p for p in candidate.posters if p.startswith("https://")), None
                )
                fields = dict(
                    id=token + "_" + str(index),
                    title=candidate.title[:180],
                    description=str(candidate.year) if candidate.year else None,
                    thumbnail_url=poster,
                    reply_markup=keyboard,
                )
                items.append(
                    InlineQueryResultArticle(
                        **fields,
                        input_message_content=InputRichMessageContent(
                            rich_message=prepared
                        ),
                    )
                )
                fallback.append(
                    InlineQueryResultArticle(
                        **fields,
                        input_message_content=InputTextMessageContent(
                            message_text=plain[:4096]
                        ),
                    )
                )
            try:
                await query.answer(items, cache_time=0, is_personal=True)
            except TelegramBadRequest:
                await query.answer(fallback, cache_time=0, is_personal=True)
        except Exception:
            logging.debug("Inline search failed", exc_info=True)
            await query.answer([], cache_time=0, is_personal=True)

    return middleware
