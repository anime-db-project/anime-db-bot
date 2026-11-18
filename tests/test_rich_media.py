from __future__ import annotations

import unittest

from aiogram.types import InputMediaPhoto, InputRichMessage, InputRichMessageMedia

from bots.telegram.rich_media import bind_remote_photos


class RichMediaBindingTests(unittest.TestCase):
    def test_remote_urls_are_bound_as_tg_photo_media(self) -> None:
        original = InputRichMessage(
            html=(
                "<tg-slideshow>"
                '<img src="https://example.org/a.jpg?foo=1&amp;bar=2"/>'
                '<img src="https://example.org/b.png"/>'
                '<img src="https://example.org/a.jpg?foo=1&amp;bar=2"/>'
                "</tg-slideshow><table><tr><td>Data</td></tr></table>"
                "<tg-button-row><tg-button>Info</tg-button></tg-button-row>"
            )
        )
        updated = bind_remote_photos(original)
        self.assertIsNot(updated, original)
        self.assertEqual(len(updated.media or []), 2)
        self.assertIn('src="tg://photo?id=adb_photo_0"', updated.html or "")
        self.assertIn('src="tg://photo?id=adb_photo_1"', updated.html or "")
        self.assertEqual(
            (updated.html or "").count('src="tg://photo?id=adb_photo_0"'), 2
        )
        self.assertEqual(
            updated.media[0].media.media,
            "https://example.org/a.jpg?foo=1&bar=2",
        )
        self.assertIn("<table>", updated.html or "")
        self.assertIn("<tg-button-row>", updated.html or "")
        self.assertIn("https://example.org/a.jpg?foo=1&amp;bar=2", original.html)

    def test_media_bindings_preserve_existing_media_ids(self) -> None:
        original = InputRichMessage(
            html='<img src="https://example.org/test.jpg"/>',
            media=[
                InputRichMessageMedia(
                    id="adb_photo_0", media=InputMediaPhoto(media="file_id_existing")
                )
            ],
        )
        bound = bind_remote_photos(original)
        self.assertIn('src="tg://photo?id=adb_photo_1"', bound.html or "")
        self.assertEqual(len(bound.media or []), 2)

    def test_no_remote_photos_returns_original(self) -> None:
        original = InputRichMessage(html='<img src="tg://photo?id=existing"/>')
        self.assertIs(bind_remote_photos(original), original)
        empty = InputRichMessage(html="<table><tr><td>T</td></tr></table>")
        self.assertIs(bind_remote_photos(empty), empty)
