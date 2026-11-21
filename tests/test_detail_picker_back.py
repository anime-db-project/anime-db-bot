from __future__ import annotations

import unittest
from types import SimpleNamespace

from bots.telegram.card.features.characters import _keyboard as characters_keyboard
from bots.telegram.card.features.episodes import _episode_keyboard
from bots.telegram.card.features.related import related_keyboard
from bots.telegram.people.views import _keyboard as people_keyboard
from core.models import AggregatedAnime
from core.related_anime import AggregatedAnimeReference


class DetailPickerBackTests(unittest.TestCase):
    def test_character_details_return_to_named_character_list(self) -> None:
        keyboard = characters_keyboard(
            "token", [], None, 1, 2, 2, 2, locale="ru"
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "ci:token:characters",
        )

    def test_people_details_keep_the_selected_source(self) -> None:
        anime = AggregatedAnime(title="Anime", sources=["AniList"])
        keyboard = people_keyboard(
            "token",
            anime,
            "voices",
            "anilist",
            1,
            2,
            2,
            2,
            locale="ru",
            source_names=["AniList"],
            first_page=1,
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "ci:token:ps~voices~anilist",
        )

    def test_related_details_return_to_scoped_choices(self) -> None:
        items = [
            AggregatedAnimeReference(
                titles={"romaji": "Naruto"}, external_ids={"mal": "20"}
            )
        ]
        keyboard = related_keyboard(
            "token",
            "related",
            items,
            0,
            source_key="mal",
            source_names={"mal": "MyAnimeList"},
            locale="en",
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "ci:token:ar~related~mal",
        )

    def test_episode_details_return_to_episode_list(self) -> None:
        service = SimpleNamespace(display_name=lambda _: "MAL / Jikan")
        keyboard = _episode_keyboard(
            "token",
            backends=["jikan"],
            backend="jikan",
            segment_backends=[],
            segment_backend="all",
            view_page=0,
            total_episodes=12,
            has_prev=False,
            has_next=True,
            locale="ru",
            service=service,
            segment_service=SimpleNamespace(),
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "ci:token:episodes",
        )


if __name__ == "__main__":
    unittest.main()
