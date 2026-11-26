from __future__ import annotations

import html
import logging

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from bots.telegram.card.text_expansion import record_pages, records_rich_html
from bots.telegram.callbacks import KeyedPageCallback, resolve_key_or_legacy_index
from bots.telegram.rich_media import deliver_rich_media
from bots.telegram.pagination import card_picker_callback
from bots.telegram.card.views.renderer import (
    edit_standard_text,
    plain_text_to_rich_html,
)
from bots.telegram.card.runtime.state import AnimeCardSession
from core.canonicalization import display_person_name, person_identity_keys
from sources.runtime.catalog import source_key_for_display_name
from core.models import CharacterRecord
from core.credit_identity import (
    ambiguous_name_keys,
    matching_credit_index,
    shared_credit_id,
)
from core.media_urls import usable_image_url
from localization import text
from services.runtime.app_context import get_app_context


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = " ".join((value or "").split()).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _role_priority(role: str | None) -> int:
    return {"main": 3, "supporting": 2, "background": 1}.get((role or "").casefold(), 0)


def _merge_characters(
    groups: list[list[CharacterRecord]], *, portrait_match=None
) -> list[CharacterRecord]:
    merged: list[CharacterRecord] = []
    ambiguous = ambiguous_name_keys(groups)
    confirmed_ids: list[dict[str, str]] = []
    for group in groups:
        for character in group:
            names = _dedupe([character.name, *character.aliases])
            match_index = matching_credit_index(
                merged, character, ambiguous, portrait_match
            )
            if match_index is None:
                merged.append(
                    CharacterRecord(
                        name=character.name,
                        role=character.role,
                        aliases=list(character.aliases),
                        external_ids=dict(character.external_ids),
                        portraits={
                            source: [url for url in urls if usable_image_url(url)]
                            for source, urls in character.portraits.items()
                        },
                    )
                )
                confirmed_ids.append(dict(character.external_ids))
                continue

            existing = merged[match_index]
            if shared_credit_id(existing, character) and any(
                confirmed_ids[match_index].get(k) == v
                for k, v in character.external_ids.items()
            ):
                confirmed_ids[match_index].update(character.external_ids)
            all_names = _dedupe(
                [
                    existing.name,
                    *existing.aliases,
                    character.name,
                    *character.aliases,
                ]
            )
            existing.aliases = [
                value
                for value in all_names
                if value.casefold() != existing.name.casefold()
            ]
            if _role_priority(character.role) > _role_priority(existing.role):
                existing.role = character.role
            # Preserve observed source-local identifiers in this view so a
            # subsequent homonym cannot bypass the conflict check.
            existing.external_ids.update(character.external_ids)
            for source_name, urls in character.portraits.items():
                existing.portraits[source_name] = _dedupe(
                    [
                        *existing.portraits.get(source_name, []),
                        *(url for url in urls if usable_image_url(url)),
                    ]
                )
    for record, ids in zip(merged, confirmed_ids):
        record.external_ids = ids
    return merged


def _source_names(session: AnimeCardSession) -> list[str]:
    available = set(session.anime.characters_by_source)
    ordered = [source for source in session.anime.sources if source in available]
    ordered.extend(
        source for source in session.anime.characters_by_source if source not in ordered
    )
    return ordered


def _source_options(session: AnimeCardSession) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_name in _source_names(session):
        key = session.character_key_by_source.get(source_name)
        if not key:
            descriptor_key = source_key_for_display_name(source_name)
            key = str(descriptor_key) if descriptor_key is not None else ""
        key = key.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append((key, source_name))
    return result


def _source_name_for_selector(
    session: AnimeCardSession,
    selector: str | None,
) -> tuple[str | None, str | None]:
    if selector is None:
        return None, None
    options = _source_options(session)
    keys = [key for key, _ in options]
    key = resolve_key_or_legacy_index(selector, keys)
    return key, dict(options)[key]


def _characters(session: AnimeCardSession, source: str | None) -> list[CharacterRecord]:
    if source is not None:
        return session.anime.characters_by_source.get(source, [])
    if session.characters_loaded and session.anime.characters:
        return session.anime.characters
    groups = [
        session.anime.characters_by_source[source] for source in _source_names(session)
    ]
    return _merge_characters(groups)


def _portrait_urls(
    character: CharacterRecord, source: str | None, source_order: list[str]
) -> list[str]:
    if source is not None:
        return _dedupe(
            [
                url
                for url in character.portraits.get(source, [])
                if usable_image_url(url)
            ]
        )
    values: list[str] = []
    for source_name in source_order:
        values.extend(character.portraits.get(source_name, []))
    for source_name, urls in character.portraits.items():
        if source_name not in source_order:
            values.extend(urls)
    return _dedupe([url for url in values if usable_image_url(url)])


def _portrait_sources(character: CharacterRecord, source_order: list[str]) -> list[str]:
    available = {
        source
        for source, urls in character.portraits.items()
        if any(usable_image_url(url) for url in urls)
    }
    ordered = [source for source in source_order if source in available]
    ordered.extend(sorted(available - set(ordered)))
    return ordered


def _role_label(role: str | None, locale: str) -> str:
    if not role:
        return text("common", "role_not_specified", locale=locale)
    return text("character_role", role, locale=locale, default=role)


def _character_line(character: CharacterRecord, locale: str) -> str:
    name = display_person_name(character.name, character.aliases, locale=locale)
    return " ".join(f"• {name} — {_role_label(character.role, locale)}".split())


def _character_state(
    session: AnimeCardSession,
    source: str | None,
) -> tuple[list[CharacterRecord], list[CharacterRecord], list[CharacterRecord]]:
    source_order = _source_names(session)
    values = _characters(session, source)
    without: list[CharacterRecord] = []
    with_portraits: list[CharacterRecord] = []
    for character in values:
        if _portrait_urls(character, source, source_order):
            with_portraits.append(character)
        else:
            without.append(character)
    return values, without, with_portraits


def prefetch_character_portraits(
    message: Message,
    session: AnimeCardSession,
    source: str | None,
    *,
    page: int = 0,
    locale: str,
) -> None:
    from bots.telegram.card.media_prefetch import prefetch_gallery

    _, without, portraits = _character_state(session, source)
    heading = (
        text("characters", "title", locale=locale)
        + (f" — {source}" if source else "")
        + ":"
    )
    groups = record_pages(
        [_character_line(p, locale) for p in without], heading=heading
    )
    index = max(0, page - len(groups) if groups else page - 1)
    prefetch_gallery(
        message,
        session,
        "characters",
        source,
        [_portrait_urls(p, source, _source_names(session)) for p in portraits],
        index=index,
    )


def _first_character_page(
    without: list[CharacterRecord],
    with_portraits: list[CharacterRecord],
) -> int:
    if without or not with_portraits:
        return 0
    return 1


def _page_text(
    session: AnimeCardSession,
    source: str | None,
    page: int,
    *,
    locale: str,
) -> tuple[str, list[str], int, int, int]:
    source_order = _source_names(session)
    values, without, with_portraits = _character_state(session, source)

    total = len(values)
    title = text("characters", "title", locale=locale)
    if source:
        title += f" — {source}"

    groups = record_pages(
        [_character_line(character, locale) for character in without],
        heading=title + ":",
    )
    group_count = len(groups)
    max_page = len(with_portraits) + max(0, group_count - 1)
    page = max(_first_character_page(without, with_portraits), min(int(page), max_page))
    if not values:
        return (
            title + ":\n\n" + text("characters", "none", locale=locale),
            [],
            0,
            0,
            page,
        )
    if page < group_count:
        return (
            "\n".join([title + ":", "", *groups[page]]),
            [],
            sum(len(group) for group in groups[: page + 1]),
            total,
            page,
        )

    portrait_index = page - group_count if group_count else page - 1
    character = with_portraits[portrait_index]
    urls = _portrait_urls(character, source, source_order)
    portrait_sources = _portrait_sources(character, source_order)
    lines = [
        title + ":",
        "",
        display_person_name(character.name, character.aliases, locale=locale),
        text(
            "characters",
            "role",
            locale=locale,
            role=_role_label(character.role, locale),
        ),
    ]
    if portrait_sources:
        lines.append(
            text(
                "characters",
                "portraits",
                locale=locale,
                sources=", ".join(portrait_sources),
            )
        )
    return "\n".join(lines), urls, len(without) + portrait_index + 1, total, page


def _rich(text_value: str, urls: list[str]) -> InputRichMessage:
    urls = _dedupe(urls)[:20]
    images = "".join(f'<img src="{html.escape(url, quote=True)}"/>' for url in urls)
    media = images if len(urls) <= 1 else f"<tg-slideshow>{images}</tg-slideshow>"
    return InputRichMessage(html=media + records_rich_html(text_value))


def _callback(token: str, source_key: str | None, page: int) -> str:
    if source_key is None:
        return f"av:{token}:characters:{page}"
    return KeyedPageCallback("ac", token, source_key, page).encode()


def _keyboard(
    token: str,
    sources: list[tuple[str, str]],
    source_key: str | None,
    page: int,
    shown: int,
    total: int,
    max_page: int,
    *,
    locale: str,
    first_page: int = 0,
    group_pages: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > first_page:
        nav.append(
            InlineKeyboardButton(
                text="◀️", callback_data=_callback(token, source_key, page - 1)
            )
        )
    without_count = max(0, shown - page) if page > 0 else max(0, shown)
    group = int(without_count > 0) if group_pages is None else group_pages
    picker_route = (
        f"gx~av~characters~{group}"
        if source_key is None
        else f"gx~ac~{source_key}~{group}"
    )
    picker_total = max_page + int(bool(group))
    picker_index = page if group else max(0, page - 1)
    nav.append(
        InlineKeyboardButton(
            text=text("characters", "shown", locale=locale, shown=shown, total=total),
            callback_data=(
                card_picker_callback(token, picker_route, picker_index, picker_total)
                if picker_total > 1 and shown > 0
                else "noop"
            ),
        )
    )
    if page < max_page:
        nav.append(
            InlineKeyboardButton(
                text="▶️", callback_data=_callback(token, source_key, page + 1)
            )
        )
    rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text=("✓ " if source_key is None else "")
                + text("characters", "all", locale=locale),
                callback_data=f"ci:{token}:characters",
            )
        ]
    )
    buttons = [
        InlineKeyboardButton(
            text=("✓ " + source_name) if source_key == key else source_name,
            callback_data=f"ci:{token}:ac~{key}",
        )
        for key, source_name in sources
    ]
    rows.extend(buttons[index : index + 2] for index in range(0, len(buttons), 2))
    rows.append(
        [
            InlineKeyboardButton(
                text=text("common", "back", locale=locale),
                callback_data=(
                    f"ci:{token}:characters" if shown > 0 else f"av:{token}:main:0"
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ensure_loaded(
    message: Message, session: AnimeCardSession, *, locale: str
) -> None:
    if session.characters_loaded:
        return
    try:
        await message.edit_text(
            text=text("characters", "loading", locale=locale), reply_markup=None
        )
    except Exception:
        logging.debug("Could not show character loading state", exc_info=True)

    result = await get_app_context().character_service.load(session.anime.identity)
    session.anime.characters_by_source = result.by_source
    session.character_key_by_source = result.key_by_source
    groups = [
        result.by_source[source]
        for source in session.anime.sources
        if source in result.by_source
    ] + [
        values
        for source, values in result.by_source.items()
        if source not in session.anime.sources
    ]
    from bots.telegram.card.portrait_matching import portrait_matcher

    matches = await portrait_matcher(groups)
    session.anime.characters = _merge_characters(groups, portrait_match=matches)
    session.character_errors = result.errors
    session.characters_loaded = True


async def edit_characters_view(
    message: Message,
    token: str,
    session: AnimeCardSession,
    *,
    source_key: str | None,
    page: int,
    locale: str,
) -> None:
    try:
        await _ensure_loaded(message, session, locale=locale)
    except Exception:
        logging.exception("Could not load characters")
        session.characters_loaded = True
        session.character_errors = {"characters": "load failed"}

    source_key, source = _source_name_for_selector(session, source_key)
    source_options = _source_options(session)

    values = _characters(session, source)
    if not values:
        content = (
            text("characters", "load_failed", locale=locale)
            if session.character_errors
            else text("characters", "empty", locale=locale)
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=text("common", "back", locale=locale),
                        callback_data=f"av:{token}:main:0",
                    )
                ]
            ]
        )
    else:
        _, without, with_portraits = _character_state(session, source)
        heading = (
            text("characters", "title", locale=locale)
            + (f" — {source}" if source else "")
            + ":"
        )
        groups = record_pages(
            [_character_line(character, locale) for character in without],
            heading=heading,
        )
        max_page = len(with_portraits) + max(0, len(groups) - 1)
        first_page = _first_character_page(without, with_portraits)
        content, urls, shown, total, page = _page_text(
            session, source, page, locale=locale
        )
        prefetch_character_portraits(message, session, source, page=page, locale=locale)
        keyboard = _keyboard(
            token,
            source_options,
            source_key,
            page,
            shown,
            total,
            max_page,
            locale=locale,
            first_page=first_page,
            group_pages=len(groups),
        )
        try:
            await deliver_rich_media(
                lambda rich: message.edit_text(
                    rich_message=rich, reply_markup=keyboard
                ),
                _rich(content, urls),
                bot=getattr(message, "bot", None),
                verify_photo=True,
            )
            return
        except Exception:
            logging.exception("Rich character view edit failed; falling back to text")

    await edit_standard_text(
        message,
        content,
        reply_markup=keyboard,
        locale=locale,
    )
