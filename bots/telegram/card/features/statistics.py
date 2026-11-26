from __future__ import annotations

import logging

from aiogram.types import Message

from bots.telegram.card.runtime.state import AnimeCardSession
from localization import text
from services.runtime.app_context import get_app_context


async def ensure_statistics_loaded(
    message: Message,
    session: AnimeCardSession,
    *,
    locale: str,
) -> None:
    if session.statistics_loaded:
        return
    try:
        await message.edit_text(
            text=text("card", "statistics_loading", locale=locale),
            reply_markup=None,
        )
    except Exception:
        logging.debug("Could not show statistics loading state", exc_info=True)

    result = await get_app_context().statistics_service.load(session.anime.identity)
    session.anime.statistics_by_source = result.by_source
    session.statistics_errors = result.errors
    session.statistics_loaded = True
