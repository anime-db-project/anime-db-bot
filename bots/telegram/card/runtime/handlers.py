from __future__ import annotations

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bots.telegram.callbacks import (
    EpisodeDetailCallback,
    KeyedPageCallback,
    PagePickerCallback,
    SectionSourceCallback,
    ViewCallback,
    resolve_key_or_legacy_index,
)
from bots.telegram.pagination import card_picker_keyboard
from bots.telegram.card.runtime.picker_entries import (
    labels_for_route,
    picker_parent_callback,
    prepare_initial_picker,
)
from bots.telegram.card.features.characters import edit_characters_view
from bots.telegram.card.features.descriptions import ensure_descriptions_loaded
from bots.telegram.card.features.popularity import ensure_popularity_loaded
from bots.telegram.card.features.wikipedia import (
    edit_wikipedia_home,
    register_wikipedia_handlers,
)
from bots.telegram.card.features.related import (
    edit_related_view,
    parse_related_route,
    send_related_item,
)
from bots.telegram.card.ui.constants import SECTION_LABELS, SOURCE_SELECTABLE_SECTIONS
from bots.telegram.card.features.episodes import edit_episode_detail, edit_episodes_view
from bots.telegram.card.features.mapping_ids import edit_mapping_ids_view
from bots.telegram.card.views.renderer import (
    edit_anime_view,
    edit_description_view,
    edit_source_section_view,
)
from bots.telegram.card.features.screenshots import (
    edit_screenshots_view,
    ensure_screenshots_loaded,
)
from bots.telegram.card.ui.selectors import (
    description_source_options,
    source_section_options,
)
from bots.telegram.card.runtime.state import AnimeCardSession, TelegramCardState
from bots.telegram.card.features.trailers import edit_trailers_view
from bots.telegram.settings import register_settings_handlers
from localization import text
from services.runtime.app_context import get_app_context
from services.users.settings import get_user_locale


def _session_for_callback(
    callback: CallbackQuery, token: str, state: TelegramCardState
) -> AnimeCardSession | None:
    session = state.anime_sessions.get(token)
    if session is None or session.owner_user_id != callback.from_user.id:
        return None
    return session


def build_dispatcher(mode_name: str, state: TelegramCardState) -> Dispatcher:
    """Build the core Telegram dispatcher without search-provider-specific handlers."""
    dp = Dispatcher()
    register_settings_handlers(dp)
    register_wikipedia_handlers(dp, state)

    @dp.message(CommandStart())
    async def start_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        locale = get_user_locale(user_id)
        context = get_app_context()
        source_names = ", ".join(source.name for source in context.sources) or text(
            "start", "no_sources", locale=locale
        )
        lines = [
            text("start", "greeting", locale=locale),
            "",
            text("start", "telegram_mode", locale=locale, mode=mode_name),
            text(
                "start",
                "default_search",
                locale=locale,
                providers=context.search_service.provider_name,
            ),
            text("start", "data_sources", locale=locale, sources=source_names),
            "",
            text("start", "merge_note", locale=locale),
            "",
            text("start", "commands", locale=locale),
            text("start", "anime", locale=locale),
            text("start", "settings", locale=locale),
            text("start", "ping", locale=locale),
        ]
        await message.answer("\n".join(lines))

    @dp.message(Command("ping"))
    async def ping_handler(message: Message) -> None:
        await message.answer("pong")

    @dp.callback_query(F.data == "noop")
    async def noop_callback(callback: CallbackQuery) -> None:
        await callback.answer()

    @dp.callback_query(F.data.startswith("ci:"))
    async def card_initial_picker_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        parts = (callback.data or "").split(":")
        initial = len(parts) == 3
        if initial:
            token, section = parts[1:]
            from bots.telegram.card.ui.constants import PAGED_ENTRY_SECTIONS

            source_route = section.startswith(
                (
                    "ar~",
                    "at~",
                    "ac~",
                    "ap~",
                    "ad~",
                    "as~screenshots~",
                    "as~titles~",
                    "as~popularity~",
                    "ps~",
                )
            )
            if section not in PAGED_ENTRY_SECTIONS and not source_route:
                await callback.answer(
                    text("card", "invalid_command", locale=locale), show_alert=True
                )
                return
            data = None
        else:
            data = PagePickerCallback.parse(callback.data, prefix="ci")
            if data is None or data.total > 5000:
                await callback.answer(
                    text("card", "invalid_command", locale=locale), show_alert=True
                )
                return
            token = data.token
        session = _session_for_callback(callback, token, state)
        if session is None:
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        if callback.message is None:
            await callback.answer()
            return
        session.main_view_active = False
        await callback.answer()
        if initial:
            try:
                if source_route and section.startswith("ap~"):
                    backend = section.split("~", 1)[1]
                    service = get_app_context().episode_service
                    available = service.available_backends(session.anime.identity)
                    if backend not in available:
                        raise ValueError("Episode backend unavailable")
                    session.episode_backend = backend
                    prepared = await prepare_initial_picker(
                        state,
                        token,
                        session,
                        "episodes",
                        callback.message,
                        locale=locale,
                    )
                elif source_route and section.startswith("ps~"):
                    prepared = await prepare_initial_picker(
                        state,
                        token,
                        session,
                        section,
                        callback.message,
                        locale=locale,
                    )
                elif source_route:
                    scoped_labels = labels_for_route(session, section, locale=locale)
                    prepared = (section, scoped_labels) if scoped_labels else None
                else:
                    prepared = await prepare_initial_picker(
                        state, token, session, section, callback.message, locale=locale
                    )
            except Exception:
                import logging

                logging.exception("Failed to prepare initial card picker")
                prepared = None
            if prepared is None:
                if source_route:
                    await callback.message.edit_text(
                        text("common", "no_data", locale=locale),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=text("common", "back", locale=locale),
                                        callback_data=picker_parent_callback(
                                            token, section
                                        ),
                                    )
                                ]
                            ]
                        ),
                    )
                    return
                if section in {"related", "recommendations"}:
                    await edit_related_view(
                        callback.message,
                        token,
                        session,
                        section=section,
                        page=0,
                        locale=locale,
                    )
                elif section == "all_ids":
                    await edit_mapping_ids_view(
                        callback.message,
                        token,
                        session.anime,
                        mapping_resolver=get_app_context().identity_resolver.mapping_resolver,
                        page=0,
                        locale=locale,
                    )
                elif section == "episodes":
                    await edit_episodes_view(
                        callback.message,
                        token,
                        session,
                        view_page=0,
                        backend=None,
                        locale=locale,
                    )
                elif section == "characters":
                    await edit_characters_view(
                        callback.message,
                        token,
                        session,
                        source_key=None,
                        page=0,
                        locale=locale,
                    )
                elif section == "screenshots":
                    await edit_screenshots_view(
                        callback.message,
                        token,
                        session,
                        source_key=None,
                        page=0,
                        locale=locale,
                    )
                elif section in {"staff", "voices"}:
                    from bots.telegram.people.portraits import _edit_people_view

                    await _edit_people_view(
                        state,
                        callback.message,
                        token,
                        session.anime,
                        section,
                        None,
                        0,
                        locale=locale,
                    )
                else:
                    await edit_anime_view(
                        callback.message,
                        token,
                        session.anime,
                        section=section,
                        page=0,
                        locale=locale,
                    )
                return
            route, labels = prepared
            data = PagePickerCallback("ci", token, route, 0, len(labels), 0)
        else:
            assert data is not None
            labels = labels_for_route(session, data.route, locale=locale)
        if data is None:
            return
        try:
            keyboard = card_picker_keyboard(
                data,
                locale=locale,
                labels=labels,
                highlight_current=False,
                back_callback=picker_parent_callback(token, data.route),
            )
        except ValueError:
            await callback.message.edit_text(
                text("card", "invalid_command", locale=locale)
            )
            return
        await callback.message.edit_text(
            "Выберите элемент:" if locale == "ru" else "Choose an item:",
            reply_markup=keyboard,
        )

    @dp.callback_query(F.data.startswith("cp:"))
    async def card_page_picker_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = PagePickerCallback.parse(callback.data, prefix="cp")
        if data is None:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return
        session = _session_for_callback(callback, data.token, state)
        if session is None:
            raw_session = state.anime_sessions.get(data.token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return
        try:
            keyboard = card_picker_keyboard(
                data,
                locale=locale,
                labels=labels_for_route(session, data.route, locale=locale),
            )
        except ValueError:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return
        await callback.answer()
        await callback.message.edit_text(
            "Выберите элемент:" if locale == "ru" else "Choose an item:",
            reply_markup=keyboard,
        )

    @dp.callback_query(F.data.startswith("ad:"))
    async def description_source_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="ad")
        if data is None:
            await callback.answer(
                text("card", "invalid_description", locale=locale), show_alert=True
            )
            return

        token = data.token
        raw_session = state.anime_sessions.get(token)
        if raw_session is None:
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        if raw_session.owner_user_id != callback.from_user.id:
            await callback.answer(
                text("card", "not_owner", locale=locale), show_alert=True
            )
            return
        anime = raw_session.anime
        if callback.message is None:
            await callback.answer()
            return

        await ensure_descriptions_loaded(callback.message, raw_session, locale=locale)
        options = description_source_options(anime)
        try:
            source_key = resolve_key_or_legacy_index(
                data.key, [key for key, _ in options]
            )
        except ValueError:
            await callback.answer(
                text("card", "description_source_unavailable", locale=locale),
                show_alert=True,
            )
            return

        await callback.answer()
        await edit_description_view(
            callback.message,
            token,
            anime,
            source_key=source_key,
            page=data.page,
            locale=locale,
        )

    @dp.callback_query(F.data.startswith("as:"))
    async def source_section_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = SectionSourceCallback.parse(callback.data)
        if data is None or data.section not in SOURCE_SELECTABLE_SECTIONS:
            await callback.answer(
                text("card", "invalid_source", locale=locale), show_alert=True
            )
            return

        token = data.token
        section = data.section
        raw_session = state.anime_sessions.get(token)
        if raw_session is None:
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        if raw_session.owner_user_id != callback.from_user.id:
            await callback.answer(
                text("card", "not_owner", locale=locale), show_alert=True
            )
            return
        anime = raw_session.anime
        if callback.message is None:
            await callback.answer()
            return

        if section == "popularity":
            await ensure_popularity_loaded(callback.message, raw_session, locale=locale)
        elif section == "screenshots":
            await ensure_screenshots_loaded(
                callback.message, raw_session, locale=locale
            )

        options = source_section_options(anime, section)
        try:
            source_key = resolve_key_or_legacy_index(
                data.source_key, [key for key, _ in options]
            )
        except ValueError:
            await callback.answer(
                text("card", "source_unavailable", locale=locale), show_alert=True
            )
            return

        await callback.answer()
        if section == "screenshots":
            await edit_screenshots_view(
                callback.message,
                token,
                raw_session,
                source_key=source_key,
                page=data.page,
                locale=locale,
            )
            return
        await edit_source_section_view(
            callback.message,
            token,
            anime,
            section,
            source_key,
            data.page,
            locale=locale,
        )

    @dp.callback_query(F.data.startswith("ap:"))
    async def episode_source_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="ap")
        if data is None:
            await callback.answer(
                text("card", "invalid_source", locale=locale), show_alert=True
            )
            return
        token = data.token
        session = _session_for_callback(callback, token, state)
        if session is None:
            raw_session = state.anime_sessions.get(token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return
        await callback.answer()
        try:
            await edit_episodes_view(
                callback.message,
                token,
                session,
                view_page=data.page,
                backend=data.key,
                locale=locale,
            )
        except ValueError:
            await edit_episodes_view(
                callback.message,
                token,
                session,
                view_page=0,
                backend=None,
                locale=locale,
            )

    @dp.callback_query(F.data.startswith("ae:"))
    async def episode_detail_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = EpisodeDetailCallback.parse(callback.data)
        if data is None:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return

        token = data.token
        session = _session_for_callback(callback, token, state)
        if session is None:
            raw_session = state.anime_sessions.get(token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return

        await callback.answer()
        await edit_episode_detail(
            callback.message,
            token,
            session,
            episode_number=data.episode_number,
            return_page=data.return_page,
            backend=data.backend,
            locale=locale,
        )

    @dp.callback_query(F.data.startswith("ac:"))
    async def character_source_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="ac")
        if data is None:
            await callback.answer(
                text("card", "invalid_source", locale=locale), show_alert=True
            )
            return

        token = data.token
        session = _session_for_callback(callback, token, state)
        if session is None:
            raw_session = state.anime_sessions.get(token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return

        await callback.answer()
        try:
            await edit_characters_view(
                callback.message,
                token,
                session,
                source_key=data.key,
                page=data.page,
                locale=locale,
            )
        except ValueError:
            await edit_characters_view(
                callback.message,
                token,
                session,
                source_key=None,
                page=0,
                locale=locale,
            )

    @dp.callback_query(F.data.startswith("at:"))
    async def trailer_source_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="at")
        if data is None:
            await callback.answer(
                text("card", "invalid_source", locale=locale), show_alert=True
            )
            return

        token = data.token
        session = _session_for_callback(callback, token, state)
        if session is None:
            raw_session = state.anime_sessions.get(token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return

        await callback.answer()
        try:
            await edit_trailers_view(
                callback.message,
                token,
                session,
                source_key=data.key,
                page=data.page,
                locale=locale,
            )
        except ValueError:
            await edit_trailers_view(
                callback.message,
                token,
                session,
                source_key=None,
                page=0,
                locale=locale,
            )

    @dp.callback_query(F.data.startswith("ar:"))
    async def related_source_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="ar")
        route = parse_related_route(data.key) if data is not None else None
        if data is None or route is None:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return
        session = _session_for_callback(callback, data.token, state)
        if session is None:
            raw_session = state.anime_sessions.get(data.token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return
        section, source_key = route
        await callback.answer()
        await edit_related_view(
            callback.message,
            data.token,
            session,
            section=section,
            source_key=source_key,
            page=data.page,
            locale=locale,
        )

    @dp.callback_query(F.data.startswith("ari:"))
    async def related_item_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = KeyedPageCallback.parse(callback.data, prefix="ari")
        route = parse_related_route(data.key) if data is not None else None
        if data is None or route is None:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return
        session = _session_for_callback(callback, data.token, state)
        if session is None:
            raw_session = state.anime_sessions.get(data.token)
            key = "not_owner" if raw_session is not None else "expired"
            await callback.answer(text("card", key, locale=locale), show_alert=True)
            return
        if callback.message is None:
            await callback.answer()
            return
        section, source_key = route
        await callback.answer()
        try:
            await send_related_item(
                callback.message,
                session,
                state,
                section=section,
                source_key=source_key,
                index=data.page,
                owner_user_id=callback.from_user.id,
            )
        except IndexError:
            await callback.message.answer(
                text("card", "invalid_command", locale=locale)
            )

    @dp.callback_query(F.data.startswith("av:"))
    async def anime_view_callback_handler(callback: CallbackQuery) -> None:
        locale = get_user_locale(callback.from_user.id)
        data = ViewCallback.parse(callback.data)
        if data is None:
            await callback.answer(
                text("card", "invalid_command", locale=locale), show_alert=True
            )
            return

        token = data.token
        section = data.section
        raw_session = state.anime_sessions.get(token)
        if raw_session is None:
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        if raw_session.owner_user_id != callback.from_user.id:
            await callback.answer(
                text("card", "not_owner", locale=locale), show_alert=True
            )
            return
        anime = raw_session.anime
        if section not in SECTION_LABELS:
            await callback.answer(
                text("card", "invalid_section", locale=locale), show_alert=True
            )
            return
        if callback.message is None:
            await callback.answer()
            return

        # Keep the delayed related-button update on the main card only.
        raw_session.main_view_active = section == "main"
        await callback.answer()
        if section == "wikipedia":
            await edit_wikipedia_home(
                callback.message,
                token,
                raw_session,
                locale=locale,
                user_id=callback.from_user.id,
            )
            return
        if section == "all_ids":
            await edit_mapping_ids_view(
                callback.message,
                token,
                anime,
                mapping_resolver=get_app_context().identity_resolver.mapping_resolver,
                page=data.page,
                locale=locale,
            )
            return
        if section in {"related", "recommendations"}:
            await edit_related_view(
                callback.message,
                token,
                raw_session,
                section=section,
                page=data.page,
                locale=locale,
            )
            return
        if section == "episodes":
            await edit_episodes_view(
                callback.message,
                token,
                raw_session,
                view_page=data.page,
                backend=None,
                locale=locale,
            )
            return
        if section == "characters":
            await edit_characters_view(
                callback.message,
                token,
                raw_session,
                source_key=None,
                page=data.page,
                locale=locale,
            )
            return
        if section == "trailers":
            await edit_trailers_view(
                callback.message,
                token,
                raw_session,
                source_key=None,
                page=0,
                locale=locale,
            )
            return
        if section == "screenshots":
            await edit_screenshots_view(
                callback.message,
                token,
                raw_session,
                source_key=None,
                page=data.page,
                locale=locale,
            )
            return
        if section == "popularity":
            await ensure_popularity_loaded(callback.message, raw_session, locale=locale)
        elif section == "description":
            await ensure_descriptions_loaded(
                callback.message, raw_session, locale=locale
            )

        await edit_anime_view(
            callback.message,
            token,
            anime,
            section=section,
            page=data.page,
            locale=locale,
            show_related=bool(raw_session.related_items),
        )

    return dp
