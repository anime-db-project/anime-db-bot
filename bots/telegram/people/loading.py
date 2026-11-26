from __future__ import annotations

import logging

from aiogram.types import Message

from bots.telegram.card.runtime.state import TelegramCardState
from core.models import AggregatedAnime
from localization import text
from services.media.people import PeopleLoadProgress, SourceLoadProgress
from services.runtime.app_context import get_app_context


def progress_key(token: str, section: str) -> str:
    return f"{token}:{section}"


def _service():
    return get_app_context().people_progress_service


def build_progress(anime: AggregatedAnime, section: str) -> PeopleLoadProgress:
    return _service().build_progress(anime, section)


def source_names(
    anime: AggregatedAnime,
    section: str,
    progress: PeopleLoadProgress | None,
) -> list[str]:
    return _service().source_names(anime, section, progress)


def scope_has_more(progress: PeopleLoadProgress, source: str | None) -> bool:
    return _service().scope_has_more(progress, source)


def scope_has_running(progress: PeopleLoadProgress, source: str | None) -> bool:
    return _service().scope_has_running(progress, source)


def scope_manifest_complete(progress: PeopleLoadProgress, source: str | None) -> bool:
    return _service().scope_manifest_complete(progress, source)


def scope_total_hint(
    anime: AggregatedAnime,
    section: str,
    progress: PeopleLoadProgress,
    source: str | None,
) -> int | None:
    return _service().scope_total_hint(anime, section, progress, source)


async def settle_started_batches(
    progress: PeopleLoadProgress, source: str | None = None
) -> None:
    await _service().settle_started_batches(progress, source)


async def ensure_lazy_people(
    state: TelegramCardState,
    token: str,
    anime: AggregatedAnime,
    message: Message,
    section: str,
    *,
    locale: str,
    source: str | None = None,
    requested_page: int = 0,
) -> PeopleLoadProgress:
    key = progress_key(token, section)
    progress = state.people_loads.get(key)
    service = _service()
    if progress is None:
        progress = service.build_progress(anime, section)
        state.people_loads[key] = progress
    if not progress.sources:
        return progress

    if not service.scope_manifest_complete(progress, None):
        loading_key = "loading_staff" if section == "staff" else "loading_voices"
        try:
            await message.edit_text(
                text=text("people", loading_key, locale=locale),
                reply_markup=None,
            )
        except Exception:
            logging.debug(
                "Could not show people manifest loading state",
                exc_info=True,
            )

    progress = await service.ensure(
        anime,
        section,
        progress,
        source=source,
        requested_page=requested_page,
    )
    if (
        section == "staff"
        and progress.portrait_cursor > progress.staff_prefetched_cursor
    ):
        from bots.telegram.media_cache import get_media_cache
        from core.people import portrait_urls

        bot = getattr(message, "bot", None)
        if isinstance(getattr(bot, "id", None), int):
            start = progress.staff_prefetched_cursor
            end = progress.portrait_cursor
            portraits = [
                portrait_urls(person, source_order=anime.sources)
                for person in anime.staff[start:end]
            ]
            # Give every person one image before queueing alternate portraits,
            # so a multi-source portrait set cannot monopolize the bounded queue.
            urls = list(
                dict.fromkeys(
                    [
                        *(images[0] for images in portraits if images),
                        *(url for images in portraits for url in images[1:]),
                    ]
                )
            )
            get_media_cache().enqueue(bot, urls)
            progress.staff_prefetched_cursor = end
    return progress


__all__ = [
    "PeopleLoadProgress",
    "SourceLoadProgress",
    "build_progress",
    "ensure_lazy_people",
    "progress_key",
    "scope_has_more",
    "scope_has_running",
    "scope_manifest_complete",
    "scope_total_hint",
    "settle_started_batches",
    "source_names",
]
