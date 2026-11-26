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
    if progress is None or (
        section == "voices"
        and progress.locale is not None
        and progress.locale != locale
    ):
        progress = (
            service.build_progress(anime, section, locale=locale)
            if section == "voices"
            else service.build_progress(anime, section)
        )
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
    if section in {"staff", "voices"}:
        session = state.anime_sessions.get(token)
        if session is not None and progress.staff_portrait_indices is not None:
            if section == "staff":
                session.staff_gallery_indices = progress.staff_source_portrait_indices
            else:
                session.voice_gallery_indices = progress.staff_source_portrait_indices
    if (
        progress.staff_portrait_indices is not None
        and progress.portrait_cursor > progress.staff_prefetched_cursor
    ):
        from bots.telegram.card.media_prefetch import enqueue_portraits
        from core.people import all_people, portrait_urls

        bot = getattr(message, "bot", None)
        if isinstance(getattr(bot, "id", None), int):
            start = progress.staff_prefetched_cursor
            end = progress.portrait_cursor

            def enqueue_batch():
                return enqueue_portraits(
                    bot,
                    [
                        portrait_urls(person, source_order=anime.sources)
                        for person in all_people(anime, section)[start:end]
                    ],
                )

            if enqueue_batch():
                progress.staff_prefetched_cursor = end
            # Shikimori supplies supplemental photos asynchronously. Queue
            # newly discovered URLs too, without blocking the initial picker.
            for task in tuple(progress.enrichment_tasks):
                task.add_done_callback(
                    lambda done: None if done.cancelled() else enqueue_batch()
                )
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
