"""Wikipedia screens for an anime card: independent title search and article reader.

A card does not imply an encyclopedia match. The user chooses a page from
Wikipedia search results. All subsequent operations stay on that page and
the article is loaded lazily outside the anime aggregator.
"""
from __future__ import annotations

import html
import logging

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bots.telegram.card.runtime.state import AnimeCardSession, TelegramCardState
from bots.telegram.card.views.renderer import edit_standard_text
from core.models import AggregatedAnime
from services.users.settings import get_user_locale, get_user_wikipedia_mode
from sources.providers.wikipedia import (
    LANGUAGES,
    WikipediaArticle,
    WikipediaHit,
    paginate_section,
    wiki_language,
    wikipedia_client,
)

SECTIONS_PER_PAGE = 7
LANG_LABELS = {
    "ru": "Русский", "en": "English", "ja": "日本語",
    "de": "Deutsch", "es": "Español", "fr": "Français", "it": "Italiano",
}


def _tr(locale: str, ru: str, en: str) -> str:
    return ru if locale == "ru" else en


def _button(label: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label[:60], callback_data=action)


def _back(token: str, locale: str) -> InlineKeyboardButton:
    return _button(_tr(locale, "К карточке", "Back to anime"), f"av:{token}:main:0")


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _search_term(anime: AggregatedAnime, language: str) -> str:
    names = anime.titles
    keys = {
        "ru": ("russian", "romaji", "english"),
        "ja": ("japanese", "romaji", "english"),
    }.get(language, ("english", "romaji", "japanese", "russian"))
    for key in keys:
        value = str(names.get(key) or "").strip()
        if len(value) >= 2:
            return value[:120]
    return str(anime.title).strip()[:120]


def _language_rows(token: str, selected: str) -> list[list[InlineKeyboardButton]]:
    choices = [
        _button(
            ("✓ " if code == selected else "") + LANG_LABELS[code],
            f"wk:{token}:lang:{code}",
        )
        for code in LANGUAGES
    ]
    return [choices[index : index + 3] for index in range(0, len(choices), 3)]


async def _edit_html(
    message: Message, value: str, keyboard: InlineKeyboardMarkup,
    *, locale: str
) -> None:
    """Safe text-mode fallback for Telegram installations rejecting HTML edits."""
    try:
        await message.edit_text(value, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest:
        # The fallback must not expose literal HTML markup or crash the bot.
        plain = html.unescape(
            __import__("re").sub(r"<[^>]+>", "", value)
        )
        await edit_standard_text(
            message, plain, reply_markup=keyboard, locale=locale,
        )


async def _search_screen(
    message: Message, token: str, session: AnimeCardSession, *, locale: str,
) -> None:
    language = session.wikipedia_language or wiki_language(locale)
    hits: tuple[WikipediaHit, ...] = tuple(
        item for item in session.wikipedia_hits if isinstance(item, WikipediaHit)
    )
    if session.wikipedia_language != language or not hits:
        term = _search_term(session.anime, language)
        hits = await wikipedia_client().search(language=language, title=term)
        session.wikipedia_hits = hits
        session.wikipedia_language = language
    rows = [
        [_button(hit.title, f"wk:{token}:a:{index}")]
        for index, hit in enumerate(hits)
    ]
    rows.extend(_language_rows(token, language))
    rows.append([_back(token, locale)])
    title = _tr(locale, "Поиск Wikipedia", "Wikipedia search")
    term = _search_term(session.anime, language)
    visible = [
        f"<b>{html.escape(title)}</b>",
        _tr(
            locale,
            "Это найденные статьи, а не подтверждённые совпадения с аниме. "
            "Выберите подходящую статью вручную.",
            "These are search results, not verified anime matches. "
            "Please choose the correct article yourself.",
        ),
        f"{html.escape(LANG_LABELS[language])}: {html.escape(term)}",
    ]
    if hits:
        for index, hit in enumerate(hits[:7], start=1):
            if hit.snippet:
                visible.append(f"{index}. {html.escape(hit.title)} — {html.escape(hit.snippet)}")
    else:
        visible.append(_tr(
            locale,
            "Статей не найдено. Попробуйте другой язык.",
            "No articles found. Try another language.",
        ))
    await _edit_html(message, "\n\n".join(visible), _keyboard(rows), locale=locale)


async def _article(
    session: AnimeCardSession, *, user_id: int
) -> WikipediaArticle | None:
    if session.wikipedia_pageid is None or session.wikipedia_language is None:
        return None
    return await wikipedia_client().article(
        language=session.wikipedia_language,
        pageid=session.wikipedia_pageid,
        mode=get_user_wikipedia_mode(user_id),
    )


async def _chapters(
    message: Message, token: str, session: AnimeCardSession, *,
    locale: str, user_id: int, page: int = 0,
) -> None:
    article = await _article(session, user_id=user_id)
    if article is None:
        await _search_screen(message, token, session, locale=locale)
        return
    chapters = article.sections
    total_pages = max(1, (len(chapters) + SECTIONS_PER_PAGE - 1) // SECTIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    rows = [
        [_button(section.title, f"wk:{token}:r:{i}:0")]
        for i, section in enumerate(chapters)
        if page * SECTIONS_PER_PAGE <= i < (page + 1) * SECTIONS_PER_PAGE
    ]
    if total_pages > 1:
        nav = []
        if page:
            nav.append(_button("◀️", f"wk:{token}:l:{page - 1}"))
        nav.append(_button(f"{page + 1}/{total_pages}", f"wk:{token}:l:{page}"))
        if page + 1 < total_pages:
            nav.append(_button("▶️", f"wk:{token}:l:{page + 1}"))
        rows.append(nav)
    rows.append([
        _button(_tr(locale, "Другая статья", "Other article"), f"wk:{token}:search")
    ])
    rows.append([_back(token, locale)])
    mode = get_user_wikipedia_mode(user_id)
    label = _tr(locale, "Очищенный" if mode == "clean" else "Полный RAW",
                "Clean" if mode == "clean" else "Full RAW")
    intro = (
        f"<b>Wikipedia — {html.escape(article.title)}</b>\n\n"
        + _tr(locale, "Выберите раздел статьи.", "Choose an article section.")
        + f"\n{html.escape(LANG_LABELS[article.language])} · {label}\n"
        + _tr(
            locale,
            "Подразделы включены в родительские разделы.",
            "Subsections are included inside their parent sections.",
        )
        + f'\n\n<a href="{html.escape(article.url, quote=True)}">Wikipedia · '
        + _tr(locale, "Оригинальная статья", "Original article") + "</a>"
    )
    await _edit_html(message, intro, _keyboard(rows), locale=locale)


def _chapter_page(
    article: WikipediaArticle, section_index: int, page: int, *,
    token: str, locale: str,
) -> tuple[str, InlineKeyboardMarkup]:
    if not 0 <= section_index < len(article.sections):
        raise ValueError("Unknown Wikipedia section")
    section = article.sections[section_index]
    pages = paginate_section(section)
    page = max(0, min(page, len(pages) - 1))
    # Page count is part of the displayed text, not only a keyboard control.
    heading = (
        f"<b>{html.escape(section.title)} — {page + 1}/{len(pages)}</b>"
    )
    attribution = (
        f'\n\n<a href="{html.escape(article.url, quote=True)}">'
        "Wikipedia</a> · "
        '<a href="https://creativecommons.org/licenses/by-sa/4.0/">'
        "CC BY-SA 4.0</a>"
    )
    rows = []
    if len(pages) > 1:
        nav = []
        if page:
            nav.append(_button("◀️", f"wk:{token}:r:{section_index}:{page - 1}"))
        nav.append(_button(f"{page + 1}/{len(pages)}", f"wk:{token}:r:{section_index}:{page}"))
        if page + 1 < len(pages):
            nav.append(_button("▶️", f"wk:{token}:r:{section_index}:{page + 1}"))
        rows.append(nav)
    rows.append([
        _button(_tr(locale, "К разделам", "Sections"), f"wk:{token}:l:0"),
        _back(token, locale),
    ])
    return heading + "\n\n" + pages[page] + attribution, _keyboard(rows)


async def edit_wikipedia_home(
    message: Message, token: str, session: AnimeCardSession, *,
    locale: str, user_id: int,
) -> None:
    try:
        if session.wikipedia_pageid is None:
            await _search_screen(message, token, session, locale=locale)
        else:
            await _chapters(message, token, session, locale=locale, user_id=user_id)
    except Exception:
        logging.exception("Wikipedia module could not load article/search")
        await _edit_html(
            message,
            _tr(
                locale,
                "Wikipedia сейчас недоступна. Попробуйте позже или выберите другой язык.",
                "Wikipedia is unavailable. Retry later or choose another language.",
            ),
            _keyboard(_language_rows(
                token, session.wikipedia_language or wiki_language(locale)
            ) + [[_back(token, locale)]]),
            locale=locale,
        )


def register_wikipedia_handlers(dp: Dispatcher, state: TelegramCardState) -> None:
    @dp.callback_query(F.data.startswith("wk:"))
    async def wikipedia_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        parts = str(callback.data or "").split(":")
        if len(parts) < 3:
            await callback.answer(_tr(locale, "Неверная кнопка", "Invalid action"), show_alert=True)
            return
        token = parts[1]
        session = state.anime_sessions.get(token)
        if session is None or session.owner_user_id != callback.from_user.id:
            await callback.answer(_tr(locale, "Сессия недоступна", "Session unavailable"), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return
        action = parts[2]
        try:
            session.main_view_active = False
            if action == "lang" and len(parts) == 4 and parts[3] in LANGUAGES:
                session.wikipedia_language = parts[3]
                session.wikipedia_pageid = None
                session.wikipedia_hits = ()
                await callback.answer()
                await _search_screen(callback.message, token, session, locale=locale)
            elif action == "search" and len(parts) == 3:
                session.wikipedia_pageid = None
                await callback.answer()
                await _search_screen(callback.message, token, session, locale=locale)
            elif action == "a" and len(parts) == 4:
                index = int(parts[3])
                if not 0 <= index < len(session.wikipedia_hits):
                    raise ValueError("Invalid Wikipedia search result")
                hit = session.wikipedia_hits[index]
                if not isinstance(hit, WikipediaHit):
                    raise ValueError("Invalid Wikipedia result")
                session.wikipedia_pageid = hit.pageid
                session.wikipedia_language = hit.language
                await callback.answer()
                await _chapters(
                    callback.message, token, session,
                    locale=locale, user_id=callback.from_user.id,
                )
            elif action == "l" and len(parts) == 4:
                await callback.answer()
                await _chapters(
                    callback.message, token, session,
                    locale=locale, user_id=callback.from_user.id, page=int(parts[3]),
                )
            elif action == "r" and len(parts) == 5:
                article = await _article(session, user_id=callback.from_user.id)
                if article is None:
                    raise ValueError("No selected Wikipedia article")
                body, keyboard = _chapter_page(
                    article, int(parts[3]), int(parts[4]), token=token, locale=locale,
                )
                await callback.answer()
                await _edit_html(callback.message, body, keyboard, locale=locale)
            else:
                raise ValueError("Invalid Wikipedia action")
        except (ValueError, IndexError):
            await callback.answer(
                _tr(locale, "Неверный или устаревший раздел", "Invalid or expired section"),
                show_alert=True,
            )
        except Exception:
            logging.exception("Wikipedia callback load failed")
            await _edit_html(
                callback.message,
                _tr(locale, "Не удалось загрузить Wikipedia. Повторите попытку.",
                    "Could not load Wikipedia. Please retry."),
                _keyboard([[_button(
                    _tr(locale, "К разделам", "Sections"), f"wk:{token}:l:0"
                ), _back(token, locale)]]),
                locale=locale,
            )
