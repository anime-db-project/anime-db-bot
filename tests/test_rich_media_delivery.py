from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendRichMessage
from aiogram.types import BufferedInputFile, FSInputFile, InputRichMessage

from bots.telegram.rich_media import (
    RichMediaDelivery,
    _verify_public_host,
)
from core.settings import ROOT_DIR, RichMediaSettings

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 35


def _failure(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        method=SendRichMessage(
            chat_id=1, rich_message=InputRichMessage(html="<p>Test</p>")
        ),
        message=f"Bad Request: {message}",
    )


class RichMediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.one = InputRichMessage(
            html=(
                '<img src="https://example.org/a.jpg"/>'
                "<h2>Card</h2>"
                "<table><tr><td>Title</td></tr></table>"
                "<tg-button-row><tg-button>Details</tg-button></tg-button-row>"
            )
        )

    async def test_fallback_order_direct_url_ram(self) -> None:
        manager = RichMediaDelivery(
            RichMediaSettings(order=("direct", "url", "ram"))
        )
        manager._fetch = AsyncMock(return_value=JPEG)
        send = AsyncMock(
            side_effect=[
                _failure("RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND"),
                _failure("failed to get HTTP URL content"),
                object(),
            ]
        )
        await manager.deliver(send, self.one)
        self.assertEqual(send.await_count, 3)
        self.assertIn('src="https://example.org/a.jpg"', send.await_args_list[0].args[0].html)
        self.assertIn('src="tg://photo?id=adb_photo_0"', send.await_args_list[1].args[0].html)
        rich = send.await_args_list[2].args[0]
        self.assertIn('src="tg://photo?id=adb_upload_0"', rich.html)
        self.assertIsInstance(rich.media[0].media.media, BufferedInputFile)
        self.assertIn("<table>", rich.html)
        self.assertIn("<tg-button-row>", rich.html)

    async def test_all_media_fail_preserves_rich_table_and_buttons(self) -> None:
        manager = RichMediaDelivery(
            RichMediaSettings(order=("direct", "url", "ram", "disk"))
        )
        manager._fetch = AsyncMock(return_value=None)
        send = AsyncMock(
            side_effect=[
                _failure("RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND"),
                _failure("failed to get HTTP URL content"),
                object(),
            ]
        )
        await manager.deliver(send, self.one)
        self.assertEqual(send.await_count, 3)
        last = send.await_args.args[0]
        self.assertNotIn("<img", last.html or "")
        self.assertIn("<table>", last.html or "")
        self.assertIn("<tg-button-row>", last.html or "")
        self.assertIsNone(last.media)

    async def test_mixed_gallery_only_drops_unavailable_photos(self) -> None:
        manager = RichMediaDelivery(
            RichMediaSettings(order=("ram",), max_images=3)
        )
        rich = InputRichMessage(
            html=(
                "<tg-slideshow>"
                '<img src="https://example.org/good.jpg"/>'
                '<img src="https://example.org/broken.jpg"/>'
                "</tg-slideshow><table><tr><td>A</td></tr></table>"
            )
        )
        manager._fetch = AsyncMock(
            side_effect=lambda url, deadline: JPEG if url.endswith("good.jpg") else None
        )
        send = AsyncMock()
        await manager.deliver(send, rich)
        result = send.await_args.args[0]
        self.assertIn('src="tg://photo?id=adb_upload_0"', result.html or "")
        self.assertNotIn("broken.jpg", result.html or "")
        self.assertIn("<table>", result.html or "")
        self.assertEqual(len(result.media or []), 1)

    async def test_local_uploads_are_globally_bounded(self) -> None:
        manager = RichMediaDelivery(
            RichMediaSettings(order=("ram",), max_local_deliveries=2)
        )
        manager._fetch = AsyncMock(return_value=JPEG)
        active = 0
        peak = 0

        async def deliver_slowly(_rich: InputRichMessage) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.025)
            active -= 1

        await asyncio.gather(
            *(
                manager.deliver(
                    deliver_slowly,
                    InputRichMessage(
                        html=f'<img src="https://example.org/photo-{i}.jpg"/>'
                    ),
                )
                for i in range(12)
            )
        )
        self.assertEqual(peak, 2)

    async def test_ram_and_disk_can_be_disabled_even_if_in_order(self) -> None:
        manager = RichMediaDelivery(
            RichMediaSettings(
                order=("ram", "disk"), ram_enabled=False, disk_enabled=False
            )
        )
        manager._fetch = AsyncMock()
        send = AsyncMock()
        await manager.deliver(send, self.one)
        send.assert_awaited_once()
        self.assertNotIn("<img", send.await_args.args[0].html or "")
        manager._fetch.assert_not_awaited()

    async def test_disk_cache_persists_files_under_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rich-test-", dir=ROOT_DIR) as root:
            settings = RichMediaSettings(
                order=("disk",),
                disk_dir=Path(root) / "images",
                disk_cache_bytes=1024,
            )
            manager = RichMediaDelivery(settings)
            manager._fetch = AsyncMock(return_value=JPEG)
            send = AsyncMock()
            await manager.deliver(send, self.one)
            path = send.await_args.args[0].media[0].media.media.path
            self.assertIsInstance(send.await_args.args[0].media[0].media.media, FSInputFile)
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).resolve().is_relative_to(Path(root)))

            second = RichMediaDelivery(settings)
            second._fetch = AsyncMock()
            await second.deliver(AsyncMock(), self.one)
            second._fetch.assert_not_awaited()

    async def test_non_media_error_is_not_swallowed(self) -> None:
        manager = RichMediaDelivery(RichMediaSettings(order=("direct",)))
        send = AsyncMock(side_effect=_failure("RICH_MESSAGE_TABLE_COLS_TOO_MANY"))
        with self.assertRaises(TelegramBadRequest):
            await manager.deliver(send, self.one)
        send.assert_awaited_once()

    async def test_no_images_does_not_need_fallback(self) -> None:
        manager = RichMediaDelivery(RichMediaSettings())
        send = AsyncMock()
        await manager.deliver(send, InputRichMessage(html="<h2>Card</h2>"))
        send.assert_awaited_once()

    def test_rejects_non_public_image_urls(self) -> None:
        for url in (
            "http://example.com/a.png",
            "https://127.0.0.1/a.png",
            "https://192.168.1.1/a.png",
            "https://example.org:8080/a.png",
            "file:///etc/passwd",
        ):
            with self.assertRaises(ValueError):
                _verify_public_host(url)
