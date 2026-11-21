from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from bots.telegram.card.runtime.picker_entries import (
    labels_for_route,
    prepare_initial_picker,
)
from bots.telegram.card.runtime.state import AnimeCardSession
from bots.telegram.pagination import card_route_callback
from core.models import AggregatedAnime, PersonCredit


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
            title="Example", sources=["AniList"],
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

    async def test_unpictured_staff_appears_as_one_group_not_many_dead_links(self) -> None:
        people = [PersonCredit(name=f"Staff {n}") for n in range(5)]
        anime = AggregatedAnime(
            title="Example", sources=["AniList"],
            staff=people, staff_by_source={"AniList": people},
        )
        session = AnimeCardSession(anime=anime, owner_user_id=1)
        labels = labels_for_route(session, "gx~av~staff~1", locale="ru")
        self.assertEqual(labels, ["Без портретов (5)"])
        self.assertEqual(
            card_route_callback("token", "gx~av~staff~1", 0),
            "av:token:staff:0",
        )


if __name__ == "__main__":
    unittest.main()
