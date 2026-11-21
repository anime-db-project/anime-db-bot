"""Stateless progressive text disclosure; each part preserves previous messages."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bots.telegram.card.ui.constants import RICH_MESSAGE_LIMIT
from bots.telegram.card.views.rendering import split_text
from core.settings import get_settings


def long_text_parts(value: str, *, locale: str) -> list[str]:
    size = get_settings().telegram.long_text_chunk_size
    return split_text(value, min(size, RICH_MESSAGE_LIMIT - 700), locale=locale)


def continuation_keyboard(
    base: InlineKeyboardMarkup | None,
    token: str,
    kind: str,
    key: str,
    page: int,
    next_part: int,
    count: int,
    *,
    locale: str,
) -> InlineKeyboardMarkup | None:
    if next_part >= count:
        return base
    rows = list(base.inline_keyboard) if base is not None else []
    rows.insert(
        max(0, len(rows) - 1),
        [
            InlineKeyboardButton(
                text="Показать всё…" if locale == "ru" else "Show more…",
                callback_data=f"tx:{token}:{kind}:{key}:{page}:{next_part}",
            )
        ],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
