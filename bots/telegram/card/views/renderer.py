from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

from bots.telegram.callbacks import resolve_key_or_legacy_index
from bots.telegram.card.ui.constants import STANDARD_MESSAGE_LIMIT
from bots.telegram.card.ui.keyboards import (
    description_keyboard,
    fallback_main_keyboard,
    navigation_keyboard,
    source_section_keyboard,
)
from bots.telegram.card.ui.selectors import (
    description_source_options,
    source_section_options,
)
from bots.telegram.card.views.main import display_anime_title, main_rich
from bots.telegram.card.views.statistics import (
    _statistics_tables_html,
    format_statistics,
)
from bots.telegram.card.views.rendering import (
    append_block,
    dedupe_strings,
    format_number,
    image_gallery_html,
    plain_text_to_rich_html,
    poster_html,
    rich,
)
from bots.telegram.card.views.rendering import (
    split_text as _split_text,
)
from bots.telegram.card.views.sections import (
    anime_sections,
    description_pages,
    format_people,
    format_screenshots,
    rich_view,
    screenshot_urls,
    screenshots_rich_html,
    source_section_pages,
    source_section_text,
    view_pages,
)
from bots.telegram.card.views.titles import _titles_table_html, format_titles
from bots.telegram.rich_media import deliver_rich_media
from core.models import AggregatedAnime
from localization import DEFAULT_LOCALE

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
    "format_statistics",
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
    allow_overflow_messages: bool = True,
) -> None:
    """Edit callback text; optional replacement never creates new messages."""
    chunks = split_text(text_value, limit, locale=locale)
    await message.edit_text(text=chunks[0], reply_markup=reply_markup)
    if not allow_overflow_messages or isinstance(
        getattr(message, "inline_message_id", None), str
    ):
        return
    for chunk in chunks[1:]:
        await message.answer(chunk)


async def send_standard_text(
    message: Message,
    text_value: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    locale: str = DEFAULT_LOCALE,
    limit: int = STANDARD_MESSAGE_LIMIT,
) -> Message | None:
    chunks = split_text(text_value, limit, locale=locale)
    sent: Message | None = None
    if isinstance(getattr(message, "inline_message_id", None), str):
        await message.edit_text(text=chunks[0], reply_markup=reply_markup)
        return message
    for index, chunk in enumerate(chunks):
        sent = await message.answer(
            chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )
    return sent


async def _deliver_rich_card(
    send: Callable[[InputRichMessage], Awaitable[object]],
    token: str,
    anime: AggregatedAnime,
    section: str,
    text_value: str,
    *,
    locale: str,
    bot=None,
) -> None:
    await deliver_rich_media(
        send,
        rich_view(token, anime, section, text_value, locale=locale),
        bot=bot,
    )


async def send_anime_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    section: str = "main",
    page: int = 0,
    *,
    locale: str,
    show_related: bool = False,
) -> tuple[Message | None, bool]:
    """Send a card and return its Telegram message plus fallback-mode flag."""
    pages = view_pages(anime, section, locale=locale)
    page = max(0, min(page, len(pages) - 1))
    keyboard = navigation_keyboard(
        token,
        anime,
        section,
        page,
        len(pages),
        locale=locale,
        show_related=show_related,
    )
    text_value = pages[page]
    sent: Message | None = None

    async def send_rich(content: InputRichMessage) -> object:
        nonlocal sent
        response = await message.answer_rich(content, reply_markup=keyboard)
        if isinstance(response, Message):
            sent = response
        elif isinstance(getattr(message, "inline_message_id", None), str):
            sent = message
        return response

    try:
        await _deliver_rich_card(
            send_rich,
            token,
            anime,
            section,
            text_value,
            locale=locale,
            bot=getattr(message, "bot", None),
        )
        return sent, False
    except Exception:
        logging.exception(
            "sendRichMessage failed; falling back to standard sendMessage"
        )
        fallback = (
            fallback_main_keyboard(
                token,
                locale=locale,
                show_all_ids=bool(anime.amdb_id),
                show_related=show_related,
            )
            if section == "main"
            else keyboard
        )
        sent = await send_standard_text(
            message,
            text_value,
            reply_markup=fallback,
            locale=locale,
            limit=STANDARD_MESSAGE_LIMIT if section == "main" else 4050,
        )
        return sent, True


async def edit_anime_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    section: str,
    page: int,
    *,
    locale: str,
    show_related: bool = False,
    replace_only: bool = False,
    related_back: bool = False,
) -> bool:
    pages = view_pages(anime, section, locale=locale)
    page = max(0, min(page, len(pages) - 1))
    keyboard = navigation_keyboard(
        token,
        anime,
        section,
        page,
        len(pages),
        locale=locale,
        show_related=show_related,
        related_back=related_back,
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
            bot=getattr(message, "bot", None),
        )
        return False
    except Exception:
        logging.exception(
            "Rich message edit failed; falling back to standard text edit"
        )
        fallback = (
            fallback_main_keyboard(
                token,
                locale=locale,
                show_all_ids=bool(anime.amdb_id),
                show_related=show_related,
                related_back=related_back,
            )
            if section == "main"
            else keyboard
        )
        await edit_standard_text(
            message,
            text_value,
            reply_markup=fallback,
            locale=locale,
            limit=STANDARD_MESSAGE_LIMIT if section == "main" else 4050,
            allow_overflow_messages=not replace_only,
        )
        return True


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
    pages = description_pages(anime, source, locale=locale)
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
            limit=4050,
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
    pages = source_section_pages(anime, section, source, locale=locale)
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
    elif section == "statistics" and len(pages) == 1:
        stats = anime.statistics_by_source.get(source)
        rich_message = InputRichMessage(
            html=(
                _statistics_tables_html(source, stats, locale=locale)
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
        await deliver_rich_media(
            lambda content: message.edit_text(
                rich_message=content,
                reply_markup=keyboard,
            ),
            rich_message,
            bot=getattr(message, "bot", None),
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
            limit=4050,
        )
