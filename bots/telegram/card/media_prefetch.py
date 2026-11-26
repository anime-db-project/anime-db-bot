"""Bounded, lazy media prefetch for catalog-backed galleries."""

from __future__ import annotations

from collections.abc import Sequence

from bots.telegram.card.runtime.state import AnimeCardSession
from core.settings import get_settings


def enqueue_portraits(bot, rows: Sequence[Sequence[str]]) -> bool:
    """Queue primary portraits before alternative images. No network wait."""
    from bots.telegram.media_cache import get_media_cache

    if not isinstance(getattr(bot, "id", None), int):
        return False
    cache = get_media_cache()
    settings = cache.settings
    if (
        not settings.media_cache_enabled
        or not settings.media_cache_background_upload
        or settings.media_cache_chat_id >= 0
    ):
        return False
    urls = list(
        dict.fromkeys(
            [
                *(row[0] for row in rows if row),
                *(url for row in rows for url in row[1:]),
            ]
        )
    )
    before = cache.stats["queue_dropped"]
    cache.enqueue(bot, urls)
    return cache.stats["queue_dropped"] == before


def prefetch_gallery(
    message,
    session: AnimeCardSession,
    section: str,
    source: str | None,
    rows: Sequence[Sequence[str]],
    *,
    index: int = 0,
    visible_count: int = 1,
) -> None:
    """Prefetch only the selected window, and any window the visible page crosses.

    Rows represent photo-bearing entities (or individual screenshots), not
    pages. The complete list remains accessible without waiting for uploads.
    """
    if not rows:
        return
    size = max(1, get_settings().sources.people_portrait_batch_size)
    start = min(max(0, index), len(rows) - 1)
    end = min(len(rows) - 1, start + max(1, visible_count) - 1)
    for batch in range(start // size, end // size + 1):
        key = (section, source, batch)
        if key in session.media_prefetch_batches:
            continue
        if enqueue_portraits(
            getattr(message, "bot", None), rows[batch * size : (batch + 1) * size]
        ):
            session.media_prefetch_batches.add(key)
