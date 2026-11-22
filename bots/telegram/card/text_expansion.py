"""Shared configurable text chunking for inline, single-message pagination."""

from __future__ import annotations

from bots.telegram.card.ui.constants import RICH_MESSAGE_LIMIT
from bots.telegram.card.views.rendering import split_text
from core.settings import get_settings


def long_text_parts(value: str, *, locale: str) -> list[str]:
    """Return complete, ordered pages; the caller renders normal navigation."""
    size = get_settings().telegram.long_text_chunk_size
    return split_text(value, min(size, RICH_MESSAGE_LIMIT - 700), locale=locale)
