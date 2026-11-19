from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.models import AggregatedAnime, SearchCandidate, SearchResultSet
from scripts.operator_console import (
    ConsoleError,
    OperatorConsole,
    _confirmed,
    _parse_source_id,
    build_parser,
    format_output,
)
from scripts import release_entry


class OperatorConsoleParserTests(unittest.TestCase):
    def test_release_launcher_exposes_both_console_modes(self) -> None:
        parser = release_entry.build_parser()
        self.assertEqual(parser.parse_args(["console"]).command, "console")
        self.assertEqual(parser.parse_args(["cli"]).command, "cli")

    def test_parser_exposes_bot_moderation_and_media_sections(self) -> None:
        parser = build_parser()
        cases = (
            ["ping"], ["users"], ["user", "42"],
            ["search", "frieren", "--source", "mal,anilist", "--limit", "5"],
            ["lookup", "mal:5114"], ["anime", "mal:5114", "--section", "episodes"],
            ["open", "1"], ["view", "related"], ["view", "episode", "--episode", "3"],
            ["view", "timestamps", "--episode", "1"],
            ["image", "https://example.com/frame.png"],
            ["ban", "42", "--seconds", "60", "--reason", "spam", "--yes"],
            ["unban", "42", "--yes"], ["language", "42", "ru"],
            ["mapping", "list"], ["mapping", "history", "--limit", "8"],
            ["mapping", "set", "accept", "mal", "1", "anilist", "2",
             "--reason", "verified", "--yes"],
            ["mapping", "delete", "mal", "1", "anilist", "2",
             "--reason", "invalid", "--yes"],
            ["config", "list"], ["config", "check"], ["config", "explain", "BOT_TOKEN"],
            ["source-health"], ["metrics"], ["image-providers"], ["logs"],
        )
        for command in cases:
            with self.subTest(command=command):
                self.assertIsNotNone(parser.parse_args(command).command)

    def test_language_positional_does_not_override_console_locale(self) -> None:
        args = build_parser().parse_args(["--locale", "ja", "language", "42", "ru"])
        self.assertEqual(args.locale, "ja")
        self.assertEqual(args.target_locale, "ru")

    def test_confirm_defaults_to_safe_in_noninteractive_mode(self) -> None:
        with self.assertRaises(ConsoleError):
            _confirmed("Change user state?", yes=False, interactive=False)
        _confirmed("Change user state?", yes=True, interactive=False)

    def test_unknown_direct_id_is_not_treated_as_valid(self) -> None:
        self.assertEqual(_parse_source_id("mal:5114"), ("mal", "5114"))
        self.assertIsNone(_parse_source_id("notarealsource:1"))

    def test_json_is_serializable_and_text_is_readable(self) -> None:
        payload = {"items": [SearchCandidate(title="Frieren", external_ids={"mal": "1"})]}
        self.assertIn("Frieren", format_output(payload))
        self.assertIn('"title": "Frieren"', format_output(payload, as_json=True))


class OperatorConsoleServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.candidate = SearchCandidate(title="Example", external_ids={"mal": "1"})
        self.anime = AggregatedAnime(title="Example", external_ids={"mal": "1"})
        self.context = SimpleNamespace(
            search_service=SimpleNamespace(
                collect=AsyncMock(return_value=SearchResultSet(items=[self.candidate]))
            ),
            direct_lookup=SimpleNamespace(
                lookup=AsyncMock(return_value=SearchResultSet(items=[self.candidate]))
            ),
            aggregator=SimpleNamespace(aggregate=AsyncMock(return_value=self.anime)),
            imdb_rating_service=SimpleNamespace(enrich=AsyncMock()),
            related_anime_service=SimpleNamespace(
                load_relations=AsyncMock(
                    return_value=SimpleNamespace(
                        items=[], source_names={}, errors={}
                    )
                ),
                load_recommendations=AsyncMock(
                    return_value=SimpleNamespace(
                        items=[], source_names={}, errors={}
                    )
                ),
            ),
            runtime_source_descriptors=Mock(return_value=[]),
            runtime_metrics=SimpleNamespace(window=Mock(return_value={})),
            source_health=SimpleNamespace(snapshot=Mock(return_value={})),
        )
        self.console = OperatorConsole(self.context)
        self.parser = build_parser()

    async def test_search_then_open_reuses_same_aggregation_service(self) -> None:
        result = await self.console.execute(self.parser.parse_args(["search", "Example"]))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["title"], "Example")
        card = await self.console.execute(self.parser.parse_args(["open", "1"]))
        self.assertIn("Example", card)
        self.context.aggregator.aggregate.assert_awaited_once_with(self.candidate)
        self.assertIs(self.console.selected, self.anime)

    async def test_anime_by_source_id_uses_direct_lookup(self) -> None:
        result = await self.console.execute(
            self.parser.parse_args(["anime", "mal:1", "--section", "titles"])
        )
        self.assertIn("external_ids", result)
        self.context.direct_lookup.lookup.assert_awaited_once_with(
            "1", source_keys=("mal",)
        )
        self.context.search_service.collect.assert_not_awaited()

    async def test_lookup_does_not_fetch_full_anime_card(self) -> None:
        result = await self.console.execute(self.parser.parse_args(["lookup", "mal:1"]))
        self.assertEqual(result["count"], 1)
        self.context.aggregator.aggregate.assert_not_awaited()

    async def test_view_requires_selection(self) -> None:
        with self.assertRaises(ConsoleError):
            await self.console.execute(self.parser.parse_args(["view", "related"]))

    async def test_related_and_recommendations_reuse_same_source_service(self) -> None:
        self.console.selected = self.anime
        related = await self.console.execute(self.parser.parse_args(["view", "related"]))
        recommended = await self.console.execute(
            self.parser.parse_args(["view", "recommendations"])
        )
        self.assertEqual(related["section"], "related")
        self.assertEqual(recommended["section"], "recommendations")
        self.context.related_anime_service.load_relations.assert_awaited_once()
        self.context.related_anime_service.load_recommendations.assert_awaited_once()

    async def test_background_process_status_is_distinct_from_live_metrics(self) -> None:
        with (
            patch("scripts.release_entry.runtime_home", return_value=Path("/tmp/bot")),
            patch("scripts.release_entry.current_pid", return_value=108),
        ):
            result = await self.console.execute(self.parser.parse_args(["status"]))
        self.assertEqual(result["supervisor_pid"], 108)

    async def test_locale_update_uses_shared_user_store(self) -> None:
        with patch(
            "services.users.settings.set_user_locale", return_value="ru"
        ) as setting:
            result = await self.console.execute(
                self.parser.parse_args(["language", "42", "ru"])
            )
        self.assertEqual(result, {"user_id": 42, "locale": "ru"})
        setting.assert_called_once_with(42, "ru")

    async def test_moderation_requires_confirmation(self) -> None:
        with self.assertRaises(ConsoleError):
            await self.console.execute(self.parser.parse_args(["ban", "42"]))

    async def test_ban_matches_telegram_storage_layer(self) -> None:
        with patch("services.users.bans.ban_user") as handler:
            await self.console.execute(
                self.parser.parse_args(
                    ["ban", "42", "--seconds", "60", "--reason", "spam", "--yes"]
                )
            )
        handler.assert_called_once_with(
            "telegram", 42, banned_by=0, duration_seconds=60, reason="spam"
        )

    async def test_local_image_file_uses_shared_recognition(self) -> None:
        self.context.image_search_service = SimpleNamespace(
            search=AsyncMock(return_value=SimpleNamespace(items=[], errors={}))
        )
        self.context.settings = SimpleNamespace(
            sources=SimpleNamespace(image_search_max_bytes=1024)
        )
        with TemporaryDirectory() as root:
            file = Path(root) / "frame.png"
            file.write_bytes(b"an image payload")
            result = await self.console.execute(
                self.parser.parse_args(["image", str(file)])
            )
        self.assertEqual(result["results"], [])
        input_image = self.context.image_search_service.search.await_args.args[0]
        self.assertEqual(input_image.content, b"an image payload")

    async def test_mapping_mutations_require_explicit_reason_and_consent(self) -> None:
        args = self.parser.parse_args(
            ["mapping", "set", "accept", "mal", "1", "anilist", "2",
             "--reason", "verified"]
        )
        with self.assertRaises(ConsoleError):
            await self.console.execute(args)

    async def test_config_list_only_returns_example_names(self) -> None:
        result = await self.console.execute(
            self.parser.parse_args(["config", "explain", "BOT_TOKEN"])
        )
        self.assertTrue(result["documented_in"])
        self.assertNotIn("value", result)


if __name__ == "__main__":
    unittest.main()
