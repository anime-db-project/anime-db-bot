from __future__ import annotations

import asyncio
import hashlib
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


def register_inline_handlers(dp, search_service, direct_lookup):
    @dp.inline_query()
    async def inline_search(query: InlineQuery):
        locale = get_user_locale(query.from_user.id)
        value = query.query.strip()
        if len(value) < 2 or len(value) > 140 or query.offset:
            await query.answer([], cache_time=0, is_personal=True)
            return
        try:
            # Inline requests have a much shorter interaction budget. Collect
            # one page per provider, with no extra expansion or image downloads.
            partial = None
            async def remember(result):
                nonlocal partial
                partial = result
            try:
                result = await asyncio.wait_for(search_service.collect(value, page_size=10, max_pages=1, on_partial=remember), timeout=5)
            except TimeoutError:
                result = partial
            if result is None:
                await query.answer([], cache_time=0, is_personal=True)
                return
            username = (await query.bot.me()).username
            items = []
            for candidate in result.items[:20]:
                payload = deep_link_payload(candidate)
                if not payload:
                    continue
                link = f"https://t.me/{username}?start={payload}"
                label = text("project", "details", locale=locale)
                name = candidate.title[:180]
                year = str(candidate.year) if candidate.year else ""
                content = "<b>" + html.escape(name) + "</b>" + ("\n" + year if year else "") + "\n\n" + f'<a href="{link}">{html.escape(label)}</a>'
                poster = next((p for p in candidate.posters if p.startswith("https://")), None)
                items.append(InlineQueryResultArticle(
                    id=hashlib.sha256(payload.encode()).hexdigest()[:32], title=name, description=year or None,
                    thumbnail_url=poster, input_message_content=InputTextMessageContent(message_text=content, parse_mode="HTML"),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=link)]])
                ))
            await query.answer(items, cache_time=0, is_personal=True)
        except Exception:
            await query.answer([], cache_time=0, is_personal=True)
