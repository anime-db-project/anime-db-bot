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


    def test_all_literal_typed_settings_are_in_sample_files(self) -> None:
        import ast

        from core.settings import ROOT_DIR

        tree = ast.parse((ROOT_DIR / "core/settings.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {
                "_raw", "_int", "_float", "_bool", "_strict_int", "_strict_float",
                "_path", "_optional_path",
            } or len(node.args) < 2:
                continue
            key = node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
        documented = {name for group in example_groups().values() for name in group}
        fallback_aliases = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        self.assertEqual(names - documented - fallback_aliases, set())

    def test_bad_numeric_value_is_detected_without_printing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.d").mkdir()
            (root / ".env.d" / "runtime.env").write_text(
                "SEARCH_CACHE_MAX_ENTRIES=not-a-valid-secret-value\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Exception, "SEARCH_CACHE_MAX_ENTRIES"
            ) as caught:
                validate_config(root, environment={})
            self.assertNotIn("not-a-valid-secret-value", str(caught.exception))

    def test_bad_boolean_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.d").mkdir()
            (root / ".env.d" / "logging.env").write_text(
                "LOG_STRUCTURED=truue\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "LOG_STRUCTURED"):
                validate_config(root, environment={})

    def test_process_environment_overrides_project_value_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.d").mkdir()
            (root / ".env.d" / "runtime.env").write_text(
                "SEARCH_CACHE_MAX_ENTRIES=123\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(Exception, "SEARCH_CACHE_MAX_ENTRIES"):
                validate_config(
                    root, environment={"SEARCH_CACHE_MAX_ENTRIES": "broken"}
                )

    def test_duplicate_config_fails_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("BOT_TOKEN=a\nBOT_TOKEN=b\n")
            with self.assertRaisesRegex(Exception, "Duplicate"):
                validate_config(root)


if __name__ == "__main__":
    unittest.main()
