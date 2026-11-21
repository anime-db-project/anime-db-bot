"""A single source of list labels and lazy entry preparation for card pickers.

The picker never fetches secrets, creates anime identities or bypasses an
existing source selector. It reads already loaded state; only the first entry
into a lazy section initiates its normal service-level loading.
"""
from __future__ import annotations

from aiogram.types import Message

from bots.telegram.card.runtime.state import AnimeCardSession, TelegramCardState
from core.canonicalization import display_person_name
from core.people import people_state
from localization import text
from services.runtime.app_context import get_app_context


def picker_parent_callback(token: str, route: str) -> str:
    """Return to the meaningful parent when leaving a source-specific picker."""
    parts = route.split("~")
    kind = parts[0]
    if kind == "ar" and len(parts) == 3 and parts[1] in {"related", "recommendations"}:
        # The all-sources selector is itself the entry screen: never return to
        # the same callback and trigger Telegram's "message is not modified".
        return (
            f"av:{token}:main:0"
            if parts[2] == "all"
            else f"ci:{token}:{parts[1]}"
        )
    if kind == "ap":
        return f"av:{token}:main:0"
    if kind == "apg":
        return f"ci:{token}:episodes"
    if kind == "ac":
        return f"ci:{token}:characters"
    if kind == "gx" and len(parts) == 5 and parts[1] == "as":
        return f"ci:{token}:{parts[2]}"
    if kind == "gx" and len(parts) == 4 and parts[1] == "ac":
        return f"ci:{token}:characters"
    if kind == "as" and len(parts) == 3 and parts[1] in {
        "screenshots", "titles", "popularity"
    }:
        return f"ci:{token}:{parts[1]}"
    if kind == "ad":
        return f"av:{token}:description:0"
    if kind == "at":
        return f"av:{token}:trailers:0"
    return f"av:{token}:main:0"


def source_character_choices(
    session: AnimeCardSession,
    selector: str,
    *,
    locale: str,
) -> tuple[str, list[str]] | None:
    """Use the same grouped portrait navigation for source-specific characters.

    The old numbered source picker counted every unpictured character as an
    individual page although all of them share page 0. Resolve the source
    first, then use the same grouped route as the main character selector.
    """
    from bots.telegram.card.features.characters import (
        _character_state,
        _source_name_for_selector,
    )

    try:
        source_key, source_name = _source_name_for_selector(session, selector)
    except ValueError:
        return None
    if source_key is None or source_name is None:
        return None
    _, without, _ = _character_state(session, source_name)
    route = f"gx~ac~{source_key}~{int(bool(without))}"
    labels = labels_for_route(session, route, locale=locale)
    return (route, labels) if labels else None


def _episode_labels(
    session: AnimeCardSession, backend: str, locale: str
) -> list[str]:
    from bots.telegram.card.features.episodes import _episode_title

    catalog = session.episode_catalogs.get(backend, [])
    labels: list[str] = []
    for index, item in enumerate(catalog):
        ordinal = item.number if isinstance(item.number, int) and item.number > 0 else index + 1
        label = f"{ordinal} серия" if locale == "ru" else f"Episode {ordinal}"
        title = _episode_title(item, locale)
        if title and title != text("card", "episode_untitled", locale=locale):
            label += f" — {title}"
        labels.append(label)
    return labels


def labels_for_route(
    session: AnimeCardSession,
    route: str,
    *,
    locale: str,
) -> list[str]:
    """Return grounded item names for the existing compact callback route."""
    parts = route.split("~")
    kind = parts[0]
    if kind in {"ap", "apg"} and len(parts) >= 2:
        labels = _episode_labels(session, parts[1], locale)
        if kind == "apg" and len(parts) == 3:
            try:
                offset = max(0, int(parts[2]))
            except ValueError:
                return []
            return labels[offset:]
        return labels

    if kind == "av" and len(parts) == 2 and parts[1] == "all_ids":
        from bots.telegram.card.features.mapping_ids import mapping_ids_pages

        resolver = get_app_context().identity_resolver.mapping_resolver
        source_ids = (
            resolver.ids_for_anchor(session.anime.amdb_id)
            if session.anime.amdb_id
            else {}
        )
        return [
            f"ID источников — часть {i + 1}"
            if locale == "ru"
            else f"Source IDs — part {i + 1}"
            for i, _ in enumerate(
                mapping_ids_pages(session.anime.amdb_id or "", source_ids, locale=locale)
            )
        ]

    if kind == "as" and len(parts) == 3 and parts[1] in {"titles", "popularity"}:
        from bots.telegram.card.ui.selectors import source_section_options
        from bots.telegram.card.views.sections import source_section_pages

        source = dict(source_section_options(
            session.anime, parts[1]
        )).get(parts[2])
        if source is None:
            return []
        return [
            f"Страница {i + 1}" if locale == "ru" else f"Page {i + 1}"
            for i, _ in enumerate(
                source_section_pages(session.anime, parts[1], source, locale=locale)
            )
        ]

    if kind == "ad" and len(parts) == 2:
        from bots.telegram.card.ui.selectors import description_source_options
        from bots.telegram.card.views.sections import description_pages

        source = dict(description_source_options(session.anime)).get(parts[1])
        if source is None:
            return []
        return [
            f"Описание — часть {i + 1}" if locale == "ru" else f"Description — part {i + 1}"
            for i, _ in enumerate(description_pages(session.anime, source, locale=locale))
        ]

    if kind == "ar" and len(parts) == 3:
        from bots.telegram.card.features.related import _display_title, _items

        return [
            _display_title(item, locale)
            for item in _items(session, parts[1], parts[2])
        ]

    if kind == "at" and len(parts) == 2:
        from bots.telegram.card.features.trailers import (
            _source_name_for_selector,
            _trailer_title,
        )

        try:
            _, source = _source_name_for_selector(session, parts[1])
        except ValueError:
            return []
        return [
            _trailer_title(item, index + 1, locale)
            for index, item in enumerate(session.trailers_by_source.get(source, []))
        ]

    if kind == "gx" and len(parts) in {4, 5}:
        from bots.telegram.card.ui.selectors import source_section_options

        if parts[1] == "ac" or (
            parts[1] == "av" and parts[2] == "characters"
        ):
            from bots.telegram.card.features.characters import (
                _character_state,
                _source_name_for_selector,
            )

            selector = parts[2] if parts[1] == "ac" else None
            try:
                _, source = _source_name_for_selector(session, selector)
            except ValueError:
                return []
            _, without, portraits = _character_state(session, source)
        elif parts[2] in {"staff", "voices"}:
            source = None
            if parts[1] == "as" and len(parts) == 5:
                source = dict(source_section_options(
                    session.anime, parts[2]
                )).get(parts[3])
            without, portraits = people_state(session.anime, parts[2], source)
        else:
            return []
        group_name = (
            f"Без портретов ({len(without)})"
            if locale == "ru"
            else f"Without portraits ({len(without)})"
        )
        return (
            ([group_name] if without else [])
            + [
                display_person_name(item.name, item.aliases, locale=locale)
                for item in portraits
            ]
        )

    if kind in {"ac", "pv"}:
        from bots.telegram.card.features.characters import (
            _character_state,
            _source_name_for_selector,
        )

        character_scope = kind == "ac" or (
            kind == "pv" and len(parts) >= 3
            and (parts[2] == "characters")
        )
        if character_scope:
            selector = parts[1] if kind == "ac" else (
                parts[3] if len(parts) == 5 else None
            )
            try:
                _, source = _source_name_for_selector(session, selector)
            except ValueError:
                return []
            _, without, portraits = _character_state(session, source)
            return [
                display_person_name(item.name, item.aliases, locale=locale)
                for item in [*without, *portraits]
            ]

    if kind == "pv" and len(parts) >= 4 and parts[2] in {"staff", "voices"}:
        from bots.telegram.card.ui.selectors import source_section_options

        section = parts[2]
        source = None
        if len(parts) == 5:
            source = dict(source_section_options(
                session.anime, section
            )).get(parts[3])
        without, portraits = people_state(session.anime, section, source)
        return [
            display_person_name(item.name, item.aliases, locale=locale)
            for item in [*without, *portraits]
        ]

    section = None
    source = None
    if kind == "av" and len(parts) == 2:
        section = parts[1]
    elif kind == "as" and len(parts) == 3:
        from bots.telegram.card.ui.selectors import source_section_options

        section = parts[1]
        source = dict(source_section_options(
            session.anime, section
        )).get(parts[2])
    if section == "screenshots":
        from bots.telegram.card.features.screenshots import (
            SCREENSHOTS_PER_PAGE,
            screenshot_urls,
        )

        count = len(screenshot_urls(session.anime, source))
        return [
            (
                f"Скриншоты {index + 1}–{min(index + SCREENSHOTS_PER_PAGE, count)}"
                if locale == "ru"
                else f"Screenshots {index + 1}–{min(index + SCREENSHOTS_PER_PAGE, count)}"
            )
            for index in range(0, count, SCREENSHOTS_PER_PAGE)
        ]

    return []


async def prepare_initial_picker(
    state: TelegramCardState,
    token: str,
    session: AnimeCardSession,
    section: str,
    message: Message,
    *,
    locale: str,
) -> tuple[str, list[str]] | None:
    """Build an item chooser before showing a detail page for large sections."""
    if section.startswith("ps~"):
        from bots.telegram.card.ui.selectors import source_options
        from bots.telegram.people.loading import (
            ensure_lazy_people,
            progress_key,
            source_names,
        )

        parts = section.split("~")
        if len(parts) != 3 or parts[1] not in {"staff", "voices"}:
            return None
        people_section, source_key = parts[1:]
        progress = state.people_loads.get(progress_key(token, people_section))
        available = dict(source_options(
            source_names(session.anime, people_section, progress)
        ))
        source_name = available.get(source_key)
        if source_name is None:
            return None
        await ensure_lazy_people(
            state,
            token,
            session.anime,
            message,
            people_section,
            locale=locale,
            source=source_name,
            requested_page=0,
        )
        without, _ = people_state(session.anime, people_section, source_name)
        route = f"gx~as~{people_section}~{source_key}~{int(bool(without))}"
        labels = labels_for_route(session, route, locale=locale)
        return (route, labels) if labels else None

    if section in {"related", "recommendations"}:
        from bots.telegram.card.features.related import ensure_related_loaded

        await ensure_related_loaded(session, section)
        route = f"ar~{section}~all"
        labels = labels_for_route(session, route, locale=locale)
        return (route, labels) if labels else None

    if section == "episodes":
        context = get_app_context()
        service = context.episode_service
        backends = service.available_backends(session.anime.identity)
        if not backends:
            return None
        backend = session.episode_backend
        if backend not in backends:
            backend = backends[0]
        session.episode_backend = backend
        catalog = session.episode_catalogs.get(backend)
        page = session.episode_pages.get((backend, 1))
        if catalog is None:
            page = session.episode_pages.get((backend, 1))
            if page is None:
                page = await service.load_page(
                    session.anime.identity, page=1, backend=backend
                )
                session.episode_pages[(backend, 1)] = page
            catalog = await service.load_catalog(
                session.anime.identity, backend=backend, first_page=page
            )
            session.episode_catalogs[backend] = catalog
        route = f"ap~{backend}"
        labels = labels_for_route(session, route, locale=locale)
        if not labels:
            # A source may expose paged episodes without a full catalog.
            count = int(session.anime.episodes_total or 0)
            if not count:
                count = len(page.items) if page is not None else 0
            labels = [
                f"{n} серия" if locale == "ru" else f"Episode {n}"
                for n in range(1, min(count, 5000) + 1)
            ]
        return (route, labels) if labels else None

    if section == "characters":
        from bots.telegram.card.features.characters import (
            _character_state,
            _ensure_loaded,
        )

        await _ensure_loaded(message, session, locale=locale)
        _, without, portraits = _character_state(session, None)
        route = f"gx~av~characters~{int(bool(without))}"
        labels = labels_for_route(session, route, locale=locale)
        return (route, labels) if labels else None

    if section in {"staff", "voices"}:
        from bots.telegram.people.loading import ensure_lazy_people

        await ensure_lazy_people(
            state, token, session.anime, message, section,
            locale=locale, requested_page=0,
        )
        without, portraits = people_state(session.anime, section, None)
        route = f"gx~av~{section}~{int(bool(without))}"
        labels = labels_for_route(session, route, locale=locale)
        return (route, labels) if labels else None

    if section == "screenshots":
        from bots.telegram.card.features.screenshots import ensure_screenshots_loaded

        await ensure_screenshots_loaded(message, session, locale=locale)
        route = "av~screenshots"
        labels = labels_for_route(session, route, locale=locale)
        return (route, labels) if labels else None

    if section == "all_ids":
        labels = labels_for_route(session, "av~all_ids", locale=locale)
        return ("av~all_ids", labels) if len(labels) > 1 else None

    if section in {"titles", "popularity"}:
        from bots.telegram.card.views.sections import view_pages

        if section == "popularity":
            from bots.telegram.card.features.popularity import ensure_popularity_loaded

            await ensure_popularity_loaded(message, session, locale=locale)
        pages = view_pages(session.anime, section, locale=locale)
        if len(pages) <= 1:
            return None
        labels = [
            f"Страница {index + 1}" if locale == "ru" else f"Page {index + 1}"
            for index in range(len(pages))
        ]
        return f"av~{section}", labels

    return None
