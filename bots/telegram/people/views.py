from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

from bots.telegram.card.text_expansion import record_pages, records_rich_html
from bots.telegram.callbacks import SectionSourceCallback, ViewCallback
from bots.telegram.pagination import card_picker_callback
from bots.telegram.card.ui.constants import ALL_SOURCE_LABELS
from bots.telegram.card.views.renderer import plain_text_to_rich_html
from bots.telegram.card.ui.selectors import source_options
from core.canonicalization import display_person_name, localize_role_token
from core.models import AggregatedAnime, PersonCredit
from core.people import (
    PEOPLE_SECTIONS,
    all_people as _all_people,
    dedupe_strings as _dedupe,
    first_people_page as _first_people_page,
    people_for_scope as _people_for_scope,
    people_state as _people_state,
    portrait_source_names as _portrait_source_names,
    portrait_urls as _portrait_urls,
    section_mapping as _section_mapping,
    sources_with_portraits as _sources_with_portraits,
)
from localization import text


def _localized_roles(person: PersonCredit, locale: str) -> str:
    roles = [
        localize_role_token(role, locale=locale)
        for role in person.roles
        if localize_role_token(role, locale=locale)
    ]
    return ", ".join(roles) or text("common", "role_not_specified", locale=locale)


def _person_line(person: PersonCredit, locale: str) -> str:
    return " ".join(
        f"• {display_person_name(person.name, person.aliases, locale=locale)} — {_localized_roles(person, locale)}".split()
    )


def staff_gallery(
    anime: AggregatedAnime,
    source: str | None,
    locale: str,
    indices: dict[str | None, list[int]] | None = None,
) -> tuple[list[list[str]], list[tuple[int, PersonCredit]]]:
    """Classify by known photo availability, not current batch state."""
    people = _people_for_scope(anime, "staff", source)
    available = (
        set(indices.get(source, []))
        if indices is not None
        else {
            i
            for i, p in enumerate(people)
            if _portrait_urls(p, source=source, source_order=anime.sources)
        }
    )
    heading = (
        text("people", "staff", locale=locale)
        + (f" — {source}" if source else "")
        + ":"
    )
    groups = record_pages(
        [_person_line(p, locale) for i, p in enumerate(people) if i not in available],
        heading=heading,
    )
    return groups, [(i + 1, p) for i, p in enumerate(people) if i in available]


def staff_gallery_ordinal(
    groups: list[list[str]], portraits: list[tuple[int, PersonCredit]], page: int
) -> int:
    index = page - len(groups) if groups else page - 1
    return portraits[index][0] if 0 <= index < len(portraits) else 0


def _page_text(
    anime: AggregatedAnime,
    section: str,
    source: str | None,
    page: int,
    *,
    locale: str,
    total_hint: int | None = None,
    staff_indices: dict[str | None, list[int]] | None = None,
) -> tuple[str, list[str], int, int, int]:
    if section == "staff":
        groups, portraits = staff_gallery(anime, source, locale, staff_indices)
        total = len(_people_for_scope(anime, section, source))
        title_value = text("people", section, locale=locale) + (
            f" — {source}" if source else ""
        )
        if not groups and not portraits:
            return (
                title_value + ":\n\n" + text("people", "none", locale=locale),
                [],
                0,
                0,
                0,
            )
        first = 0 if groups else 1
        maximum = len(portraits) + max(0, len(groups) - 1)
        page = max(first, min(int(page), maximum))
        if page < len(groups):
            return (
                "\n".join([title_value + ":", "", *groups[page]]),
                [],
                sum(len(g) for g in groups[: page + 1]),
                total,
                page,
            )
        portrait_index = page - len(groups) if groups else page - 1
        person = portraits[portrait_index][1]
        urls = _portrait_urls(person, source=source, source_order=anime.sources)
        lines = [
            title_value + ":",
            "",
            display_person_name(person.name, person.aliases, locale=locale),
            text(
                "people", "roles", locale=locale, roles=_localized_roles(person, locale)
            ),
        ]
        sources = _portrait_source_names(person, anime.sources)
        if sources:
            lines.append(
                text("people", "portraits", locale=locale, sources=", ".join(sources))
            )
        return (
            "\n".join(lines),
            urls,
            sum(len(g) for g in groups) + portrait_index + 1,
            total,
            page,
        )
    without, with_portraits = _people_state(anime, section, source, locale=locale)
    actual_total = len(without) + len(with_portraits)
    total = (
        actual_total if section == "voices" else max(actual_total, int(total_hint or 0))
    )
    title_value = text("people", section, locale=locale)
    if source:
        title_value += f" — {source}"
    groups = record_pages(
        [_person_line(person, locale) for person in without], heading=title_value + ":"
    )
    group_count = len(groups)
    first_page = _first_people_page(without, with_portraits)
    max_page = len(with_portraits) + max(0, group_count - 1)
    page = max(first_page, min(int(page), max_page))
    if not with_portraits and not groups:
        return (
            title_value + ":\n\n" + text("people", "none", locale=locale),
            [],
            0,
            total,
            page,
        )
    if page < group_count:
        lines = [title_value + ":", "", *groups[page]]
        shown = sum(len(group) for group in groups[: page + 1])
        return "\n".join(lines), [], shown, total, page

    portrait_index = page - group_count if group_count else page - 1
    person = with_portraits[portrait_index]
    portrait_sources = _portrait_source_names(person, anime.sources)
    urls = _portrait_urls(person, source=source, source_order=anime.sources)
    lines = [
        f"{title_value}:",
        "",
        display_person_name(person.name, person.aliases, locale=locale),
        text("people", "roles", locale=locale, roles=_localized_roles(person, locale)),
    ]
    if portrait_sources:
        lines.append(
            text(
                "people",
                "portraits",
                locale=locale,
                sources=", ".join(portrait_sources),
            )
        )
    return "\n".join(lines), urls, len(without) + portrait_index + 1, total, page


def _portrait_html(urls: list[str]) -> str:
    urls = _dedupe(urls)[:20]
    if not urls:
        return ""
    images = "".join(f'<img src="{html.escape(url, quote=True)}"/>' for url in urls)
    return images if len(urls) == 1 else f"<tg-slideshow>{images}</tg-slideshow>"


def _rich(text_value: str, urls: list[str]) -> InputRichMessage:
    return InputRichMessage(html=_portrait_html(urls) + records_rich_html(text_value))


def _page_callback(token: str, section: str, source_key: str | None, page: int) -> str:
    if source_key is None:
        return ViewCallback(token, section, page).encode()
    return SectionSourceCallback(token, section, source_key, page).encode()


def _source_rows(
    token: str,
    anime: AggregatedAnime,
    section: str,
    selected_source_key: str | None,
    *,
    locale: str,
    source_names: list[str] | None = None,
) -> list[list[InlineKeyboardButton]]:
    names = list(source_names or _sources_with_portraits(anime, section))
    sources = source_options(names)
    if not sources:
        return []
    all_label = text("card", ALL_SOURCE_LABELS[section], locale=locale)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✓ " + all_label) if selected_source_key is None else all_label,
                callback_data=f"ci:{token}:{section}",
            )
        ]
    ]
    buttons = [
        InlineKeyboardButton(
            text=("✓ " + source_name)
            if selected_source_key == source_key
            else source_name,
            callback_data=f"ci:{token}:ps~{section}~{source_key}",
        )
        for source_key, source_name in sources
    ]
    rows.extend(buttons[index : index + 2] for index in range(0, len(buttons), 2))
    return rows


def _keyboard(
    token: str,
    anime: AggregatedAnime,
    section: str,
    source_key: str | None,
    page: int,
    shown: int,
    total: int,
    max_page: int,
    *,
    locale: str,
    source_names: list[str] | None = None,
    has_more: bool = False,
    first_page: int = 0,
    group_pages: int | None = None,
) -> InlineKeyboardMarkup:
    nav: list[InlineKeyboardButton] = []
    if page > first_page:
        nav.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=_page_callback(token, section, source_key, page - 1),
            )
        )
    without_count = max(0, shown - page) if page > 0 else max(0, shown)
    group = int(without_count > 0) if group_pages is None else group_pages
    picker_route = (
        f"gx~av~{section}~{group}"
        if source_key is None
        else f"gx~as~{section}~{source_key}~{group}"
    )
    picker_total = max_page + int(bool(group))
    picker_index = page if group else max(0, page - 1)
    nav.append(
        InlineKeyboardButton(
            text=text(
                "people",
                "shown",
                locale=locale,
                shown=shown,
                total=total,
            ),
            callback_data=(
                card_picker_callback(token, picker_route, picker_index, picker_total)
                if picker_total > 1 and shown > 0
                else "noop"
            ),
        )
    )
    if page < max_page or has_more:
        nav.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=_page_callback(token, section, source_key, page + 1),
            )
        )
    rows: list[list[InlineKeyboardButton]] = [nav]
    rows.extend(
        _source_rows(
            token,
            anime,
            section,
            source_key,
            locale=locale,
            source_names=source_names,
        )
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=text("common", "back", locale=locale),
                callback_data=(
                    f"ci:{token}:ps~{section}~{source_key}"
                    if shown and source_key is not None
                    else f"ci:{token}:{section}"
                    if shown
                    else ViewCallback(token, "main", 0).encode()
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
