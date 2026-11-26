from __future__ import annotations

import asyncio
import copy
import unittest

from core.models import AggregatedAnime, PersonCredit
from core.people_loading import PeopleManifest, without_portraits
from services.media.people import PeopleProgressService


class _FakeAggregator:
    def merge_people_source(
        self,
        anime: AggregatedAnime,
        *,
        source_name: str,
        people: list[PersonCredit],
        section: str,
    ) -> None:
        mapping = getattr(anime, f"{section}_by_source")
        # Enough identity merging for these service-level tests.
        by_name: dict[str, PersonCredit] = {}
        for person in people:
            key = person.name.casefold()
            current = by_name.get(key)
            if current is None:
                by_name[key] = copy.deepcopy(person)
                continue
            current.roles = list(dict.fromkeys([*current.roles, *person.roles]))
            current.external_ids.update(person.external_ids)
            for source, urls in person.portraits.items():
                current.portraits[source] = list(
                    dict.fromkeys([*current.portraits.get(source, []), *urls])
                )
        mapping[source_name] = list(by_name.values())

        merged_by_name: dict[str, PersonCredit] = {}
        for values in mapping.values():
            for person in values:
                key = person.name.casefold()
                current = merged_by_name.get(key)
                if current is None:
                    merged_by_name[key] = copy.deepcopy(person)
                    continue
                current.roles = list(dict.fromkeys([*current.roles, *person.roles]))
                current.external_ids.update(person.external_ids)
                for source, urls in person.portraits.items():
                    current.portraits[source] = list(
                        dict.fromkeys([*current.portraits.get(source, []), *urls])
                    )
        setattr(anime, section, list(merged_by_name.values()))


class _ManifestSource:
    name = "AniList"
    source_key = "anilist"

    def __init__(self, count: int = 5, release: asyncio.Event | None = None) -> None:
        self.release = release
        self.manifest_calls = 0
        self.people = [
            PersonCredit(
                name=f"Person {index}",
                external_ids={"anilist": str(index)},
                portraits={"AniList": [f"https://img.example/{index}.jpg"]},
            )
            for index in range(1, count + 1)
        ]

    async def load_voices_manifest(self, identity) -> PeopleManifest:
        del identity
        self.manifest_calls += 1
        if self.release is not None:
            await self.release.wait()
        return PeopleManifest(
            people=without_portraits(self.people),
            portrait_pool=copy.deepcopy(self.people),
        )


class _NamedManifestSource(_ManifestSource):
    def __init__(
        self,
        name: str,
        source_key: str,
        person_name: str,
        release: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.source_key = source_key
        self.release = release
        self.manifest_calls = 0
        self.people = [
            PersonCredit(
                name=person_name,
                external_ids={source_key: "1"},
                portraits={name: [f"https://img.example/{source_key}.jpg"]},
            )
        ]


class _PortraitOnlySource:
    name = "Shikimori"
    source_key = "shikimori"

    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.calls = 0

    async def load_voices_manifest(self, identity) -> PeopleManifest:
        del identity
        return PeopleManifest(portrait_only=True)

    async def load_voices_portraits(
        self, identity, people: list[PersonCredit]
    ) -> list[PersonCredit]:
        del identity
        self.calls += 1
        await self.release.wait()
        result = copy.deepcopy(people)
        for person in result:
            person.portraits["Shikimori"] = [f"https://shiki.example/{person.name}.jpg"]
        return result


class PeopleProgressServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_people_loader_ignores_truncated_preview(self) -> None:
        class FullOnlySource:
            name = "AniList"
            source_key = "anilist"

            def __init__(self) -> None:
                self.calls = 0

            async def load_staff(self, identity):
                del identity
                self.calls += 1
                return [PersonCredit(name=f"Staff {n}") for n in range(56)]

        source = FullOnlySource()
        service = PeopleProgressService([source], _FakeAggregator())
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            external_ids={"anilist": "1"},
            staff_by_source={"AniList": [PersonCredit(name="Staff 0")]},
            staff=[PersonCredit(name="Staff 0")],
        )
        progress = service.build_progress(anime, "staff")
        self.assertFalse(progress.sources["AniList"].manifest_complete)
        await service.ensure(anime, "staff", progress, requested_page=0)
        self.assertEqual(len(anime.staff), 56)
        self.assertEqual(source.calls, 1)

    async def test_staff_full_roster_stable_picker_and_real_portrait_batches(self):
        from bots.telegram.card.runtime.state import AnimeCardSession
        from bots.telegram.card.runtime.picker_entries import labels_for_route
        from bots.telegram.people.views import _page_text
        from bots.telegram.pagination import card_route_callback
        from services.aggregation.merge import AggregationMergeSupport

        class StaffSource(_ManifestSource):
            async def load_staff_manifest(self, identity):
                return await self.load_voices_manifest(identity)

        source = StaffSource(count=141)
        # 20 names have no photo at all and must not consume the budget.
        for person in source.people[:20]:
            person.portraits = {}
        service = PeopleProgressService(
            [source], AggregationMergeSupport([source]), portrait_batch_size=50
        )
        anime = AggregatedAnime(
            title="Example", sources=["AniList"], external_ids={"anilist": "1"}
        )
        progress = service.build_progress(anime, "staff")
        await service.settle_manifests(anime, "staff", progress)
        self.assertEqual(len(anime.staff), 141)
        self.assertEqual(sum(bool(p.portraits) for p in anime.staff), 0)
        from bots.telegram.people.views import staff_gallery, staff_gallery_ordinal

        session = AnimeCardSession(
            anime=anime,
            owner_user_id=7,
            staff_gallery_indices=progress.staff_source_portrait_indices,
        )
        route = "gx~av~staff~1"
        before = labels_for_route(session, route, locale="en")
        self.assertEqual(len(before), 122)
        groups, portraits = staff_gallery(
            anime, None, "en", session.staff_gallery_indices
        )
        self.assertEqual(sum(map(len, groups)), 20)
        self.assertEqual(staff_gallery_ordinal(groups, portraits, 51), 71)
        self.assertEqual(card_route_callback("token", route, 121), "av:token:staff:121")
        await service.ensure(anime, "staff", progress, requested_page=0)
        self.assertEqual(len(anime.staff), 141)
        self.assertEqual(progress.portrait_cursor, 70)
        self.assertEqual(sum(bool(p.portraits) for p in anime.staff), 50)
        self.assertEqual(labels_for_route(session, route, locale="en"), before)

        def view(page):
            return _page_text(
                anime,
                "staff",
                None,
                page,
                locale="en",
                staff_indices=session.staff_gallery_indices,
            )

        self.assertIn("Person 1", view(0)[0])
        self.assertIn("Person 20", view(0)[0])
        self.assertEqual(view(0)[1], [])
        self.assertTrue(view(50)[1])
        self.assertEqual(view(51)[1], [])
        await service.ensure(anime, "staff", progress, requested_page=70)
        self.assertEqual(progress.portrait_cursor, 70)
        await service.ensure(
            anime,
            "staff",
            progress,
            requested_page=staff_gallery_ordinal(groups, portraits, 51),
        )
        self.assertEqual(progress.portrait_cursor, 120)
        self.assertEqual(sum(bool(p.portraits) for p in anime.staff), 100)
        self.assertTrue(view(51)[1])
        await service.ensure(
            anime, "staff", progress, source="AniList", requested_page=141
        )
        self.assertEqual(progress.portrait_cursor, 141)
        self.assertEqual(sum(bool(p.portraits) for p in anime.staff), 121)
        self.assertEqual(labels_for_route(session, route, locale="en"), before)
        self.assertEqual(
            labels_for_route(session, "gx~as~staff~anilist~1", locale="en"), before
        )
        self.assertEqual(view(121)[2:], (141, 141, 121))
        self.assertEqual(source.manifest_calls, 1)

    async def test_voice_gallery_uses_localized_complete_roster_and_stable_batches(
        self,
    ):
        from bots.telegram.card.runtime.state import AnimeCardSession
        from bots.telegram.card.runtime.picker_entries import labels_for_route
        from bots.telegram.people.views import (
            staff_gallery,
            staff_gallery_ordinal,
            _page_text,
        )

        source = _ManifestSource(count=160)
        for i, person in enumerate(source.people):
            person.roles = [
                "voice_of::Example [en]" if i < 40 else "voice_of::Example [ja]"
            ]
            if 40 <= i < 50:
                person.portraits = {}
        service = PeopleProgressService(
            [source], _FakeAggregator(), portrait_batch_size=50
        )
        anime = AggregatedAnime(
            title="Example", sources=["AniList"], external_ids={"anilist": "1"}
        )
        progress = service.build_progress(anime, "voices", locale="ru")
        await service.ensure(anime, "voices", progress, requested_page=0)
        indices = progress.staff_source_portrait_indices
        groups, portraits = staff_gallery(anime, None, "ru", indices, section="voices")
        self.assertEqual(len(anime.voices), 160)
        self.assertEqual(sum(map(len, groups)), 10)
        self.assertEqual(len(portraits), 110)
        self.assertEqual(sum(bool(p.portraits) for p in anime.voices), 50)
        session = AnimeCardSession(anime, 1, voice_gallery_indices=indices)
        route = f"gx~av~voices~{len(groups)}"
        before = labels_for_route(session, route, locale="ru")
        self.assertEqual(len(before), 111)
        self.assertEqual(progress.portrait_cursor, 100)
        target = staff_gallery_ordinal(groups, portraits, 51)
        self.assertEqual(target, 101)
        await service.ensure(anime, "voices", progress, requested_page=target)
        self.assertEqual(sum(bool(p.portraits) for p in anime.voices), 100)
        self.assertEqual(labels_for_route(session, route, locale="ru"), before)
        self.assertTrue(
            _page_text(anime, "voices", None, 51, locale="ru", staff_indices=indices)[1]
        )
        self.assertEqual(
            _page_text(anime, "voices", None, 51, locale="ru", staff_indices=indices)[
                3
            ],
            120,
        )

    async def test_preview_people_do_not_mark_manifest_as_complete(self) -> None:
        source = _ManifestSource(count=56)
        service = PeopleProgressService(
            [source], _FakeAggregator(), portrait_batch_size=50
        )
        preview_person = source.people[0]
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            external_ids={"anilist": "1"},
            voices_by_source={"AniList": [preview_person]},
            voices=[preview_person],
        )
        progress = service.build_progress(anime, "voices")
        self.assertFalse(progress.sources["AniList"].manifest_complete)
        await service.ensure(anime, "voices", progress, requested_page=0)
        self.assertEqual(source.manifest_calls, 1)
        self.assertEqual(len(anime.voices), 56)
        self.assertEqual(len(anime.voices_by_source["AniList"]), 56)

    async def test_first_navigation_shows_all_people_from_manifest(self) -> None:
        source = _ManifestSource(count=141)
        service = PeopleProgressService(
            [source], _FakeAggregator(), portrait_batch_size=50
        )
        anime = AggregatedAnime(
            title="Example", sources=["AniList"], external_ids={"anilist": "1"}
        )
        progress = service.build_progress(anime, "voices")
        await service.ensure(anime, "voices", progress, requested_page=0)
        self.assertEqual(len(anime.voices), 141)
        self.assertEqual(len(anime.voices_by_source["AniList"]), 141)
        self.assertEqual(progress.portrait_cursor, 50)
        await service.ensure(anime, "voices", progress, requested_page=0)
        self.assertEqual(len(anime.voices), 141)
        self.assertEqual(source.manifest_calls, 1)

    async def test_manifest_total_is_exact_before_portrait_batches(self) -> None:
        source = _ManifestSource(count=5)
        service = PeopleProgressService(
            [source],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=2,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1"},
            sources=["AniList"],
        )
        progress = service.build_progress(anime, "voices")
        self.assertEqual(service.source_names(anime, "voices", progress), ["AniList"])

        service.start_manifest_loads(anime, "voices", progress)
        await service.settle_manifests(anime, "voices", progress)

        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 5)
        self.assertEqual(len(anime.voices), 5)
        self.assertFalse(any(p.portraits for p in anime.voices))
        self.assertEqual(progress.portrait_cursor, 0)

    async def test_portraits_are_enriched_in_global_batches(self) -> None:
        source = _ManifestSource(count=5)
        service = PeopleProgressService(
            [source],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=2,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1"},
            sources=["AniList"],
        )
        progress = service.build_progress(anime, "voices")

        await service.ensure(anime, "voices", progress, requested_page=1)
        self.assertEqual(progress.portrait_cursor, 2)
        self.assertEqual(len(anime.voices), 5)
        self.assertEqual(len([p for p in anime.voices if p.portraits]), 2)
        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 5)
        self.assertTrue(service.scope_has_more(progress, None))

        await service.ensure(anime, "voices", progress, requested_page=3)
        self.assertEqual(progress.portrait_cursor, 4)
        self.assertEqual(len(anime.voices), 5)
        self.assertEqual(len([p for p in anime.voices if p.portraits]), 4)
        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 5)

        await service.ensure(anime, "voices", progress, requested_page=5)
        self.assertEqual(progress.portrait_cursor, 5)
        self.assertEqual(len(anime.voices), 5)
        self.assertFalse(service.scope_has_more(progress, None))

    async def test_manifest_settle_waits_for_every_source_for_exact_union(self) -> None:
        release = asyncio.Event()
        fast = _NamedManifestSource("Fast", "anilist", "Same Person")
        slow = _NamedManifestSource("Slow", "mal", "Second Person", release)
        service = PeopleProgressService(
            [fast, slow],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=50,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1", "mal": "1"},
            sources=["Fast", "Slow"],
        )
        progress = service.build_progress(anime, "voices")
        service.start_manifest_loads(anime, "voices", progress)
        settle_task = asyncio.create_task(
            service.settle_manifests(anime, "voices", progress)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(fast.manifest_calls, 1)
        self.assertEqual(slow.manifest_calls, 1)
        self.assertFalse(settle_task.done())
        self.assertIsNone(service.scope_total_hint(anime, "voices", progress, None))

        release.set()
        await settle_task
        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 2)

    async def test_duplicate_people_across_sources_count_once(self) -> None:
        first = _NamedManifestSource("AniList", "anilist", "Same Person")
        second = _NamedManifestSource("MAL", "mal", "Same Person")
        service = PeopleProgressService(
            [first, second],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=50,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1", "mal": "1"},
            sources=["AniList", "MAL"],
        )
        progress = service.build_progress(anime, "voices")
        await service.settle_manifests(anime, "voices", progress)
        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 1)

    async def test_portrait_batch_size_is_global_across_sources(self) -> None:
        first = _ManifestSource(count=60)
        second = _ManifestSource(count=60)
        second.name = "MAL"
        second.source_key = "mal"
        second.people = [
            PersonCredit(
                name=f"Person {index}",
                external_ids={"mal": str(index)},
                portraits={"MAL": [f"https://mal.example/{index}.jpg"]},
            )
            for index in range(1, 61)
        ]
        service = PeopleProgressService(
            [first, second],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=50,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1", "mal": "1"},
            sources=["AniList", "MAL"],
        )
        progress = service.build_progress(anime, "voices")
        await service.ensure(anime, "voices", progress, requested_page=1)

        self.assertEqual(service.scope_total_hint(anime, "voices", progress, None), 60)
        self.assertEqual(progress.portrait_cursor, 50)
        self.assertEqual(len(anime.voices), 60)
        self.assertEqual(len([p for p in anime.voices if p.portraits]), 50)

    async def test_slow_portrait_only_enrichment_never_blocks_batch_cursor(
        self,
    ) -> None:
        release = asyncio.Event()
        authority = _ManifestSource(count=60)
        shikimori = _PortraitOnlySource(release)
        service = PeopleProgressService(
            [authority, shikimori],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=50,
        )
        anime = AggregatedAnime(
            title="Example",
            external_ids={"anilist": "1", "shikimori": "1"},
            sources=["AniList", "Shikimori"],
        )
        progress = service.build_progress(anime, "voices")

        await service.ensure(anime, "voices", progress, requested_page=1)
        await asyncio.sleep(0)

        self.assertEqual(progress.portrait_cursor, 50)
        self.assertEqual(len(anime.voices), 60)
        self.assertEqual(len([p for p in anime.voices if p.portraits]), 50)
        self.assertEqual(shikimori.calls, 1)
        self.assertTrue(progress.enrichment_tasks)

        release.set()
        await asyncio.gather(*list(progress.enrichment_tasks))
        self.assertFalse(progress.enrichment_tasks)

    async def test_progress_excludes_sources_without_confirmed_identity(self) -> None:
        source = _ManifestSource()
        service = PeopleProgressService(
            [source],
            _FakeAggregator(),  # type: ignore[arg-type]
            portrait_batch_size=2,
        )
        anime = AggregatedAnime(title="Example", external_ids={"mal": "1"})
        self.assertFalse(service.build_progress(anime, "voices").sources)


if __name__ == "__main__":
    unittest.main()
