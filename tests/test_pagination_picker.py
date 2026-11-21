from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bots.telegram.callbacks import PagePickerCallback
from bots.telegram.card.runtime.picker_entries import picker_parent_callback
from bots.telegram.card.ui.keyboards import description_selection_rows
from core.models import AggregatedAnime
from bots.telegram.pagination import (
    card_picker_keyboard,
    card_route_callback,
    picker_block_for_route,
    selection_page_size,
    indexed_selection_rows,
    search_picker_keyboard,
)


class PaginationPickerTests(unittest.TestCase):
    def test_route_specific_pagination_limits_with_global_fallback(self) -> None:
        settings = SimpleNamespace(
            telegram=SimpleNamespace(
                pagination_page_size=8,
                pagination_block_page_sizes={
                    "episodes": 3,
                    "wikipedia": 4,
                    "settings": 12,
                },
            )
        )
        with patch("bots.telegram.pagination.get_settings", return_value=settings):
            self.assertEqual(selection_page_size("episodes"), 3)
            self.assertEqual(selection_page_size("related"), 8)
            self.assertEqual(selection_page_size("wikipedia"), 4)
            self.assertEqual(selection_page_size("settings"), 12)
            self.assertEqual(picker_block_for_route("ap~jikan"), "episodes")
            self.assertEqual(picker_block_for_route("gx~as~voices~anilist~1"), "voices")
            self.assertEqual(picker_block_for_route("ar~recommendations~all"), "recommendations")
            picker = card_picker_keyboard(
                PagePickerCallback("cp", "token", "ap~jikan", 4, 24, 1),
                locale="ru",
            )
            labels = [
                button.text for row in picker.inline_keyboard
                for button in row if button.callback_data.startswith("ap:")
            ]
            self.assertEqual(labels, ["4. 4 серия", "✓ 5. 5 серия", "6. 6 серия"])

    def test_shared_selection_rows_use_configured_page_size(self) -> None:
        settings = SimpleNamespace(telegram=SimpleNamespace(pagination_page_size=3))
        with patch("bots.telegram.pagination.get_settings", return_value=settings):
            rows, page, total_pages = indexed_selection_rows(
                ["First", "Second", "Third", "Fourth", "Fifth"],
                window=1,
                destination=lambda index: f"select:{index}",
                change_window=lambda window: f"window:{window}",
            )
        self.assertEqual(page, 1)
        self.assertEqual(total_pages, 2)
        self.assertEqual([row[0].text for row in rows[:2]], ["4. Fourth", "5. Fifth"])
        self.assertEqual([row[0].callback_data for row in rows[:2]], ["select:3", "select:4"])
        self.assertEqual(rows[-1][0].callback_data, "window:0")
        self.assertEqual(rows[-1][1].text, "2/2")

    def test_source_picker_back_leads_to_its_parent(self) -> None:
        self.assertEqual(
            picker_parent_callback("token", "ar~related~mal"),
            "ci:token:related",
        )
        self.assertEqual(
            picker_parent_callback("token", "ap~jikan"),
            "av:token:main:0",
        )
        self.assertEqual(
            picker_parent_callback("token", "at~kitsu"),
            "av:token:trailers:0",
        )
        self.assertEqual(
            picker_parent_callback("token", "ad~shikimori"),
            "av:token:description:0",
        )

    def test_description_source_uses_picker_only_when_long(self) -> None:
        short = AggregatedAnime(
            title="Test", descriptions={"Shikimori": "A short synopsis."}
        )
        long = AggregatedAnime(
            title="Test", descriptions={"Shikimori": "This is a long synopsis. " * 4000}
        )
        short_row = description_selection_rows("token", short, locale="ru")
        long_row = description_selection_rows("token", long, locale="ru")
        self.assertEqual(
            short_row[0][0].callback_data, "ad:token:shikimori:0"
        )
        self.assertEqual(
            long_row[0][0].callback_data, "ci:token:ad~shikimori"
        )

    def test_picker_creates_only_visible_labels_for_large_page_totals(self) -> None:
        data = PagePickerCallback("cp", "token", "ap~jikan", 4321, 5000, 432)
        with patch(
            "bots.telegram.pagination._default_label",
            side_effect=lambda _, index, __: f"Episode {index + 1}",
        ) as formatter:
            keyboard = card_picker_keyboard(data, locale="ru")
        self.assertEqual(formatter.call_count, 10)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("4321. Episode 4321", labels)
        self.assertIn("4330. Episode 4330", labels)
        self.assertNotIn("4320. Episode 4320", labels)

    def test_card_picker_uses_named_selection_pages(self) -> None:
        data = PagePickerCallback("cp", "token", "ap~jikan", 32, 75, 3)
        keyboard = card_picker_keyboard(data, locale="ru")
        flattened = [button for row in keyboard.inline_keyboard for button in row]

        self.assertTrue(any(button.text == "31. 31 серия" for button in flattened))
        self.assertTrue(any(button.text == "✓ 33. 33 серия" for button in flattened))
        self.assertTrue(any(button.text == "40. 40 серия" for button in flattened))
        self.assertFalse(any(button.text.startswith("30.") for button in flattened))
        self.assertTrue(
            any(button.callback_data == "ap:token:jikan:32" for button in flattened)
        )
        self.assertTrue(
            any(
                button.callback_data == "cp:token:ap~jikan:32:75:2"
                for button in flattened
            )
        )
        self.assertTrue(
            any(
                button.callback_data == "cp:token:ap~jikan:32:75:4"
                for button in flattened
            )
        )
        self.assertEqual(keyboard.inline_keyboard[-1][0].text, "← Назад")
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data,
            "ap:token:jikan:32",
        )

    def test_episode_group_picker_offsets_local_index(self) -> None:
        route = "apg~kitsu~12"
        self.assertEqual(
            card_route_callback("token", route, 0),
            "ap:token:kitsu:12",
        )
        self.assertEqual(
            card_route_callback("token", route, 5),
            "ap:token:kitsu:17",
        )

    def test_related_picker_preserves_section_and_source(self) -> None:
        route = "ar~recommendations~anidb"
        self.assertEqual(
            card_route_callback("token", route, 4),
            "ar:token:recommendations~anidb:4",
        )

    def test_people_visible_position_maps_back_to_portrait_page(self) -> None:
        route = "pv~av~voices~3"
        self.assertEqual(card_route_callback("token", route, 0), "av:token:voices:0")
        self.assertEqual(card_route_callback("token", route, 2), "av:token:voices:0")
        self.assertEqual(card_route_callback("token", route, 3), "av:token:voices:1")
        self.assertEqual(card_route_callback("token", route, 9), "av:token:voices:7")

    def test_grouped_galleries_only_offer_one_unpictured_group(self) -> None:
        self.assertEqual(
            card_route_callback("token", "gx~av~voices~1", 0),
            "av:token:voices:0",
        )
        self.assertEqual(
            card_route_callback("token", "gx~av~voices~1", 2),
            "av:token:voices:2",
        )
        self.assertEqual(
            card_route_callback("token", "gx~ac~anilist~0", 0),
            "ac:token:anilist:1",
        )

    def test_source_id_route_opens_original_card_page(self) -> None:
        self.assertEqual(
            card_route_callback("token", "av~all_ids", 2),
            "av:token:all_ids:2",
        )

    def test_selector_back_does_not_loop_to_itself(self) -> None:
        from bots.telegram.card.runtime.picker_entries import (
            picker_parent_callback,
        )

        cases = {
            "ar~related~all": "av:token:main:0",
            "ar~related~mal": "ci:token:related",
            "ar~recommendations~all": "av:token:main:0",
            "ar~recommendations~anilist": "ci:token:recommendations",
            "ap~jikan": "av:token:main:0",
            "apg~jikan~20": "ci:token:episodes",
            "gx~as~voices~anilist~1": "ci:token:voices",
            "ps~voices~anilist": "ci:token:voices",
            "ps~staff~anilist": "ci:token:staff",
            "gx~ac~shikimori~1": "ci:token:characters",
            "at~mal": "av:token:trailers:0",
            "ad~anilist": "av:token:description:0",
        }
        for route, expected in cases.items():
            with self.subTest(route=route):
                self.assertEqual(picker_parent_callback("token", route), expected)

    def test_people_source_route_preserves_source_key(self) -> None:
        route = "pv~as~staff~anilist~2"
        self.assertEqual(
            card_route_callback("token", route, 2),
            "as:token:staff:anilist:1",
        )

    def test_search_picker_returns_to_existing_result_callbacks(self) -> None:
        data = PagePickerCallback("sp", "token", "sr", 12, 18, 1)
        keyboard = search_picker_keyboard(data, locale="ru")
        flattened = [button for row in keyboard.inline_keyboard for button in row]

        self.assertTrue(any(button.text == "✓ 13" for button in flattened))
        self.assertTrue(
            any(button.callback_data == "sr:token:17" for button in flattened)
        )
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, "sr:token:12")


if __name__ == "__main__":
    unittest.main()
