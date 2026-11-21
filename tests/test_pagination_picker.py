from __future__ import annotations

import unittest

from bots.telegram.callbacks import PagePickerCallback
from bots.telegram.pagination import (
    card_picker_keyboard,
    card_route_callback,
    search_picker_keyboard,
)


class PaginationPickerTests(unittest.TestCase):
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

        self.assertTrue(any(button.text == "✓ 13. Страница 13" for button in flattened))
        self.assertTrue(
            any(button.callback_data == "sr:token:17" for button in flattened)
        )
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, "sr:token:12")


if __name__ == "__main__":
    unittest.main()
