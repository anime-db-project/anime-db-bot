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

    async def test_unpictured_staff_all_have_distinct_stable_pages(self) -> None:
        people = [PersonCredit(name=f"Staff {n}") for n in range(5)]
        anime = AggregatedAnime(
            title="Example",
            sources=["AniList"],
            staff=people,
            staff_by_source={"AniList": people},
        )
        session = AnimeCardSession(anime=anime, owner_user_id=1)
        labels = labels_for_route(session, "gx~av~staff~0", locale="ru")
        self.assertEqual(labels, [f"Staff {n}" for n in range(5)])
        for i in range(5):
            self.assertEqual(
                card_route_callback("token", "gx~av~staff~0", i),
                f"av:token:staff:{i + 1}",
            )


if __name__ == "__main__":
    unittest.main()
