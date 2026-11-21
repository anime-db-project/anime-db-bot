from __future__ import annotations

import unittest
from unittest.mock import patch
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bots.telegram.card.text_expansion import (
    continuation_keyboard,
    long_text_parts,
)


class LongTextExpansionTests(unittest.TestCase):
    def test_twelve_thousand_characters_become_three_clicks(self) -> None:
        settings = SimpleNamespace(
            telegram=SimpleNamespace(long_text_chunk_size=4000)
        )
        with patch(
            "bots.telegram.card.text_expansion.get_settings",
            return_value=settings,
        ):
            parts = long_text_parts("x" * 12000, locale="ru")
        self.assertEqual([len(part) for part in parts], [4000, 4000, 4000])
        self.assertEqual("".join(parts), "x" * 12000)

    def test_more_button_preserves_original_navigation(self) -> None:
        base = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Источники", callback_data="av:t:description:0"),
            InlineKeyboardButton(text="Назад", callback_data="av:t:main:0"),
        ]])
        next_markup = continuation_keyboard(
            base, "token", "ad", "shikimori", 0, 1, 3, locale="ru"
        )
        self.assertIsNotNone(next_markup)
        assert next_markup is not None
        self.assertEqual(
            next_markup.inline_keyboard[0][0].callback_data,
            "tx:token:ad:shikimori:0:1",
        )
        self.assertEqual(next_markup.inline_keyboard[-1], base.inline_keyboard[0])
        self.assertIs(
            continuation_keyboard(
                base, "token", "ad", "shikimori", 0, 3, 3, locale="ru"
            ),
            base,
        )


if __name__ == "__main__":
    unittest.main()
