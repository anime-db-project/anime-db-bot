from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from bots.telegram.card.runtime.picker_entries import (
    labels_for_route,
    prepare_initial_picker,
    source_character_choices,
)
from bots.telegram.card.runtime.state import AnimeCardSession
from bots.telegram.pagination import card_route_callback
from core.models import AggregatedAnime, CharacterRecord, PersonCredit


class SourceScopedInitialPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_switching_voice_source_lists_real_names_before_page(self) -> None:
        portrait = PersonCredit(
            name="Voice Actress",
            portraits={"AniList": ["https://images.example/voice.jpg"]},
        )
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            voices=[portrait],
            voices_by_source={"AniList": [portrait]},
        )
        session = AnimeCardSession(anime=anime, owner_user_id=1)
        state = Mock()
        state.people_loads.get.return_value = None

        with (
            patch(
                "bots.telegram.people.loading.source_names",
                return_value=["AniList"],
            ),
            patch(
                "bots.telegram.people.loading.ensure_lazy_people",
                new_callable=AsyncMock,
            ) as ensure,
        ):
            choice = await prepare_initial_picker(
                state,
                "token",
                session,
                "ps~voices~anilist",
                Mock(),
                locale="en",
            )

        self.assertIsNotNone(choice)
        assert choice is not None
        route, labels = choice
        self.assertEqual(route, "gx~as~voices~anilist~0")
        self.assertEqual(labels, ["Voice Actress"])
        self.assertEqual(
            card_route_callback("token", route, 0),
            "as:token:voices:anilist:1",
        )
        self.assertEqual(ensure.await_args.kwargs["source"], "AniList")

    async def test_invalid_people_source_does_not_open_another_source(self) -> None:
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
        )
        session = AnimeCardSession(anime=anime, owner_user_id=1)
        state = Mock()
        state.people_loads.get.return_value = None
        with patch(
            "bots.telegram.people.loading.source_names",
            return_value=["AniList"],
        ):
            choice = await prepare_initial_picker(
                state, "token", session, "ps~voices~unknown", Mock(), locale="en"
            )
        self.assertIsNone(choice)

    async def test_selected_character_source_groups_unpictured_people(self) -> None:
        anime = AggregatedAnime(
            title="Example",
            sources=["Shikimori"],
            characters_by_source={
                "Shikimori": [
                    CharacterRecord(name="No portrait 1"),
                    CharacterRecord(name="No portrait 2"),
                    CharacterRecord(
                        name="Portrait character",
                        portraits={"Shikimori": ["https://images.example/char.png"]},
                    ),
                ]
            },
        )
        session = AnimeCardSession(
            anime=anime,
            owner_user_id=1,
            characters_loaded=True,
            character_key_by_source={"Shikimori": "shikimori"},
        )
        choice = source_character_choices(session, "shikimori", locale="ru")
        self.assertIsNotNone(choice)
        assert choice is not None
        route, labels = choice
        self.assertEqual(route, "gx~ac~shikimori~1")
        self.assertEqual(labels, ["Без портретов 1–2", "Portrait character"])
        self.assertEqual(card_route_callback("token", route, 0), "ac:token:shikimori:0")
        self.assertEqual(card_route_callback("token", route, 1), "ac:token:shikimori:1")
        self.assertIsNone(source_character_choices(session, "unknown", locale="ru"))

    async def test_unpictured_staff_appears_as_one_group_not_many_dead_links(
        self,
    ) -> None:
        people = [PersonCredit(name=f"Staff {n}") for n in range(5)]
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            staff=people,
            staff_by_source={"AniList": people},
        )
        session = AnimeCardSession(anime=anime, owner_user_id=1)
        labels = labels_for_route(session, "gx~av~staff~1", locale="ru")
        self.assertEqual(labels, ["Без портретов 1–5"])
        self.assertEqual(
            card_route_callback("token", "gx~av~staff~1", 0), "av:token:staff:0"
        )

    async def test_grouped_picker_real_ordinals_across_windows_and_sources(self):
        from bots.telegram.card.runtime.picker_entries import picker_display_entries
        from bots.telegram.pagination import card_picker_keyboard
        from bots.telegram.callbacks import PagePickerCallback
        from bots.telegram.people.views import _page_text

        people = [PersonCredit(name=f"Unpictured {i}") for i in range(40)] + [
            PersonCredit(
                name=f"Portrait {i}",
                portraits={"AniList": [f"https://example.org/{i}.jpg"]},
            )
            for i in range(30)
        ]
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            staff=people,
            staff_by_source={"AniList": people},
            voices=people,
            voices_by_source={"AniList": people},
        )
        session = AnimeCardSession(
            anime,
            1,
            staff_gallery_indices={
                None: list(range(40, 70)),
                "AniList": list(range(40, 70)),
            },
            voice_gallery_indices={
                None: list(range(40, 70)),
                "AniList": list(range(40, 70)),
            },
        )
        for section in ("staff", "voices"):
            for route in (f"gx~av~{section}~1", f"gx~as~{section}~anilist~1"):
                labels, spans = picker_display_entries(session, route, locale="ru")
                self.assertEqual(spans[0], (1, 40))
                self.assertEqual(spans[20], (60, 60))
                kb = card_picker_keyboard(
                    PagePickerCallback("cp", "token", route, 20, len(labels), 2),
                    locale="ru",
                    labels=labels,
                    record_ranges=spans,
                )
                first = kb.inline_keyboard[0][0]
                self.assertEqual(first.text, "✓ 60. Portrait 19")
                self.assertEqual(
                    first.callback_data, card_route_callback("token", route, 20)
                )
                self.assertIn(
                    "60–69/70", [b.text for r in kb.inline_keyboard for b in r]
                )
                kb = card_picker_keyboard(
                    PagePickerCallback("ci", "token", route, 0, len(labels), 0),
                    locale="ru",
                    labels=labels,
                    record_ranges=spans,
                    highlight_current=False,
                )
                self.assertEqual(kb.inline_keyboard[0][0].text, "Без портретов 1–40")
                self.assertEqual(kb.inline_keyboard[1][0].text, "41. Portrait 0")
                view = _page_text(
                    anime,
                    section,
                    "AniList" if "~as~" in route else None,
                    20,
                    locale="ru",
                    staff_indices=session.staff_gallery_indices,
                )
                self.assertEqual(view[2], 60)

    async def test_multiple_text_groups_and_screenshot_ranges(self):
        from bots.telegram.card.runtime.picker_entries import picker_display_entries
        from bots.telegram.pagination import card_picker_keyboard
        from bots.telegram.callbacks import PagePickerCallback

        people = [
            CharacterRecord(name="Unpictured " + str(i) + " " + "X" * 100)
            for i in range(80)
        ] + [
            CharacterRecord(
                name="Portrait", portraits={"AniList": ["https://example.org/p.jpg"]}
            )
        ]
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            characters_by_source={"AniList": people},
            screenshots_by_source={
                "AniList": [f"https://example.org/{i}.jpg" for i in range(19)]
            },
        )
        session = AnimeCardSession(
            anime,
            1,
            characters_loaded=True,
            character_key_by_source={"AniList": "anilist"},
        )
        route, labels = source_character_choices(session, "anilist", locale="ru")
        labels, spans = picker_display_entries(session, route, locale="ru")
        self.assertGreater(len(spans), 2)
        self.assertEqual(spans[-1], (81, 81))
        for left, right in zip(spans, spans[1:]):
            self.assertEqual(left[1] + 1, right[0])
        kb = card_picker_keyboard(
            PagePickerCallback("ci", "token", route, 0, len(labels), 0),
            locale="ru",
            labels=labels,
            record_ranges=spans,
            highlight_current=False,
        )
        self.assertEqual(kb.inline_keyboard[len(spans) - 1][0].text, "81. Portrait")
        for route in ("av~screenshots", "as~screenshots~anilist"):
            labels, spans = picker_display_entries(session, route, locale="ru")
            self.assertEqual(spans, [(1, 8), (9, 16), (17, 19)])
            kb = card_picker_keyboard(
                PagePickerCallback("cp", "token", route, 1, 3, 0),
                locale="ru",
                labels=labels,
                record_ranges=spans,
            )
            self.assertEqual(
                [r[0].text for r in kb.inline_keyboard[:3]],
                ["Скриншоты 1–8", "✓ Скриншоты 9–16", "Скриншоты 17–19"],
            )
            self.assertEqual(
                kb.inline_keyboard[1][0].callback_data,
                card_route_callback("token", route, 1),
            )


if __name__ == "__main__":
    unittest.main()
