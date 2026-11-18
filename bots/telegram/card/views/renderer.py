from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

from bots.telegram.callbacks import resolve_key_or_legacy_index
from bots.telegram.card.ui.constants import RICH_MESSAGE_LIMIT, STANDARD_MESSAGE_LIMIT
from bots.telegram.card.ui.keyboards import (
    description_keyboard,
    fallback_main_keyboard,
    navigation_keyboard,
    source_section_keyboard,
)
from bots.telegram.card.views.main import display_anime_title, main_rich
from bots.telegram.card.views.popularity import (
    _popularity_tables_html,
    format_popularity,
)
from bots.telegram.card.views.rendering import (
    append_block,
    dedupe_strings,
    format_number,
    image_gallery_html,
    plain_text_to_rich_html,
    poster_html,
    rich,
    split_text as _split_text,
)
from bots.telegram.card.views.sections import (
    anime_sections,
    format_people,
    format_screenshots,
    rich_view,
    screenshot_urls,
    screenshots_rich_html,
    source_section_text,
    view_pages,
)
from bots.telegram.card.ui.selectors import (
    description_source_options,
    source_section_options,
)
from bots.telegram.card.views.titles import _titles_table_html, format_titles
from core.cache import BoundedTTLCache
from core.models import AggregatedAnime
from localization import DEFAULT_LOCALE, text

__all__ = [
    "anime_sections",
    "append_block",
    "dedupe_strings",
    "display_anime_title",
    "edit_anime_view",
    "edit_description_view",
    "edit_source_section_view",
    "edit_standard_text",
    "format_number",
    "format_people",
    "format_popularity",
    "format_screenshots",
    "format_titles",
    "image_gallery_html",
    "main_rich",
    "plain_text_to_rich_html",
    "poster_html",
    "rich",
    "rich_view",
    "screenshot_urls",
    "screenshots_rich_html",
    "send_anime_view",
    "send_standard_text",
    "source_section_text",
    "split_text",
    "view_pages",
]


def split_text(
    text_value: str, limit: int, *, locale: str = DEFAULT_LOCALE
) -> list[str]:
    return _split_text(text_value, limit, locale=locale)


async def edit_standard_text(
    message: Message,
    text_value: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    locale: str = DEFAULT_LOCALE,
    limit: int = STANDARD_MESSAGE_LIMIT,
) -> None:
    """Edit callback text without discarding overflow chunks."""
    chunks = split_text(text_value, limit, locale=locale)
    await message.edit_text(text=chunks[0], reply_markup=reply_markup)
    for chunk in chunks[1:]:
        await message.answer(chunk)


async def send_standard_text(
    message: Message,
    text_value: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    locale: str = DEFAULT_LOCALE,
) -> None:
    chunks = split_text(text_value, STANDARD_MESSAGE_LIMIT, locale=locale)
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


_PHOTO_MEDIA_ERROR = "RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND"
_POSTER_FAILURES = BoundedTTLCache[tuple[str, ...], bool](
    max_entries=512,
    ttl_seconds=1800.0,
    name="telegram_poster_media_failure",
)


async def _deliver_rich_card(
    send: Callable[[InputRichMessage], Awaitable[object]],
    token: str,
    anime: AggregatedAnime,
    section: str,
    text_value: str,
    *,
    locale: str,
) -> None:
    """Preserve rich tables/buttons when Telegram cannot fetch a remote poster.

    Once a poster set fails, prefer the media-free rich card for half an hour,
    avoiding an extra failing request on each repeated card opening.
    """
    poster_key = tuple(anime.posters[:50]) if section == "main" else ()
    with_posters = not poster_key or _POSTER_FAILURES.get(poster_key) is not True
    try:
        await send(
            rich_view(
                token,
                anime,
                section,
                text_value,
                locale=locale,
                include_posters=with_posters,
            )
        )
    except TelegramBadRequest as exc:
        if not poster_key or not with_posters or _PHOTO_MEDIA_ERROR not in str(exc):
            raise
        logging.warning(
            "Telegram could not fetch card poster; retrying rich card without media",
            extra={
                "event": "telegram_rich_poster_retry",
                "outcome": "retry",
                "size": len(poster_key),
            },
        )
        _POSTER_FAILURES.set(poster_key, True)
        await send(
            rich_view(
                token,
                anime,
                section,
                text_value,
                locale=locale,
                include_posters=False,
            )
        )


async def send_anime_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    section: str = "main",
    page: int = 0,
    *,
    locale: str,
) -> None:
    pages = view_pages(anime, section, locale=locale)
    page = max(0, min(page, len(pages) - 1))
    keyboard = navigation_keyboard(
        token, anime, section, page, len(pages), locale=locale
    )
    text_value = pages[page]

    try:
        await _deliver_rich_card(
            lambda content: message.answer_rich(content, reply_markup=keyboard),
            token,
            anime,
            section,
            text_value,
            locale=locale,
        )
    except Exception:
        logging.exception(
            "sendRichMessage failed; falling back to standard sendMessage"
        )
        fallback = (
            fallback_main_keyboard(
                token, locale=locale, show_all_ids=bool(anime.amdb_id)
            )
            if section == "main"
            else keyboard
        )
        await send_standard_text(
            message, text_value, reply_markup=fallback, locale=locale
        )


async def edit_anime_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    section: str,
    page: int,
    *,
    locale: str,
) -> None:
    pages = view_pages(anime, section, locale=locale)
    page = max(0, min(page, len(pages) - 1))
    keyboard = navigation_keyboard(
        token, anime, section, page, len(pages), locale=locale
    )
    text_value = pages[page]

    try:
        await _deliver_rich_card(
            lambda content: message.edit_text(
                rich_message=content,
                reply_markup=keyboard,
            ),
            token,
            anime,
            section,
            text_value,
            locale=locale,
        )
    except Exception:
        logging.exception(
            "Rich message edit failed; falling back to standard text edit"
        )
        fallback = (
            fallback_main_keyboard(
                token, locale=locale, show_all_ids=bool(anime.amdb_id)
            )
            if section == "main"
            else keyboard
        )
        await edit_standard_text(
            message,
            text_value,
            reply_markup=fallback,
            locale=locale,
        )


async def edit_description_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    source_key: str,
    page: int,
    *,
    locale: str,
) -> None:
    options = description_source_options(anime)
    keys = [key for key, _ in options]
    source_key = resolve_key_or_legacy_index(source_key, keys)
    source = dict(options)[source_key]
    description = anime.descriptions.get(source, "")
    text_value = text("card", "description_title", locale=locale, source=source)
    if description:
        text_value += f"\n\n{description}"
    background = anime.backgrounds_by_source.get(source)
    if background is None and source in {"MyAnimeList", "MAL (MyAnimeList) / Jikan"}:
        background = anime.background
    if background:
        text_value += f"\n\n{text('card', 'extra_info', locale=locale)}\n{background}"
    if not description and not background:
        text_value += f"\n\n{text('common', 'no_data', locale=locale)}"

    pages = split_text(text_value, RICH_MESSAGE_LIMIT, locale=locale)
    page = max(0, min(page, len(pages) - 1))
    keyboard = description_keyboard(token, source_key, page, len(pages), locale=locale)

    try:
        await message.edit_text(
            rich_message=rich(pages[page]),
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception(
            "Rich description edit failed; falling back to standard text edit"
        )
        await edit_standard_text(
            message,
            pages[page],
            reply_markup=keyboard,
            locale=locale,
        )


async def edit_source_section_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    section: str,
    source_key: str,
    page: int,
    *,
    locale: str,
) -> None:
    options = source_section_options(anime, section)
    keys = [key for key, _ in options]
    source_key = resolve_key_or_legacy_index(source_key, keys)
    source = dict(options)[source_key]
    section_text = source_section_text(anime, section, source, locale=locale)
    pages = split_text(
        section_text,
        RICH_MESSAGE_LIMIT,
        locale=locale,
    )
    page = max(0, min(page, len(pages) - 1))
    keyboard = source_section_keyboard(
        token,
        anime,
        section,
        source_key,
        page,
        len(pages),
        locale=locale,
    )

    if section == "titles" and len(pages) == 1:
        rich_message = InputRichMessage(
            html=_titles_table_html(
                anime.titles_by_source.get(source, {}),
                anime.synonyms_by_source.get(source, []),
                source=source,
                locale=locale,
            )
        )
    elif section == "popularity" and len(pages) == 1:
        stats = anime.popularity_by_source.get(source)
        rich_message = InputRichMessage(
            html=(
                _popularity_tables_html(source, stats, locale=locale)
                if stats is not None
                else plain_text_to_rich_html(pages[page])
            )
        )
    elif section == "screenshots" and len(pages) == 1:
        rich_message = InputRichMessage(
            html=screenshots_rich_html(anime, source=source, locale=locale)
        )
    else:
        rich_message = rich(pages[page])

    try:
        await message.edit_text(
            rich_message=rich_message,
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception(
            "Rich source section edit failed; falling back to standard text edit"
        )
        await edit_standard_text(
            message,
            pages[page],
            reply_markup=keyboard,
            locale=locale,
        )
