from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.config_report import example_groups, validate_config


class ConfigReportTests(unittest.TestCase):
    def test_examples_list_groups_without_values(self) -> None:
        groups = example_groups()
        for expected in (
            "telegram.env",
            "sources.env",
            "runtime.env",
            "storage.env",
            "mappings.env",
            "rate-limits.env",
        ):
            self.assertIn(expected, groups)
        self.assertIn("HTTP_MAX_CONCURRENT_REQUESTS", groups["runtime.env"])
        self.assertIn("TELEGRAM_DENIED_MESSAGE", groups["telegram.env"])
        self.assertIn("USER_DEFAULT_LOCALE", groups["storage.env"])

    def test_unknown_keys_reported_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / ".env.example.d"
            sample.mkdir()
            (sample / "telegram.env").write_text(
                "BOT_TOKEN=\n", encoding="utf-8"
            )
            active = root / ".env.d"
            active.mkdir()
            (active / "telegram.env").write_text(
                "BOT_TOKEN=private-secret\nUNKNOWN_KEY=private-value\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_config(root), ["UNKNOWN_KEY"])

    def test_duplicate_config_fails_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("BOT_TOKEN=a\nBOT_TOKEN=b\n")
            with self.assertRaisesRegex(Exception, "Duplicate"):
                validate_config(root)


if __name__ == "__main__":
    unittest.main()
