"""An uncluttered Wikipedia reader attached to a card, outside aggregation.

The user's global /settings language selects Wikipedia. Search hits require an
explicit choice. The article is rendered as flowing encyclopedic prose with
major sections, linked text and direct previous/next section navigation.
"""

from __future__ import annotations

import html
import logging
import re

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from bots.telegram.card.runtime.state import AnimeCardSession, TelegramCardState
from bots.telegram.card.ui.constants import RICH_MESSAGE_LIMIT, STANDARD_MESSAGE_LIMIT
from bots.telegram.card.views.renderer import (
    edit_standard_text,
)
from bots.telegram.pagination import indexed_selection_rows, selection_page_size
from core.models import AggregatedAnime
from core.settings import get_settings
from services.users.settings import get_user_locale, get_user_wikipedia_mode
from sources.providers.wikipedia import (
    JAPANESE_KANA,
    WikipediaArticle,
    WikipediaHit,
    japanese_title_in_article,
    japanese_title_in_text,
    paginate_section,
    wiki_language,
    wikipedia_client,
)

SECTIONS_PER_PAGE = 10  # shared selector default


def _chapter_limit(*, plain: bool) -> int:
    native = PLAIN_CHAPTER_BUDGET if plain else RICH_CHAPTER_BUDGET
    return min(native, get_settings().telegram.long_text_chunk_size)


def _rich_wikipedia_html(body: str) -> str:
    """Render every page as flowing encyclopedia prose, never a synthetic layout.

    Keep source inline links/emphasis and paragraph spacing, independent of
    where a section is split into pages.
    """
    paragraphs = [
        f"<p>{paragraph.strip().replace(chr(10), '<br>')}</p>"
        for paragraph in body.split("\n\n")
        if paragraph.strip()
    ]
    # Rich-message renderers can collapse adjacent <p> margins. Make the
    # blank line explicit between the heading, prose paragraphs and the
    # final Wikipedia / CC BY-SA attribution.
    return "<p><br></p>".join(paragraphs)


def _plain_wikipedia_with_links(body: str) -> str:
    """Retain clickable/source URLs when rich HTML must fall back to sendMessage."""
    links = re.compile(r'<a href="([^"]+)">(.*?)</a>', re.DOTALL)
    with_links = links.sub(
        lambda match: (
            re.sub(r"<[^>]+>", "", match.group(2))
            + " ("
            + html.unescape(match.group(1))
            + ")"
        ),
        body,
    )
    return html.unescape(re.sub(r"<[^>]+>", "", with_links))


# Headers, source attribution and Telegram internals also consume characters.
RICH_CHAPTER_BUDGET = RICH_MESSAGE_LIMIT - 750
PLAIN_CHAPTER_BUDGET = STANDARD_MESSAGE_LIMIT - 450


def _tr(locale: str, ru: str, en: str) -> str:
    return ru if locale == "ru" else en


def _button(label: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=action)


def _back(token: str, locale: str) -> InlineKeyboardButton:
    return _button(_tr(locale, "К карточке", "Back to anime"), f"av:{token}:main:0")


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _search_term(anime: AggregatedAnime, language: str) -> str:
    titles = anime.titles
    keys = {
        "ru": ("russian", "romaji", "english"),
        "ja": ("japanese", "romaji", "english"),
    }.get(language, ("english", "romaji", "japanese", "russian"))
    for key in keys:
        candidate = str(titles.get(key) or "").strip()
        if len(candidate) >= 2:
            return candidate[:120]
    return str(anime.title).strip()[:120]


def _japanese_label(anime: AggregatedAnime, hit: WikipediaHit) -> str:
    """No invented Japanese aliases: use one only when the hit mentions it."""
    title = hit.title
    japanese = str(anime.titles.get("japanese") or "").strip()
    if (
        japanese
        and JAPANESE_KANA.search(japanese)
        and japanese not in title
        and japanese_title_in_text((hit.title, hit.snippet), japanese)
    ):
        return f"{title} ({japanese})"
    return title


def _japanese_hit(hit: WikipediaHit) -> bool:
    """Reject results whose Wikipedia title/snippet lacks Japanese characters."""
    return bool(JAPANESE_KANA.search(f"{hit.title} {hit.snippet}"))


def _reset_for_locale(session: AnimeCardSession, locale: str) -> str:
    """Changing the global language invalidates a previous wiki page choice."""
    language = wiki_language(locale)
    if session.wikipedia_language != language:
        session.wikipedia_language = language
        session.wikipedia_pageid = None
        session.wikipedia_hits = ()
    return language


async def _edit_html(
    message: Message,
    value: str,
    keyboard: InlineKeyboardMarkup,
    *,
    locale: str,
) -> None:
    """Use standard Telegram HTML for short menus, with a plain fallback."""
    try:
        await message.edit_text(
            value,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
        plain = _plain_wikipedia_with_links(value)
        await edit_standard_text(
            message,
            plain,
            reply_markup=keyboard,
            locale=locale,
        )


async def _search_screen(
    message: Message,
    token: str,
    session: AnimeCardSession,
    *,
    locale: str,
    page: int = 0,
) -> None:
    language = _reset_for_locale(session, locale)
    hits = tuple(hit for hit in session.wikipedia_hits if isinstance(hit, WikipediaHit))
    if not hits:
        term = _search_term(session.anime, language)
        found = await wikipedia_client().search(language=language, title=term)
        hits = tuple(hit for hit in found if _japanese_hit(hit))
        if not hits:
            # Also filter Japanese-title retry; never display a generic hit.
            japanese = str(session.anime.titles.get("japanese") or "").strip()
            if JAPANESE_KANA.search(japanese) and japanese != term:
                found = await wikipedia_client().search(
                    language=language,
                    title=japanese,
                )
                hits = tuple(hit for hit in found if _japanese_hit(hit))
        session.wikipedia_hits = hits
    rows, _, _ = indexed_selection_rows(
        [_japanese_label(session.anime, hit) for hit in hits],
        window=page,
        block="wikipedia",
        destination=lambda index: f"wk:{token}:a:{index}",
        change_window=lambda target: f"wk:{token}:s:{target}",
    )
    rows.append([_back(token, locale)])
    header = _tr(
        locale,
        "<b>Wikipedia</b>\nВыберите статью:",
        "<b>Wikipedia</b>\nChoose an article:",
    )
    if not hits:
        header += "\n\n" + _tr(
            locale,
            "Статья не найдена. Можно изменить язык в /settings.",
            "No article found. Change your language in /settings if needed.",
        )
    await _edit_html(message, header, _keyboard(rows), locale=locale)


async def _article(
    session: AnimeCardSession,
    *,
    user_id: int,
    locale: str,
) -> WikipediaArticle | None:
    language = _reset_for_locale(session, locale)
    if session.wikipedia_pageid is None:
        return None
    return await wikipedia_client().article(
        language=language,
        pageid=session.wikipedia_pageid,
        mode=get_user_wikipedia_mode(user_id),
    )


async def _chapters(
    message: Message,
    token: str,
    session: AnimeCardSession,
    *,
    locale: str,
    user_id: int,
    page: int = 0,
) -> None:
    article = await _article(session, user_id=user_id, locale=locale)
    if article is None:
        await _search_screen(message, token, session, locale=locale)
        return
    sections = article.sections
    rows, page, _ = indexed_selection_rows(
        [section.title for section in sections],
        window=page,
        block="wikipedia",
        destination=lambda index: f"wk:{token}:r:{index}:0",
        change_window=lambda target: f"wk:{token}:l:{target}",
    )
    rows.append(
        [
            _button(
                _tr(locale, "Другие статьи", "Other articles"), f"wk:{token}:search"
            ),
            _back(token, locale),
        ]
    )
    japanese_title = japanese_title_in_article(
        article, session.anime.titles.get("japanese")
    )
    title = f"<b>Wikipedia — {html.escape(article.title)}</b>"
    if japanese_title and japanese_title not in article.title:
        title += f"\n{html.escape(japanese_title)}"
    title += "\n\n<b>" + _tr(locale, "Оглавление", "Contents") + "</b>"
    title += "\n\n" + _tr(locale, "Выберите раздел:", "Choose a section:")
    await _edit_html(message, title, _keyboard(rows), locale=locale)


def _chapter_page(
    article: WikipediaArticle,
    section_index: int,
    page: int,
    *,
    token: str,
    locale: str,
    limit: int = RICH_CHAPTER_BUDGET,
) -> tuple[str, InlineKeyboardMarkup]:
    if not 0 <= section_index < len(article.sections):
        raise ValueError("Unknown Wikipedia section")
    chapter = article.sections[section_index]
    pages = paginate_section(chapter, limit=limit)
    page = max(0, min(page, len(pages) - 1))
    heading = f"<b>{html.escape(chapter.title)}</b>"
    attribution = (
        f'\n\n<a href="{html.escape(article.url, quote=True)}">Wikipedia</a>'
        ' · <a href="https://creativecommons.org/licenses/by-sa/4.0/">'
        "CC BY-SA 4.0</a>"
    )
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    # No within-section pager at all when content fits the message budget.
    if len(pages) > 1:
        page_navigation = []
        if page:
            page_navigation.append(
                _button(
                    "◀️",
                    f"wk:{token}:r:{section_index}:{page - 1}",
                )
            )
        page_navigation.append(
            _button(
                f"{page + 1}/{len(pages)}",
                f"wk:{token}:p:{section_index}:{page // selection_page_size('wikipedia')}",
            )
        )
        if page + 1 < len(pages):
            page_navigation.append(
                _button(
                    "▶️",
                    f"wk:{token}:r:{section_index}:{page + 1}",
                )
            )
        keyboard_rows.append(page_navigation)
    # Cross-chapter navigation works from every chapter, even when it is
    # short enough to fit in one message.
    chapter_navigation = []
    if section_index:
        chapter_navigation.append(
            _button(
                "◀️",
                f"wk:{token}:r:{section_index - 1}:0",
            )
        )
    # The chapter counter opens the first page of the section index.
    # The separate within-chapter page counter keeps its own "p" route.
    chapter_navigation.append(
        _button(f"{section_index + 1}/{len(article.sections)}", f"wk:{token}:l:0")
    )
    if section_index + 1 < len(article.sections):
        chapter_navigation.append(
            _button(
                "▶️",
                f"wk:{token}:r:{section_index + 1}:0",
            )
        )
    keyboard_rows.append(chapter_navigation)
    keyboard_rows.append(
        [
            _button(
                _tr(locale, "К разделам", "All sections"),
                f"wk:{token}:l:{section_index // selection_page_size('wikipedia')}",
            ),
            _back(token, locale),
        ]
    )
    return heading + "\n\n" + pages[page] + attribution, _keyboard(keyboard_rows)


async def _chapter_pages_index(
    message: Message,
    token: str,
    article: WikipediaArticle,
    *,
    section_index: int,
    window: int,
    locale: str,
    limit: int,
) -> None:
    if not 0 <= section_index < len(article.sections):
        raise ValueError("Unknown Wikipedia section")
    pages = paginate_section(article.sections[section_index], limit=limit)
    rows, _, _ = indexed_selection_rows(
        [
            f"Страница {index + 1}" if locale == "ru" else f"Page {index + 1}"
            for index in range(len(pages))
        ],
        window=window,
        block="wikipedia",
        destination=lambda index: f"wk:{token}:r:{section_index}:{index}",
        change_window=lambda target: f"wk:{token}:p:{section_index}:{target}",
    )
    rows.append(
        [
            _button(
                _tr(locale, "К разделам", "All sections"),
                f"wk:{token}:l:{section_index // selection_page_size('wikipedia')}",
            ),
            _back(token, locale),
        ]
    )
    await _edit_html(
        message,
        f"<b>{html.escape(article.sections[section_index].title)}</b>\n\n"
        + _tr(locale, "Выберите страницу:", "Choose a page:"),
        _keyboard(rows),
        locale=locale,
    )


async def _render_chapter(
    message: Message,
    token: str,
    session: AnimeCardSession,
    article: WikipediaArticle,
    *,
    section_index: int,
    page: int,
    locale: str,
) -> None:
    # Prefer sendRichMessage/edit rich text for long Wikipedia paragraphs.
    # If the Telegram API server rejects rich text, remember the fallback
    # and paginate at the sendMessage limit only then.
    plain_mode = session.wikipedia_plain_fallback
    limit = _chapter_limit(plain=plain_mode)
    body, keyboard = _chapter_page(
        article,
        section_index,
        page,
        token=token,
        locale=locale,
        limit=limit,
    )
    if not plain_mode:
        try:
            await message.edit_text(
                rich_message=InputRichMessage(html=_rich_wikipedia_html(body)),
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return
            logging.warning(
                "Wikipedia rich-message edit rejected, using standard HTML: %s",
                type(exc).__name__,
            )
            session.wikipedia_plain_fallback = True
            body, keyboard = _chapter_page(
                article,
                section_index,
                page,
                token=token,
                locale=locale,
                limit=_chapter_limit(plain=True),
            )
    await _edit_html(message, body, keyboard, locale=locale)


async def edit_wikipedia_home(
    message: Message,
    token: str,
    session: AnimeCardSession,
    *,
    locale: str,
    user_id: int,
) -> None:
    try:
        _reset_for_locale(session, locale)
        if session.wikipedia_pageid is None:
            await _search_screen(message, token, session, locale=locale)
        else:
            await _chapters(
                message,
                token,
                session,
                locale=locale,
                user_id=user_id,
            )
    except Exception:
        logging.exception("Wikipedia module could not load article/search")
        await _edit_html(
            message,
            _tr(
                locale,
                "Не удалось загрузить Wikipedia. Попробуйте ещё раз.",
                "Wikipedia could not be loaded. Please retry.",
            ),
            _keyboard(
                [
                    [
                        _button(
                            _tr(locale, "Повторить", "Retry"), f"av:{token}:wikipedia:0"
                        )
                    ],
                    [_back(token, locale)],
                ]
            ),
            locale=locale,
        )


def register_wikipedia_handlers(dp: Dispatcher, state: TelegramCardState) -> None:
    @dp.callback_query(F.data.startswith("wk:"))
    async def wikipedia_callback(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        parts = str(callback.data or "").split(":")
        if len(parts) < 3:
            await callback.answer(
                _tr(locale, "Неверная кнопка", "Invalid action"),
                show_alert=True,
            )
            return
        token = parts[1]
        session = state.anime_sessions.get(token)
        if session is None or session.owner_user_id != callback.from_user.id:
            await callback.answer(
                _tr(locale, "Сессия недоступна", "Session unavailable"),
                show_alert=True,
            )
            return
        if callback.message is None:
            await callback.answer()
            return
        action = parts[2]
        try:
            session.main_view_active = False
            _reset_for_locale(session, locale)
            if action == "search" and len(parts) == 3:
                session.wikipedia_pageid = None
                session.wikipedia_hits = ()
                await callback.answer()
                await _search_screen(
                    callback.message,
                    token,
                    session,
                    locale=locale,
                )
            elif action == "s" and len(parts) == 4:
                await callback.answer()
                await _search_screen(
                    callback.message,
                    token,
                    session,
                    locale=locale,
                    page=int(parts[3]),
                )
            elif action == "a" and len(parts) == 4:
                index = int(parts[3])
                if not 0 <= index < len(session.wikipedia_hits):
                    raise ValueError("Invalid Wikipedia search result")
                hit = session.wikipedia_hits[index]
                if not isinstance(hit, WikipediaHit):
                    raise ValueError("Invalid Wikipedia result")
                if hit.language != session.wikipedia_language:
                    raise ValueError("Wikipedia search result language expired")
                session.wikipedia_pageid = hit.pageid
                await callback.answer()
                await _chapters(
                    callback.message,
                    token,
                    session,
                    locale=locale,
                    user_id=callback.from_user.id,
                )
            elif action == "l" and len(parts) == 4:
                await callback.answer()
                await _chapters(
                    callback.message,
                    token,
                    session,
                    locale=locale,
                    user_id=callback.from_user.id,
                    page=int(parts[3]),
                )
            elif action == "p" and len(parts) == 5:
                article = await _article(
                    session,
                    user_id=callback.from_user.id,
                    locale=locale,
                )
                if article is None:
                    raise ValueError("Wikipedia article expired")
                await callback.answer()
                await _chapter_pages_index(
                    callback.message,
                    token,
                    article,
                    section_index=int(parts[3]),
                    window=int(parts[4]),
                    locale=locale,
                    limit=(_chapter_limit(plain=session.wikipedia_plain_fallback)),
                )
            elif action == "r" and len(parts) == 5:
                article = await _article(
                    session,
                    user_id=callback.from_user.id,
                    locale=locale,
                )
                if article is None:
                    await callback.answer(
                        _tr(
                            locale,
                            "Сменился язык бота. Откройте Wikipedia снова.",
                            "Bot language changed. Reopen Wikipedia.",
                        ),
                        show_alert=True,
                    )
                    return
                await callback.answer()
                await _render_chapter(
                    callback.message,
                    token,
                    session,
                    article,
                    section_index=int(parts[3]),
                    page=int(parts[4]),
                    locale=locale,
                )
            else:
                raise ValueError("Invalid Wikipedia action")
        except (ValueError, IndexError):
            await callback.answer(
                _tr(
                    locale,
                    "Неверный или устаревший раздел",
                    "Invalid or expired section",
                ),
                show_alert=True,
            )
        except Exception:
            logging.exception("Wikipedia callback load failed")
            await _edit_html(
                callback.message,
                _tr(
                    locale,
                    "Не удалось загрузить Wikipedia. Повторите попытку.",
                    "Could not load Wikipedia. Please retry.",
                ),
                _keyboard(
                    [
                        [
                            _button(
                                _tr(locale, "К разделам", "All sections"),
                                f"wk:{token}:l:0",
                            )
                        ],
                        [_back(token, locale)],
                    ]
                ),
                locale=locale,
            )
