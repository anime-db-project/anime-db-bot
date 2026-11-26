from __future__ import annotations

import asyncio
import logging
import time
from services.runtime.rate_limits import background_upstream

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
    if state.anime_sessions.get(token) is not session:
        return
    try:
        with background_upstream():
            async with asyncio.timeout(12):
                result = await get_app_context().related_anime_service.load_relations(
                    session.anime.identity
                )
    except Exception:
        logging.exception("Unable to check related anime availability")
        return

    if session.related_loaded and not result.complete:
        return
    session.related_items = result.items
    session.related_by_source = result.by_source
    session.related_source_names = result.source_names
    session.related_errors = result.errors
    session.related_loaded = result.complete

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


def cancel_related_probe(session: AnimeCardSession) -> None:
    task = session.related_probe_task
    if task is not None and not task.done():
        task.cancel()
    session.related_probe_task = None


def schedule_related_probe(
    message: Message,
    token: str,
    session: AnimeCardSession,
    state: TelegramCardState,
    *,
    locale: str,
    plain_fallback: bool = False,
) -> None:
    if session.related_loaded or session.related_items:
        return
    if session.related_probe_task is not None and not session.related_probe_task.done():
        return
    task = asyncio.create_task(
        _check_related_and_update_button(
            message, token, session, state, locale=locale, plain_fallback=plain_fallback
        )
    )
    session.related_probe_task = task
    state.background_tasks.add(task)

    def finished(done):
        state.background_tasks.discard(done)
        if session.related_probe_task is done:
            session.related_probe_task = None

    task.add_done_callback(finished)


async def aggregate_and_send(
    message: Message,
    candidate: SearchCandidate,
    owner_user_id: int,
    state: TelegramCardState,
    *,
    replace_message: bool = False,
    related_return: RelatedReturnFrame | None = None,
) -> bool:
    started = time.monotonic()
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

    metadata_done = time.monotonic()
    try:
        async with asyncio.timeout(2):
            await context.imdb_rating_service.enrich(anime)
    except Exception as exc:
        logging.warning(
            "IMDb rating supplement failed for %s: %s: %s",
            anime.identity.describe(),
            type(exc).__name__,
            exc,
            exc_info=True,
        )

    imdb_done = time.monotonic()
    token = state.remember_anime(anime, owner_user_id)
    state.anime_sessions[token].related_return = related_return
    state.anime_sessions[token].inline_message_id = (
        getattr(message, "inline_message_id", None)
        if isinstance(getattr(message, "inline_message_id", None), str)
        else None
    )
    if replace_message:
        try:
            plain_fallback = await edit_anime_view(
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
        sent_message = message
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
    logging.info(
        "Anime card delivery stages",
        extra={
            "event": "card_delivery",
            "operation": "replace" if replace_message else "send",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "metadata_ms": round((metadata_done - started) * 1000, 3),
            "imdb_ms": round((imdb_done - metadata_done) * 1000, 3),
            "delivery_ms": round((time.monotonic() - imdb_done) * 1000, 3),
            "depth": state.anime_sessions[token].related_depth,
            "size": len(anime.posters),
        },
    )
    if sent_message is not None:
        schedule_related_probe(
            sent_message,
            token,
            state.anime_sessions[token],
            state,
            locale=locale,
            plain_fallback=plain_fallback,
        )
    return sent_message is not None
