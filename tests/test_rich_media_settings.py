from __future__ import annotations

import unittest

from core.settings import AppSettings


class RichMediaConfigTests(unittest.TestCase):
    def test_defaults_are_bounded(self) -> None:
        settings = AppSettings.from_env({}).telegram.rich_media
        self.assertEqual(settings.order, ("direct", "url", "ram", "disk"))
        self.assertEqual(settings.max_downloads, 8)
        self.assertEqual(settings.max_image_bytes, 4194304)
        self.assertTrue(settings.disk_dir.as_posix().endswith("data/runtime/rich-media"))

    def test_custom_order_and_disabling_server_downloads(self) -> None:
        settings = AppSettings.from_env(
            {
                "RICH_MEDIA_ORDER": "url,direct,url,disk",
                "RICH_MEDIA_RAM_ENABLED": "false",
                "RICH_MEDIA_DISK_ENABLED": "false",
                "RICH_MEDIA_MAX_IMAGES": "2",
                "RICH_MEDIA_DISK_CACHE_BYTES": "2048",
            }
        ).telegram.rich_media
        self.assertEqual(settings.order, ("url", "direct", "disk"))
        self.assertFalse(settings.ram_enabled)
        self.assertFalse(settings.disk_enabled)
        self.assertEqual(settings.max_images, 2)
        self.assertEqual(settings.disk_cache_bytes, 2048)

    def test_bad_method_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AppSettings.from_env({"RICH_MEDIA_ORDER": "direct,ftp"})
