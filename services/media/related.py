from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from core.async_snapshot import AsyncSnapshotCache
from core.settings import get_settings, TelegramSettings
from services.runtime.rate_limits import is_background_upstream
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.capabilities import SourceCapability
from core.identity import AnimeIdentity
from core.media_urls import usable_image_url
from core.models import AnimeRecord
from core.related_anime import (
    AggregatedAnimeReference,
    AnimeReference,
    RELATION_ORDER,
    canonical_relation_kind,
)
from sources.runtime.catalog import source_supports


class IdentityMappingResolver(Protocol):
    def enrich(self, identity: AnimeIdentity) -> AnimeIdentity: ...


@dataclass(slots=True)
class RelatedAnimeLoadResult:
    items: list[AggregatedAnimeReference] = field(default_factory=list)
    by_source: dict[str, list[AggregatedAnimeReference]] = field(default_factory=dict)
    source_names: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    complete: bool = True


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw or "").split()).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _sort_items(
    items: list[AggregatedAnimeReference], *, recommendations: bool
) -> None:
    if recommendations:
        items.sort(
            key=lambda item: (
                -len(item.sources),
                -item.rank_score,
                item.candidate.title.casefold(),
            )
        )
        return
    items.sort(
        key=lambda item: (
            min(
                (RELATION_ORDER.get(value, 99) for value in item.relations),
                default=99,
            ),
            -len(item.sources),
            -item.rank_score,
            item.candidate.title.casefold(),
        )
    )


def _borrow_display_metadata(
    target: AggregatedAnimeReference,
    aggregate: AggregatedAnimeReference,
) -> None:
    for key, value in aggregate.titles.items():
        target.titles.setdefault(key, value)
    target.aliases = _dedupe([*target.aliases, *aggregate.aliases])
    target.posters = _dedupe([*target.posters, *aggregate.posters])
    target.kind = target.kind or aggregate.kind


class RelatedAnimeService:
    """Aggregate lazy related/recommended titles without choosing a canonical DB."""

    def __init__(
        self,
        sources: tuple[Any, ...] | list[Any],
        mapping_resolver: IdentityMappingResolver,
        *,
        cache_settings: TelegramSettings | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._mapping_resolver = mapping_resolver
        settings = cache_settings or get_settings().telegram
        self._snapshots = AsyncSnapshotCache[tuple, RelatedAnimeLoadResult](
            entries=settings.card_metadata_cache_entries,
            ttl=settings.card_metadata_cache_ttl_seconds,
            name="related_metadata",
        )

    async def load_relations(self, identity: AnimeIdentity) -> RelatedAnimeLoadResult:
        return await self._load(
            identity,
            loader_name="load_relations",
            capability=SourceCapability.RELATIONS,
            recommendations=False,
        )

    async def load_recommendations(
        self, identity: AnimeIdentity
    ) -> RelatedAnimeLoadResult:
        return await self._load(
            identity,
            loader_name="load_recommendations",
            capability=SourceCapability.RECOMMENDATIONS,
            recommendations=True,
        )

    async def _load(
        self,
        identity: AnimeIdentity,
        *,
        loader_name: str,
        capability: SourceCapability,
        recommendations: bool,
    ) -> RelatedAnimeLoadResult:
        key = (identity.fingerprint(), loader_name)
        started = time.monotonic()
        try:
            result = await self._snapshots.get(
                key,
                lambda: self._load_uncached(
                    identity,
                    loader_name=loader_name,
                    capability=capability,
                    recommendations=recommendations,
                ),
                cacheable=lambda result: not result.errors and result.complete,
            )
            if not result.complete and not is_background_upstream():
                result = await self._snapshots.get(
                    (key, "full"),
                    lambda: self._load_uncached(
                        identity,
                        loader_name=loader_name,
                        capability=capability,
                        recommendations=recommendations,
                    ),
                    cacheable=lambda value: not value.errors and value.complete,
                )
                if result.complete and not result.errors and self._snapshots.ttl > 0:
                    self._snapshots.cache.set(key, deepcopy(result))
            return result
        finally:
            logging.info(
                "Discovery metadata stage completed",
                extra={
                    "event": "card_stage",
                    "operation": loader_name,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                },
            )

    async def _load_uncached(
        self,
        identity: AnimeIdentity,
        *,
        loader_name: str,
        capability: SourceCapability,
        recommendations: bool,
    ) -> RelatedAnimeLoadResult:
        capable: list[Any] = []
        for source in self._sources:
            source_key = str(getattr(source, "source_key", "") or "").strip().casefold()
            loader = getattr(source, loader_name, None)
            if (
                source_key
                and identity.has(source_key)
                and source_supports(source_key, capability)
                and callable(loader)
            ):
                capable.append(source)

        async def run(source: Any):
            source_key = str(getattr(source, "source_key", "") or "").strip().casefold()
            try:
                values = await getattr(source, loader_name)(identity.copy())
                return source.name, source_key, list(values or []), None
            except Exception as exc:
                return (
                    source.name,
                    source_key,
                    [],
                    f"{type(exc).__name__}: {exc}",
                )

        result = RelatedAnimeLoadResult()
        if not capable:
            return result

        loaded = await asyncio.gather(*(run(source) for source in capable))
        merged: list[AggregatedAnimeReference] = []
        for source_name, source_key, values, error in loaded:
            if error:
                result.errors[source_name] = error
                continue

            local_items: list[AggregatedAnimeReference] = []
            for reference in values[:30]:
                if (
                    not isinstance(reference, AnimeReference)
                    or reference.identity.is_empty()
                ):
                    continue
                enriched = self._mapping_resolver.enrich(reference.identity)
                if enriched.matches(identity):
                    continue
                self._merge_reference(local_items, reference, enriched)
                self._merge_reference(merged, reference, enriched)

            if local_items:
                _sort_items(local_items, recommendations=recommendations)
                result.by_source[source_key] = local_items
                result.source_names[source_key] = next(
                    (
                        value.source
                        for value in values
                        if isinstance(value, AnimeReference) and value.source.strip()
                    ),
                    source_name,
                )

        if is_background_upstream():
            result.complete = not any(
                item.candidate.title == "Unknown anime"
                or not item.posters
                or item.kind == "music"
                for item in merged
            ) and not any(
                "BackgroundQuotaUnavailable" in error
                for error in result.errors.values()
            )
        else:
            await self._enrich_missing_metadata(merged)
        _sort_items(merged, recommendations=recommendations)
        result.items = merged

        # Keep source-local semantics (its own relation type and ordering), but
        # reuse richer title/poster metadata from the confirmed cross-source peer.
        for local_items in result.by_source.values():
            for local_item in local_items:
                aggregate = next(
                    (
                        item
                        for item in merged
                        if item.identity.matches(local_item.identity)
                    ),
                    None,
                )
                if aggregate is not None:
                    _borrow_display_metadata(local_item, aggregate)
        return result

    async def _enrich_missing_metadata(
        self, items: list[AggregatedAnimeReference]
    ) -> None:
        incomplete = [
            item
            for item in items
            if item.candidate.title == "Unknown anime"
            or not item.posters
            or item.kind == "music"
        ]
        if not incomplete:
            return
        slots = asyncio.Semaphore(3)
        source_map = {
            str(getattr(source, "source_key", "")): source for source in self._sources
        }

        async def fill(item):
            async with slots:
                for key in (
                    "mal",
                    "anilist",
                    "shikimori",
                    "kitsu",
                    "anisearch",
                    "anidb",
                ):
                    id = item.external_ids.get(key)
                    loader = getattr(source_map.get(key), "get_details", None)
                    if not id or not callable(loader):
                        continue
                    try:
                        record = await asyncio.wait_for(
                            loader(AnimeIdentity({key: id})), timeout=4
                        )
                    except Exception:
                        continue
                    if (
                        not isinstance(record, AnimeRecord)
                        or record.identity.get(key) != id
                        or record.identity.conflicting_ids(item.identity)
                    ):
                        continue
                    for title_key, value in record.titles.items():
                        if value:
                            item.titles.setdefault(title_key, value)
                    item.aliases = _dedupe([*item.aliases, *record.synonyms])
                    item.posters = _dedupe(
                        [
                            *item.posters,
                            *(url for url in record.posters if usable_image_url(url)),
                        ]
                    )
                    item.kind = record.kind or item.kind
                    if item.candidate.title != "Unknown anime" and item.posters:
                        return

        tasks = [asyncio.create_task(fill(item)) for item in incomplete[:12]]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        except TimeoutError:
            pass

    @staticmethod
    def _merge_reference(
        merged: list[AggregatedAnimeReference],
        reference: AnimeReference,
        identity: AnimeIdentity,
    ) -> None:
        matching = [
            index
            for index, item in enumerate(merged)
            if item.identity.matches(identity)
        ]
        if matching:
            target = merged[matching[0]]
            for index in reversed(matching[1:]):
                other = merged[index]
                if not target.identity.conflicting_ids(other.identity):
                    RelatedAnimeService._merge_aggregated(target, other)
                    del merged[index]
        else:
            target = AggregatedAnimeReference()
            merged.append(target)

        current_identity = target.identity
        if current_identity.conflicting_ids(identity):
            return
        target.external_ids.update(identity.external_ids)
        for key, raw_value in reference.titles.items():
            value = " ".join(str(raw_value or "").split()).strip()
            if not value:
                continue
            existing = target.titles.get(key)
            if existing is None:
                target.titles[key] = value
            elif existing.casefold() != value.casefold():
                target.aliases = _dedupe([*target.aliases, value])
        target.aliases = _dedupe([*target.aliases, *reference.aliases])
        target.posters = _dedupe(
            [
                *target.posters,
                *(url for url in reference.posters if usable_image_url(url)),
            ]
        )
        target.kind = target.kind or reference.kind
        target.sources = _dedupe([*target.sources, reference.source])
        if reference.relation:
            relation = canonical_relation_kind(reference.relation)
            target.relations = _dedupe([*target.relations, relation])
        target.rank_score += 1.0 / max(1, int(reference.rank))

    @staticmethod
    def _merge_aggregated(
        target: AggregatedAnimeReference,
        other: AggregatedAnimeReference,
    ) -> None:
        target.external_ids.update(other.external_ids)
        for key, value in other.titles.items():
            existing = target.titles.get(key)
            if existing is None:
                target.titles[key] = value
            elif existing.casefold() != value.casefold():
                target.aliases = _dedupe([*target.aliases, value])
        target.aliases = _dedupe([*target.aliases, *other.aliases])
        target.posters = _dedupe([*target.posters, *other.posters])
        target.kind = target.kind or other.kind
        target.relations = _dedupe([*target.relations, *other.relations])
        target.sources = _dedupe([*target.sources, *other.sources])
        target.rank_score += other.rank_score
