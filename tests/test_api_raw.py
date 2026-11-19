"""Unmerged source/capability API contracts (no upstream network)."""
from __future__ import annotations

import base64
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from api.http import create_app
from api.operations import ApiOperationError, execute_operation
from api.raw import execute_raw
from core.models import AnimeRecord, PersonCredit, SearchCandidate, SearchPage
from core.people_loading import PeopleManifest
from core.settings import AppSettings


READ_KEY = "r" * 32


def _source(key: str) -> SimpleNamespace:
    async def details(identity):
        return AnimeRecord(
            source=key,
            source_id=identity.get(key) or "",
            titles={"english": f"{key} title"},
            external_ids={key: identity.get(key) or ""},
        )

    return SimpleNamespace(
        source_key=key,
        name=key,
        get_details=AsyncMock(side_effect=details),
        load_voices_manifest=AsyncMock(
            return_value=PeopleManifest(
                people=[PersonCredit(name=f"{key} actor", roles=["voice"])]
            )
        ),
        load_voices=AsyncMock(
            return_value=[
                PersonCredit(
                    name=f"{key} actor",
                    portraits={key: ["https://example.com/portrait.png"]},
                )
            ]
        ),
        load_staff_manifest=AsyncMock(
            return_value=PeopleManifest(
                people=[PersonCredit(name=f"{key} director", roles=["director"])]
            )
        ),
        load_relations=AsyncMock(return_value=[{"source": key, "relation": "sequel"}]),
        load_characters=AsyncMock(return_value=[]),
    )


def fake_context() -> SimpleNamespace:
    mal = _source("mal")
    anilist = _source("anilist")
    kitsu = _source("kitsu")
    settings = AppSettings.from_env({
        "API_READ_KEY": READ_KEY,
    })
    search_mal = SimpleNamespace(search=AsyncMock(return_value=SearchPage(
        items=[SearchCandidate(title="MAL result", external_ids={"mal": "1"})],
        page=1, page_size=20, has_next=False,
    )))
    search_anilist = SimpleNamespace(search=AsyncMock(return_value=SearchPage(
        items=[SearchCandidate(
            title="AniList result", external_ids={"anilist": "2"}
        )],
        page=1, page_size=20, has_next=False,
    )))
    context = SimpleNamespace(
        settings=settings,
        sources=(mal, anilist, kitsu),
        search_provider_map={"mal": search_mal, "anilist": search_anilist},
        episode_service=SimpleNamespace(sources=()),
        episode_segment_service=SimpleNamespace(sources=()),
        trailer_service=SimpleNamespace(sources=()),
        anime_theme_service=SimpleNamespace(
            source=SimpleNamespace(
                backend_key="animethemes",
                supports_identity=lambda _: False,
            )
        ),
        image_search_service=SimpleNamespace(providers={}),
        identity_resolver=SimpleNamespace(
            mapping_resolver=SimpleNamespace(
                resolve=lambda identity: (
                    {"anilist": "2"} if identity.get("mal") == "1" else {}
                )
            )
        ),
    )
    return context


class RawSourceContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = fake_context()

    async def test_direct_id_never_aggregates(self) -> None:
        result = await execute_raw(
            self.context, "id", {"source": "mal", "id": "1"}
        )
        self.assertFalse(result["aggregated"])
        self.assertEqual(result["sources"]["mal"]["data"]["source_id"], "1")
        self.assertEqual(result["sources"]["mal"]["data"]["source"], "mal")
        self.context.sources[0].get_details.assert_not_awaited()
        self.context.sources[1].get_details.assert_not_awaited()

    async def test_identity_verification_is_opt_in(self) -> None:
        result = await execute_raw(
            self.context, "id", {"source": "mal", "id": "1", "verify": True}
        )
        self.assertTrue(result["sources"]["mal"]["data"]["exists"])
        self.context.sources[0].get_details.assert_awaited_once()

    async def test_all_sources_return_separate_records(self) -> None:
        result = await execute_raw(
            self.context, "metadata", {
                "source": "all",
                "ids": {"mal": "1", "anilist": "2"},
            }
        )
        self.assertEqual(result["sources"]["mal"]["data"]["source_id"], "1")
        self.assertEqual(result["sources"]["anilist"]["data"]["source_id"], "2")
        self.assertEqual(result["sources"]["kitsu"]["status"], "missing_id")
        self.assertEqual(
            self.context.sources[0].get_details.await_args.args[0].get("mal"), "1"
        )
        self.assertEqual(
            self.context.sources[1].get_details.await_args.args[0].get("anilist"), "2"
        )

    async def test_no_numeric_id_cross_source_inference(self) -> None:
        with self.assertRaisesRegex(ApiOperationError, "single id"):
            await execute_raw(
                self.context, "metadata", {"source": "all", "id": "1"}
            )
        for provider in self.context.sources:
            provider.get_details.assert_not_awaited()

    async def test_explicit_mapping_only_expands_verified_sources(self) -> None:
        result = await execute_raw(
            self.context, "metadata", {
                "source": "all", "ids": {"mal": "1"}, "resolve": True,
            }
        )
        self.assertEqual(result["identity"]["anilist"], "2")
        self.assertEqual(result["sources"]["anilist"]["status"], "ok")
        self.assertEqual(result["sources"]["kitsu"]["status"], "missing_id")

    async def test_staff_and_seiyuu_independent_lazy_manifests(self) -> None:
        result = await execute_raw(
            self.context, "seiyuu", {"source": "mal", "id": "1"}
        )
        person = result["sources"]["mal"]["data"]["people"][0]
        self.assertEqual(person["name"], "mal actor")
        self.assertEqual(person["portraits"], {})
        self.context.sources[0].load_staff_manifest.assert_not_awaited()
        self.context.sources[0].load_voices.assert_not_awaited()
        result = await execute_raw(
            self.context, "staff", {"source": "mal", "id": "1"}
        )
        self.assertEqual(
            result["sources"]["mal"]["data"]["people"][0]["name"],
            "mal director",
        )
        self.context.sources[0].get_details.assert_not_awaited()

    async def test_full_people_opt_in(self) -> None:
        result = await execute_raw(
            self.context, "voices", {
                "source": "mal", "id": "1", "detail": "full"
            }
        )
        person = result["sources"]["mal"]["data"]["people"][0]
        self.assertIn("mal", person["portraits"])
        self.context.sources[0].load_voices.assert_awaited_once()

    async def test_unsupported_source_capability_is_explicit(self) -> None:
        result = await execute_raw(
            self.context, "voices", {"source": "kitsu", "id": "4"}
        )
        self.assertEqual(result["sources"]["kitsu"]["status"], "unsupported")
        self.context.sources[2].load_voices.assert_not_awaited()

    async def test_search_uses_native_providers_not_merged_search(self) -> None:
        result = await execute_raw(
            self.context, "search", {"source": "all", "query": "test"}
        )
        self.assertIn("MAL result", str(result["sources"]["mal"]["data"]))
        self.assertIn("AniList result", str(result["sources"]["anilist"]["data"]))
        self.context.search_provider_map["mal"].search.assert_awaited_once()
        for source in self.context.sources:
            source.get_details.assert_not_awaited()

    async def test_search_errors_do_not_fail_other_sources(self) -> None:
        self.context.search_provider_map["mal"].search.side_effect = RuntimeError(
            "do not expose secret URL token=private"
        )
        result = await execute_raw(
            self.context, "search", {"query": "Frieren"}
        )
        self.assertEqual(result["sources"]["mal"]["status"], "error")
        self.assertEqual(result["sources"]["mal"]["error_type"], "RuntimeError")
        self.assertNotIn("private", str(result))
        self.assertEqual(result["sources"]["anilist"]["status"], "ok")

    async def test_bundle_invokes_only_requested_sections(self) -> None:
        result = await execute_raw(
            self.context, "bundle", {
                "ids": {"mal": "1", "anilist": "2"},
                "capabilities": ["metadata", "voices"],
            },
        )
        self.assertEqual(set(result["results"]), {"metadata", "voices"})
        self.context.sources[0].load_staff_manifest.assert_not_awaited()

    async def test_invalid_ids_are_rejected_before_requests(self) -> None:
        with self.assertRaises(ApiOperationError):
            await execute_raw(
                self.context, "metadata", {
                    "ids": "mal:1,mal:2", "source": "all"
                },
            )
        for source in self.context.sources:
            source.get_details.assert_not_awaited()

    async def test_capability_catalog_is_discoverable(self) -> None:
        result = await execute_raw(self.context, "catalog")
        self.assertIn("voices", result["capabilities"])
        self.assertIn("metadata", result["capabilities"])
        self.assertFalse(result["aggregated"])


class RawHttpAndGraphQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = fake_context()
        self.activation = patch(
            "api.operations.activate_app_context",
            side_effect=lambda *_: nullcontext(),
        )
        self.activation.start()
        self.client = TestClient(TestServer(create_app(context=self.context)))
        await self.client.start_server()
        self.headers = {"X-API-Key": READ_KEY}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.activation.stop()

    async def test_rest_direct_source_routes(self) -> None:
        response = await self.client.get(
            "/api/v1/raw/mal/1/voices", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        data = (await response.json())["data"]
        self.assertEqual(data["sources"]["mal"]["status"], "ok")

    async def test_rest_all_source_search(self) -> None:
        response = await self.client.get(
            "/api/v1/raw/search?source=all&query=Frieren", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        data = (await response.json())["data"]
        self.assertEqual(set(data["sources"]), {"mal", "anilist"})

    async def test_rest_post_can_use_explicit_identity_map(self) -> None:
        response = await self.client.post(
            "/api/v1/raw/query",
            headers=self.headers,
            json={
                "capability": "metadata", "source": "all",
                "ids": {"mal": "1", "anilist": "2"},
            },
        )
        self.assertEqual(response.status, 200)
        data = (await response.json())["data"]
        self.assertEqual(data["sources"]["anilist"]["status"], "ok")

    async def test_graphql_has_separate_seiyuu_and_search_queries(self) -> None:
        response = await self.client.post(
            "/graphql",
            headers=self.headers,
            json={
                "query": """
                    query {
                        rawSeiyuu(source:"mal", id:"1")
                        rawSearch(query:"Frieren", source:"anilist")
                        sourceCatalog
                    }
                """
            },
        )
        self.assertEqual(response.status, 200)
        data = (await response.json())["data"]
        self.assertEqual(
            data["rawSeiyuu"]["sources"]["mal"]["status"], "ok"
        )
        self.assertEqual(
            data["rawSearch"]["sources"]["anilist"]["status"], "ok"
        )
        self.assertIn("voices", data["sourceCatalog"]["capabilities"])

    async def test_rest_rejects_unqualified_numeric_id(self) -> None:
        response = await self.client.get(
            "/api/v1/raw/metadata?id=1&source=all", headers=self.headers
        )
        self.assertEqual(response.status, 400)

    async def test_raw_routes_remain_authenticated(self) -> None:
        response = await self.client.get("/api/v1/raw/search?query=Frieren")
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
