from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest

from aiogram.types import Message

from bots.telegram.card.runtime.state import (
    AnimeCardSession,
    TelegramCardState,
    RelatedReturnFrame,
)
from bots.telegram.card.ui.keyboards import fallback_main_keyboard, navigation_keyboard
from bots.telegram.card.views.renderer import send_anime_view, edit_anime_view
from core.models import SearchCandidate
from localization import text
from services.runtime.app_context import get_app_context
from services.users.settings import get_user_locale


async def _check_related_and_update_button(
    sent_message: Message,
    token: str,
    session: AnimeCardSession,
    state: TelegramCardState,
    *,
    locale: str,
    plain_fallback: bool,
) -> None:
    """Discover related titles after sending a card, without delaying initial display.

    The button is initially hidden. It becomes visible only after the related
    service returns at least one verified, non-self anime reference. An empty
    result, including a failed upstream lookup, never creates a dead button.
    """
    try:
        result = await get_app_context().related_anime_service.load_relations(
            session.anime.identity
        )
    except Exception:
        logging.exception("Unable to check related anime availability")
        return

    session.related_items = result.items
    session.related_by_source = result.by_source
    session.related_source_names = result.source_names
    session.related_errors = result.errors
    session.related_loaded = True

    if (
        not result.items
        or not session.main_view_active
        or state.anime_sessions.get(token) is not session
    ):
        return

    keyboard = (
        fallback_main_keyboard(
            token,
            locale=locale,
            show_all_ids=bool(session.anime.amdb_id),
            show_related=True,
            related_back=session.related_return is not None,
        )
        if plain_fallback
        else navigation_keyboard(
            token,
            session.anime,
            "main",
            0,
            1,
            locale=locale,
            show_related=True,
            related_back=session.related_return is not None,
        )
    )
    try:
        await sent_message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest:
        # An old/changed message is not a reason to fail the card session.
        logging.debug("Related anime keyboard update was rejected by Telegram")
    except Exception:
        logging.exception("Failed to update related anime button")


async def aggregate_and_send(
    message: Message,
    candidate: SearchCandidate,
    owner_user_id: int,
    state: TelegramCardState,
    *,
    replace_message: bool = False,
    related_return: RelatedReturnFrame | None = None,
) -> bool:
    locale = get_user_locale(owner_user_id)
    identity = candidate.identity
    if identity.is_empty():
        if not replace_message:
            await message.answer(text("search", "result_unavailable", locale=locale))
        return False

    context = get_app_context()
    try:
        anime = await context.aggregator.aggregate(candidate)
    except Exception as exc:
        logging.error(
            "Anime aggregation failed for %s: %s: %s",
            identity.describe(),
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        if not replace_message:
            await message.answer(text("search", "get_result_failed", locale=locale))
        return False

    try:
        await context.imdb_rating_service.enrich(anime)
    except Exception as exc:
        logging.warning(
            "IMDb rating supplement failed for %s: %s: %s",
            anime.identity.describe(),
            type(exc).__name__,
            exc,
            exc_info=True,
        )

    token = state.remember_anime(anime, owner_user_id)
    state.anime_sessions[token].related_return = related_return
    state.anime_sessions[token].inline_message_id = (
        getattr(message, "inline_message_id", None)
        if isinstance(getattr(message, "inline_message_id", None), str)
        else None
    )
    if replace_message:
        try:
            await edit_anime_view(
                message,
                token,
                anime,
                "main",
                0,
                locale=locale,
                show_related=False,
                replace_only=True,
                related_back=related_return is not None,
            )
        except Exception:
            state.anime_sessions.pop(token, None)
            logging.exception("Could not replace discovery card")
            return False
        sent_message, plain_fallback = message, False
    else:
        sent_message, plain_fallback = await send_anime_view(
            message,
            token,
            anime,
            section="main",
            page=0,
            locale=locale,
            show_related=False,
        )
    if sent_message is not None:
        task = asyncio.create_task(
            _check_related_and_update_button(
                sent_message,
                token,
                state.anime_sessions[token],
                state,
                locale=locale,
                plain_fallback=plain_fallback,
            )
        )
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)
    return sent_message is not None
