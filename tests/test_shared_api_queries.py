from __future__ import annotations

import unittest
from unittest.mock import Mock

from services.search.query_contract import (
    parse_direct_id, parse_scope, scoped_search_service,
)


class SharedApiSearchQueryTests(unittest.TestCase):
    def test_source_scoped_ids_remain_independent(self) -> None:
        self.assertEqual(parse_direct_id("mal:5114"), ("mal", "5114"))
        self.assertEqual(parse_direct_id("anilist:5114"), ("anilist", "5114"))
        self.assertNotEqual(
            parse_direct_id("mal:5114"),
            parse_direct_id("anilist:5114"),
        )
        self.assertEqual(parse_direct_id("myanimelist:42"), ("mal", "42"))
        self.assertEqual(parse_direct_id("42"), (None, "42"))
        self.assertIsNone(parse_direct_id("mal:not-numeric"))
        self.assertIsNone(parse_direct_id("unknown:1"))

    def test_source_scope_aliases_are_bounded(self) -> None:
        self.assertEqual(parse_scope("mal+anilist"), ("mal", "anilist"))
        self.assertIsNone(parse_scope("mal,unknown"))
        self.assertIn("shikimori", parse_scope("all"))

    def test_existing_default_service_is_reused(self) -> None:
        from core.settings import AppSettings
        from services.search.service import SearchService

        settings = AppSettings.from_env({}).search
        service = SearchService([Mock()], settings=settings)
        providers = {key: Mock() for key in settings.default_provider_keys}
        selection = tuple(str(key) for key in settings.default_provider_keys)
        self.assertIs(scoped_search_service(selection, service, providers), service)


if __name__ == "__main__":
    unittest.main()
