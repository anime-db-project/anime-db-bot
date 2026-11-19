from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core.settings import ConfigFileError
from scripts.console_config import inspect_key, update_key


class OperatorConfigurationTests(unittest.TestCase):
    def test_can_inspect_documented_key_without_exposing_token(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / ".env.d"
            active.mkdir()
            (active / "telegram.env").write_text(
                "BOT_TOKEN=test-private-token\n", encoding="utf-8"
            )
            result = inspect_key("BOT_TOKEN", root=root)
        self.assertTrue(result["configured"])
        self.assertEqual(result["value"], "<redacted>")
        self.assertNotIn("test-private-token", str(result))

    def test_writes_new_setting_to_matching_group_and_preserves_comments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = update_key("USER_DEFAULT_LOCALE", "ru", root=root)
            target = root / ".env.d" / "storage.env"
            self.assertTrue(result["changed"])
            self.assertTrue(result["restart_required"])
            self.assertIn("USER_DEFAULT_LOCALE=ru", target.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_updates_legacy_env_without_creating_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".env"
            legacy.write_text(
                "# Existing config\nUSER_DEFAULT_LOCALE=en\n", encoding="utf-8"
            )
            update_key("USER_DEFAULT_LOCALE", "de", root=root)
            text = legacy.read_text(encoding="utf-8")
            self.assertIn("# Existing config", text)
            self.assertIn("USER_DEFAULT_LOCALE=de", text)
            self.assertFalse((root / ".env.d" / "storage.env").exists())

    def test_rejects_invalid_scalar_without_editing_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "BOT_RESTART_MAX_ATTEMPTS"):
                update_key("BOT_RESTART_MAX_ATTEMPTS", "-200", root=root)
            self.assertFalse((root / ".env.d" / "lifecycle.env").exists())

    def test_rejects_unrecognized_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigFileError):
                update_key("UNDOCUMENTED_SECRET", "value", root=Path(tmp))

    def test_rejects_multiline_to_prevent_injected_env_options(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                update_key("USER_DEFAULT_LOCALE", "en\nBOT_TOKEN=fake", root=Path(tmp))

    def test_reset_deletes_only_selected_override(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / ".env.d"
            active.mkdir()
            file = active / "storage.env"
            file.write_text(
                "USER_DEFAULT_LOCALE=ru\nUSER_LOCALE_CACHE_MAX_ENTRIES=250\n",
                encoding="utf-8",
            )
            result = update_key("USER_DEFAULT_LOCALE", None, root=root)
            self.assertTrue(result["changed"])
            text = file.read_text(encoding="utf-8")
            self.assertNotIn("USER_DEFAULT_LOCALE=", text)
            self.assertIn("USER_LOCALE_CACHE_MAX_ENTRIES=250", text)

    def test_sensitive_connection_settings_are_masked(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / ".env.d"
            active.mkdir()
            (active / "runtime.env").write_text(
                "REDIS_URL=redis://secret:password@localhost:6379/0\n",
                encoding="utf-8",
            )
            result = inspect_key("REDIS_URL", root=root)
            self.assertEqual(result["value"], "<redacted>")
            self.assertNotIn("password", str(result))


if __name__ == "__main__":
    unittest.main()
