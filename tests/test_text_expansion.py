from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bots.telegram.card.text_expansion import long_text_parts
from bots.telegram.card.ui.keyboards import description_keyboard
from bots.telegram.card.views.sections import (
    description_pages,
    source_section_pages,
    view_pages,
)
from core.models import AggregatedAnime


class LongTextPaginationTests(unittest.TestCase):
    def test_twelve_thousand_characters_become_three_inline_pages(self) -> None:
        settings = SimpleNamespace(telegram=SimpleNamespace(long_text_chunk_size=4000, long_text_include_ui=False))
        with patch(
            "bots.telegram.card.text_expansion.get_settings",
            return_value=settings,
        ):
            parts = long_text_parts("x" * 12000, locale="ru")
            self.assertEqual([len(part) for part in parts], [4000, 4000, 4000])
            self.assertEqual("".join(parts), "x" * 12000)

            anime = AggregatedAnime(
                title="Example",
                descriptions={"AniList": "x" * 12000},
            )
            description = description_pages(anime, "AniList", locale="ru")
            self.assertGreater(len(description), 1)
            self.assertEqual(len(description), 3)
            self.assertTrue(all(len(part.partition('\n\n')[2]) == 4000 for part in description))
            keyboard = description_keyboard(
                "token", "anilist", 0, len(description), locale="ru"
            )
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertIn("ad:token:anilist:1", callbacks)
            self.assertFalse(
                any(value.startswith("tx:") for value in callbacks if value)
            )

    def test_non_main_views_use_configured_limit(self) -> None:
        settings = SimpleNamespace(telegram=SimpleNamespace(long_text_chunk_size=4000, long_text_include_ui=False))
        anime = AggregatedAnime(
            title="Example",
            synonyms=["abc" * 2000],
            descriptions={"AniList": "x" * 12000},
        )
        with patch(
            "bots.telegram.card.text_expansion.get_settings",
            return_value=settings,
        ):
            for page in view_pages(anime, "titles", locale="ru"):
                self.assertLessEqual(len(page), 4000)
            for page in source_section_pages(anime, "titles", "AniList", locale="ru"):
                self.assertLessEqual(len(page), 4000)


if __name__ == "__main__":
    unittest.main()
