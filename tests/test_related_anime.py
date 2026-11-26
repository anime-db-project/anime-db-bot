from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bots.telegram.card.features.related import related_keyboard
from bots.telegram.card.runtime.service import _check_related_and_update_button
from bots.telegram.card.runtime.state import TelegramCardState
from bots.telegram.card.ui.keyboards import fallback_main_keyboard, navigation_keyboard
from bots.telegram.card.views.main import main_rich
from core.identity import AnimeIdentity
from core.models import AggregatedAnime
from core.related_anime import (
    AggregatedAnimeReference,
    AnimeReference,
    canonical_relation_kind,
)
from services.media.related import RelatedAnimeLoadResult, RelatedAnimeService
from sources.providers.anidb import AniDBSource
from sources.providers.anilist import AniListSource
from sources.providers.anisearch import AniSearchSource
from sources.providers.kitsu import KitsuSource
from sources.providers.mal import MALSource
from sources.providers.shikimori import ShikimoriSource


class _MappingResolver:
    def enrich(self, identity: AnimeIdentity) -> AnimeIdentity:
        result = identity.copy()
        if result.get("mal") == "20":
            result.merge_missing({"anilist": "200"})
        if result.get("anilist") == "200":
            result.merge_missing({"mal": "20"})
        if result.get("shikimori") == "2000":
            result.merge_missing({"mal": "20", "anilist": "200"})
        return result


class _Source:
    def __init__(
        self,
        name: str,
        source_key: str,
        *,
        relations: list[AnimeReference] | None = None,
        recommendations: list[AnimeReference] | None = None,
    ) -> None:
        self.name = name
        self.source_key = source_key
        self._relations = relations or []
        self._recommendations = recommendations or []

    async def load_relations(self, identity: AnimeIdentity) -> list[AnimeReference]:
        del identity
        return list(self._relations)

    async def load_recommendations(
        self, identity: AnimeIdentity
    ) -> list[AnimeReference]:
        del identity
        return list(self._recommendations)


class RelatedAnimeCoreTests(unittest.TestCase):
    def test_relation_aliases_are_canonical(self) -> None:
        self.assertEqual(canonical_relation_kind("Spin-off"), "spin_off")
        self.assertEqual(
            canonical_relation_kind("Alternative version"), "alternative_version"
        )
        self.assertEqual(canonical_relation_kind("PARENT_STORY"), "parent")


class RelatedAnimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_mapping_edges_merge_three_sources(self) -> None:
        service = RelatedAnimeService(
            (
                _Source(
                    "MAL (MyAnimeList) / Jikan",
                    "mal",
                    recommendations=[
                        AnimeReference(
                            source="MAL (MyAnimeList)",
                            titles={"romaji": "Target"},
                            external_ids={"mal": "20"},
                            rank=1,
                        )
                    ],
                ),
                _Source(
                    "AniList",
                    "anilist",
                    recommendations=[
                        AnimeReference(
                            source="AniList",
                            titles={"english": "Target English"},
                            external_ids={"anilist": "200"},
                            rank=2,
                        )
                    ],
                ),
                _Source(
                    "Shikimori",
                    "shikimori",
                    recommendations=[
                        AnimeReference(
                            source="Shikimori",
                            titles={"russian": "Цель"},
                            external_ids={"shikimori": "2000"},
                            rank=3,
                        )
                    ],
                ),
            ),
            _MappingResolver(),
        )
        result = await service.load_recommendations(
            AnimeIdentity({"mal": "1", "anilist": "1", "shikimori": "1"})
        )
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.external_ids["mal"], "20")
        self.assertEqual(item.external_ids["anilist"], "200")
        self.assertEqual(item.external_ids["shikimori"], "2000")
        self.assertEqual(item.sources, ["MAL (MyAnimeList)", "AniList", "Shikimori"])
        self.assertEqual(set(result.by_source), {"mal", "anilist", "shikimori"})
        self.assertEqual(result.source_names["mal"], "MAL (MyAnimeList)")
        self.assertEqual(len(result.by_source["mal"]), 1)
        self.assertEqual(result.by_source["mal"][0].sources, ["MAL (MyAnimeList)"])

    async def test_same_title_without_identity_evidence_does_not_merge(self) -> None:
        service = RelatedAnimeService(
            (
                _Source(
                    "MAL (MyAnimeList) / Jikan",
                    "mal",
                    recommendations=[
                        AnimeReference(
                            source="MAL (MyAnimeList)",
                            titles={"romaji": "Same title"},
                            external_ids={"mal": "20"},
                        )
                    ],
                ),
                _Source(
                    "AniList",
                    "anilist",
                    recommendations=[
                        AnimeReference(
                            source="AniList",
                            titles={"romaji": "Same title"},
                            external_ids={"anilist": "999"},
                        )
                    ],
                ),
            ),
            _MappingResolver(),
        )
        result = await service.load_recommendations(
            AnimeIdentity({"mal": "1", "anilist": "1"})
        )
        self.assertEqual(len(result.items), 2)

    async def test_id_only_reference_fetches_verified_title_and_real_poster(self):
        from core.models import AnimeRecord
        from unittest.mock import AsyncMock

        source = _Source(
            "AniList",
            "anilist",
            relations=[
                AnimeReference(
                    source="AniList",
                    external_ids={"anilist": "2248"},
                    posters=[
                        "https://shikimori.io/assets/globals/missing_original.jpg"
                    ],
                )
            ],
        )
        source.get_details = AsyncMock(
            return_value=AnimeRecord(
                source="AniList",
                source_id="2248",
                external_ids={"anilist": "2248"},
                titles={"english": "Sports Festival"},
                posters=["https://example.org/poster.jpg"],
                kind="special",
            )
        )
        service = RelatedAnimeService([source], _MappingResolver())
        result = await service.load_relations(AnimeIdentity({"anilist": "1"}))
        self.assertEqual(result.items[0].candidate.title, "Sports Festival")
        self.assertEqual(result.items[0].posters, ["https://example.org/poster.jpg"])
        self.assertEqual(
            result.by_source["anilist"][0].candidate.title, "Sports Festival"
        )

    async def test_current_anime_is_filtered_from_results(self) -> None:
        service = RelatedAnimeService(
            (
                _Source(
                    "AniList",
                    "anilist",
                    relations=[
                        AnimeReference(
                            source="AniList",
                            titles={"romaji": "Self"},
                            external_ids={"anilist": "1"},
                            relation="sequel",
                        )
                    ],
                ),
            ),
            _MappingResolver(),
        )
        result = await service.load_relations(AnimeIdentity({"anilist": "1"}))
        self.assertEqual(result.items, [])


class RelatedAnimeSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mal_relations_and_recommendations_are_lazy(self) -> None:
        source = MALSource.__new__(MALSource)
        source._settings = object()
        related_payload = {
            "related_anime": [
                {
                    "relation_type": "sequel",
                    "node": {
                        "id": 20,
                        "title": "Target",
                        "main_picture": {"large": "https://img/20.jpg"},
                    },
                }
            ]
        }
        rec_payload = {
            "recommendations": [
                {
                    "num_recommendations": 8,
                    "node": {"id": 30, "title": "Recommended"},
                }
            ]
        }
        with patch(
            "sources.providers.mal._get_raw",
            AsyncMock(side_effect=[related_payload, rec_payload]),
        ) as request:
            relations = await source.load_relations(AnimeIdentity({"mal": "1"}))
            recommendations = await source.load_recommendations(
                AnimeIdentity({"mal": "1"})
            )
        self.assertEqual(relations[0].relation, "sequel")
        self.assertEqual(relations[0].external_ids, {"mal": "20"})
        self.assertEqual(recommendations[0].external_ids, {"mal": "30"})
        self.assertEqual(request.await_count, 2)

    async def test_anilist_relations_keep_peer_mal_id(self) -> None:
        source = AniListSource()
        payload = {
            "Media": {
                "id": 1,
                "relations": {
                    "edges": [
                        {
                            "relationType": "PREQUEL",
                            "node": {
                                "id": 200,
                                "idMal": 20,
                                "type": "ANIME",
                                "title": {"romaji": "Target"},
                                "coverImage": {},
                            },
                        }
                    ]
                },
            }
        }
        with patch(
            "sources.providers.anilist.graphql_request",
            AsyncMock(return_value=payload),
        ):
            result = await source.load_relations(AnimeIdentity({"anilist": "1"}))
        self.assertEqual(result[0].relation, "prequel")
        self.assertEqual(result[0].external_ids, {"anilist": "200", "mal": "20"})

    async def test_shikimori_related_skips_manga_and_similar_is_capped(self) -> None:
        source = ShikimoriSource.__new__(ShikimoriSource)
        source._settings = object()
        related = [
            {"relation": "Adaptation", "manga": {"id": 1}, "anime": None},
            {
                "relation": "Sequel",
                "anime": {
                    "id": 20,
                    "name": "Target",
                    "russian": "Цель",
                    "image": {},
                },
            },
        ]
        similar = [
            {"id": index, "name": f"Anime {index}", "image": {}}
            for index in range(1, 50)
        ]
        with (
            patch(
                "sources.providers.shikimori._load_related_rest",
                AsyncMock(side_effect=[related, similar]),
            ),
            patch(
                "sources.providers.shikimori._load_peer_mal_ids",
                AsyncMock(side_effect=[{"20": "200"}, {}]),
            ),
        ):
            relations = await source.load_relations(AnimeIdentity({"shikimori": "1"}))
            recommendations = await source.load_recommendations(
                AnimeIdentity({"shikimori": "1"})
            )
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].relation, "sequel")
        self.assertEqual(relations[0].external_ids, {"shikimori": "20", "mal": "200"})
        self.assertEqual(len(recommendations), 30)

    async def test_kitsu_relations_keep_only_anime_destinations(self) -> None:
        source = KitsuSource.__new__(KitsuSource)
        source._settings = object()
        payload = {
            "data": {
                "type": "anime",
                "id": "1",
                "relationships": {
                    "mediaRelationships": {
                        "data": [
                            {"type": "mediaRelationships", "id": "10"},
                            {"type": "mediaRelationships", "id": "11"},
                        ]
                    }
                },
            },
            "included": [
                {
                    "type": "mediaRelationships",
                    "id": "10",
                    "attributes": {"role": "sequel"},
                    "relationships": {
                        "destination": {"data": {"type": "anime", "id": "2"}}
                    },
                },
                {
                    "type": "mediaRelationships",
                    "id": "11",
                    "attributes": {"role": "adaptation"},
                    "relationships": {
                        "destination": {"data": {"type": "manga", "id": "3"}}
                    },
                },
                {
                    "type": "anime",
                    "id": "2",
                    "attributes": {
                        "titles": {"en_jp": "Target"},
                        "canonicalTitle": "Target",
                        "posterImage": {"large": "https://img/2.jpg"},
                    },
                },
                {"type": "manga", "id": "3", "attributes": {"canonicalTitle": "Manga"}},
            ],
        }
        with patch(
            "sources.providers.kitsu._request_json",
            AsyncMock(return_value=payload),
        ):
            result = await source.load_relations(AnimeIdentity({"kitsu": "1"}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].external_ids, {"kitsu": "2"})
        self.assertEqual(result[0].relation, "sequel")
        self.assertEqual(result[0].titles["romaji"], "Target")

    async def test_anidb_uses_related_and_similar_anime_blocks(self) -> None:
        source = AniDBSource.__new__(AniDBSource)
        source._settings = object()
        root = ET.fromstring(
            """
            <anime id="1">
              <relatedanime>
                <anime id="2" type="Sequel">Target sequel</anime>
              </relatedanime>
              <similaranime>
                <anime id="3" approval="5" total="9">Lower</anime>
                <anime id="4" approval="12" total="20">Higher</anime>
              </similaranime>
            </anime>
            """
        )
        source._load_root = AsyncMock(return_value=root)
        relations = await source.load_relations(AnimeIdentity({"anidb": "1"}))
        recommendations = await source.load_recommendations(
            AnimeIdentity({"anidb": "1"})
        )
        self.assertEqual(relations[0].external_ids, {"anidb": "2"})
        self.assertEqual(relations[0].relation, "Sequel")
        self.assertEqual(
            [item.external_ids["anidb"] for item in recommendations], ["4", "3"]
        )

    async def test_anisearch_relations_use_related_anime_map(self) -> None:
        source = AniSearchSource.__new__(AniSearchSource)
        source._settings = object()
        source._payload = AsyncMock(
            return_value={"related": {"anime": {"20": "Side Story", "30": "Summary"}}}
        )
        result = await source.load_relations(AnimeIdentity({"anisearch": "1"}))
        self.assertEqual(
            [item.external_ids["anisearch"] for item in result], ["20", "30"]
        )
        self.assertEqual(result[0].relation, "Side Story")


class RelatedAnimeUiTests(unittest.TestCase):
    def test_main_card_keeps_discovery_actions_out_of_rich_html(self) -> None:
        anime = AggregatedAnime(title="Test", external_ids={"mal": "1"})
        rich = main_rich("token", anime, "Test", locale="ru")
        self.assertNotIn('data="ci:token:related"', rich.html)
        self.assertNotIn('data="ci:token:recommendations"', rich.html)

    def test_main_navigation_uses_inline_discovery_buttons(self) -> None:
        anime = AggregatedAnime(title="Test", external_ids={"mal": "1"})
        keyboard = navigation_keyboard(
            "token", anime, "main", 0, 1, locale="ru", show_related=True
        )
        self.assertIsNotNone(keyboard)
        assert keyboard is not None
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(
            callbacks,
            [
                "wlcard:token",
                "av:token:wikipedia:0",
                "ci:token:related",
                "ci:token:recommendations",
            ],
        )

    def test_fallback_main_keyboard_has_both_discovery_buttons(self) -> None:
        keyboard = fallback_main_keyboard("token", locale="ru", show_related=True)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("ci:token:related", callbacks)
        self.assertIn("ci:token:recommendations", callbacks)

    def test_related_is_hidden_by_default(self) -> None:
        anime = AggregatedAnime(title="Standalone", external_ids={"mal": "1"})
        for keyboard in (
            navigation_keyboard("token", anime, "main", 0, 1, locale="en"),
            fallback_main_keyboard("token", locale="en"),
        ):
            self.assertIsNotNone(keyboard)
            assert keyboard is not None
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertNotIn("ci:token:related", callbacks)
            self.assertIn("ci:token:recommendations", callbacks)

    def test_empty_related_disappears_without_hiding_recommendations(self) -> None:
        anime = AggregatedAnime(title="Standalone", external_ids={"mal": "1"})
        for keyboard in (
            navigation_keyboard(
                "token", anime, "main", 0, 1, locale="en", show_related=False
            ),
            fallback_main_keyboard("token", locale="en", show_related=False),
        ):
            self.assertIsNotNone(keyboard)
            assert keyboard is not None
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertNotIn("ci:token:related", callbacks)
            self.assertIn("ci:token:recommendations", callbacks)

    def test_related_keyboard_uses_item_callbacks_and_pagination(self) -> None:
        items = [
            __import__(
                "core.related_anime", fromlist=["AggregatedAnimeReference"]
            ).AggregatedAnimeReference(
                titles={"romaji": f"Title {index}"},
                external_ids={"mal": str(index)},
                relations=["sequel"],
            )
            for index in range(1, 8)
        ]
        keyboard = related_keyboard(
            "token",
            "related",
            items,
            0,
            source_key="all",
            source_names={"mal": "MAL", "anilist": "AniList"},
            locale="ru",
        )
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("ari:token:related~all:0", callbacks)
        self.assertIn("ci:token:ar~related~mal", callbacks)
        self.assertIn("ci:token:ar~related~anilist", callbacks)
        self.assertIn("ci:token:related", callbacks)
        self.assertTrue(
            any(value.startswith("cp:token:ar~related~all:") for value in callbacks)
        )


class RelatedAnimeAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _setup() -> tuple[TelegramCardState, str]:
        state = TelegramCardState.create(session_ttl_seconds=600)
        token = state.remember_anime(
            AggregatedAnime(title="Test", external_ids={"mal": "1"}),
            owner_user_id=42,
        )
        return state, token

    async def test_related_details_replace_existing_message_and_retire_old_session(
        self,
    ):
        from bots.telegram.card.features.related import send_related_item

        state, old_token = self._setup()
        session = state.anime_sessions[old_token]
        session.related_loaded = True
        session.related_items = [
            AggregatedAnimeReference(
                titles={"romaji": "New title"}, external_ids={"mal": "2"}
            )
        ]
        replacement = AggregatedAnime(title="New title", external_ids={"mal": "2"})
        context = SimpleNamespace(
            aggregator=SimpleNamespace(aggregate=AsyncMock(return_value=replacement)),
            imdb_rating_service=SimpleNamespace(enrich=AsyncMock()),
            related_anime_service=SimpleNamespace(
                load_relations=AsyncMock(return_value=RelatedAnimeLoadResult())
            ),
        )
        message = SimpleNamespace(
            answer=AsyncMock(),
            answer_rich=AsyncMock(),
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
        )
        with (
            patch(
                "bots.telegram.card.features.related.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_user_locale", return_value="en"
            ),
        ):
            await send_related_item(
                message,
                session,
                state,
                section="related",
                source_key="all",
                index=0,
                owner_user_id=42,
            )
            await __import__("asyncio").gather(*tuple(state.background_tasks))
        message.edit_text.assert_awaited_once()
        message.answer.assert_not_awaited()
        message.answer_rich.assert_not_awaited()
        self.assertIsNone(state.anime_sessions.get(old_token))
        self.assertEqual(len(state.anime_sessions), 1)

    async def test_nested_discovery_depth_back_and_stale_callbacks(self):
        import asyncio
        from bots.telegram.card.features.related import (
            send_related_item,
            return_from_related,
            edit_related_view,
        )
        from bots.telegram.card.views.renderer import edit_anime_view
        from bots.telegram.card.ui.keyboards import fallback_main_keyboard
        from bots.telegram.card.runtime.service import _check_related_and_update_button
        from bots.telegram.inline_mode import CARD_PREFIXES
        from core.settings import get_settings
        from dataclasses import replace
        from core.models import SearchCandidate

        state, root_token = self._setup()
        root = state.anime_sessions[root_token]
        root_progress = SimpleNamespace(marker="saved")
        state.people_loads[f"{root_token}:staff"] = root_progress

        def populate(session, number):
            items = [
                AggregatedAnimeReference(
                    titles={"romaji": f"Title {number + 1}"},
                    external_ids={"mal": str(number + 1)},
                ),
                AggregatedAnimeReference(
                    titles={"romaji": f"Title {number + 2}"},
                    external_ids={"mal": str(number + 2)},
                ),
            ]
            session.related_loaded = True
            session.related_items = items
            session.related_by_source = {"mal": items}
            session.related_source_names = {"mal": "MAL"}
            session.recommendations_loaded = True
            session.recommendation_items = items
            session.recommendation_by_source = {"mal": items}
            session.recommendation_source_names = {"mal": "MAL"}

        async def aggregate(candidate):
            return AggregatedAnime(
                title=candidate.title, external_ids=dict(candidate.external_ids)
            )

        context = SimpleNamespace(
            aggregator=SimpleNamespace(aggregate=AsyncMock(side_effect=aggregate)),
            imdb_rating_service=SimpleNamespace(enrich=AsyncMock()),
            related_anime_service=SimpleNamespace(
                load_relations=AsyncMock(return_value=RelatedAnimeLoadResult())
            ),
        )
        message = SimpleNamespace(
            answer=AsyncMock(),
            answer_rich=AsyncMock(),
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
        )
        settings = get_settings()
        cfg = replace(
            settings,
            telegram=replace(settings.telegram, related_navigation_max_depth=10),
        )
        self.assertIn("ab", CARD_PREFIXES)

        def buttons(markup):
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        def last_markup():
            return message.edit_text.await_args.kwargs["reply_markup"]

        def active():
            self.assertEqual(len(state.anime_sessions), 1)
            token = next(iter(state.anime_sessions))
            return token, state.anime_sessions[token]

        with (
            patch(
                "bots.telegram.card.features.related.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_user_locale", return_value="en"
            ),
            patch("bots.telegram.card.features.related.get_settings", return_value=cfg),
        ):
            token, session = active()
            original_sessions = [root]
            previous_tokens = [root_token]
            for depth in range(10):
                populate(session, depth * 2 + 10)
                section = "related" if depth % 2 == 0 else "recommendations"
                source = "mal" if depth % 2 == 0 else "all"
                self.assertTrue(
                    await send_related_item(
                        message,
                        session,
                        state,
                        section=section,
                        source_key=source,
                        index=1,
                        owner_user_id=42,
                    )
                )
                await asyncio.gather(*tuple(state.background_tasks))
                token, session = active()
                self.assertEqual(session.related_depth, depth + 1)
                self.assertEqual(
                    last_markup().inline_keyboard[-1][0].callback_data, f"ab:{token}"
                )
                self.assertIsNone(state.anime_sessions.get(previous_tokens[-1]))
                original_sessions.append(session)
                previous_tokens.append(token)
            populate(session, 99)
            for section in ("related", "recommendations"):
                await edit_related_view(
                    message, token, session, section=section, page=1, locale="en"
                )
                self.assertFalse(
                    any((b or "").startswith("ari:") for b in buttons(last_markup()))
                )
                self.assertFalse(
                    await send_related_item(
                        message,
                        session,
                        state,
                        section=section,
                        source_key="all",
                        index=1,
                        owner_user_id=42,
                    )
                )
            self.assertEqual(context.aggregator.aggregate.await_count, 10)
            # Background discovery must keep the history button as the bottom row.
            context.related_anime_service.load_relations.return_value = (
                RelatedAnimeLoadResult(items=session.related_items)
            )
            await _check_related_and_update_button(
                message, token, session, state, locale="en", plain_fallback=False
            )
            self.assertEqual(
                message.edit_reply_markup.await_args.kwargs["reply_markup"]
                .inline_keyboard[-1][0]
                .callback_data,
                f"ab:{token}",
            )
            self.assertEqual(
                fallback_main_keyboard(token, locale="en", related_back=True)
                .inline_keyboard[-1][0]
                .callback_data,
                f"ab:{token}",
            )
            for depth in range(10, 0, -1):
                old_token = token
                self.assertTrue(
                    await return_from_related(
                        message, token, session, state, locale="en"
                    )
                )
                token, session = active()
                self.assertIs(session, original_sessions[depth - 1])
                self.assertEqual(session.related_depth, depth - 1)
                self.assertNotIn(token, previous_tokens)
                self.assertIsNone(state.anime_sessions.get(old_token))
                section = "related" if (depth - 1) % 2 == 0 else "recommendations"
                source = "mal" if (depth - 1) % 2 == 0 else "all"
                self.assertIn(
                    f"ari:{token}:{section}~{source}:1", buttons(last_markup())
                )
                await edit_anime_view(
                    message,
                    token,
                    session.anime,
                    "main",
                    0,
                    locale="en",
                    related_back=session.related_return is not None,
                )
                self.assertEqual(f"ab:{token}" in buttons(last_markup()), depth > 1)
            self.assertIs(state.people_loads.get(f"{token}:staff"), root_progress)
            self.assertFalse(
                await return_from_related(message, token, session, state, locale="en")
            )
            self.assertEqual(context.aggregator.aggregate.await_count, 10)
        message.answer.assert_not_awaited()
        message.answer_rich.assert_not_awaited()

    async def test_unlimited_discovery_allows_transition_beyond_fifty_levels(self):
        import asyncio
        from dataclasses import replace
        from bots.telegram.card.features.related import (
            edit_related_view,
            send_related_item,
            return_from_related,
        )
        from bots.telegram.card.runtime.state import (
            AnimeCardSession,
            RelatedReturnFrame,
        )
        from core.settings import get_settings

        state, token = self._setup()
        session = state.anime_sessions[token]
        for i in range(51):
            parent = AnimeCardSession(
                AggregatedAnime(title=f"Parent {i}"),
                42,
                related_return=session.related_return,
            )
            session.related_return = RelatedReturnFrame(parent, "related", "all", 0)
        self.assertEqual(session.related_depth, 51)
        self.assertTrue(session.can_open_related(0))
        self.assertFalse(session.can_open_related(50))
        item = AggregatedAnimeReference(
            titles={"romaji": "Next"}, external_ids={"mal": "2"}
        )
        session.related_loaded = session.recommendations_loaded = True
        session.related_items = session.recommendation_items = [item]
        context = SimpleNamespace(
            aggregator=SimpleNamespace(
                aggregate=AsyncMock(
                    return_value=AggregatedAnime(
                        title="Next", external_ids={"mal": "2"}
                    )
                )
            ),
            imdb_rating_service=SimpleNamespace(enrich=AsyncMock()),
            related_anime_service=SimpleNamespace(
                load_relations=AsyncMock(return_value=RelatedAnimeLoadResult())
            ),
        )
        message = SimpleNamespace(
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            answer=AsyncMock(),
            answer_rich=AsyncMock(),
        )
        settings = get_settings()
        cfg = replace(
            settings,
            telegram=replace(settings.telegram, related_navigation_max_depth=0),
        )
        with (
            patch(
                "bots.telegram.card.features.related.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_app_context",
                return_value=context,
            ),
            patch(
                "bots.telegram.card.runtime.service.get_user_locale", return_value="en"
            ),
            patch("bots.telegram.card.features.related.get_settings", return_value=cfg),
        ):
            for section in ("related", "recommendations"):
                await edit_related_view(
                    message, token, session, section=section, page=0, locale="en"
                )
                buttons = [
                    b.callback_data
                    for row in message.edit_text.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for b in row
                ]
                self.assertIn(f"ari:{token}:{section}~all:0", buttons)
            self.assertTrue(
                await send_related_item(
                    message,
                    session,
                    state,
                    section="recommendations",
                    source_key="all",
                    index=0,
                    owner_user_id=42,
                )
            )
            await asyncio.gather(*tuple(state.background_tasks))
            child_token = next(iter(state.anime_sessions))
            child = state.anime_sessions[child_token]
            self.assertEqual(child.related_depth, 52)
            self.assertTrue(
                await return_from_related(
                    message, child_token, child, state, locale="en"
                )
            )
            self.assertIs(
                state.anime_sessions[next(iter(state.anime_sessions))], session
            )
            self.assertEqual(session.related_depth, 51)
        message.answer.assert_not_awaited()
        message.answer_rich.assert_not_awaited()

    async def test_relations_present_reveal_main_button(self) -> None:
        state, token = self._setup()
        session = state.anime_sessions[token]
        result = RelatedAnimeLoadResult(
            items=[AggregatedAnimeReference(external_ids={"mal": "2"})]
        )
        service = SimpleNamespace(load_relations=AsyncMock(return_value=result))
        message = SimpleNamespace(edit_reply_markup=AsyncMock())
        with patch(
            "bots.telegram.card.runtime.service.get_app_context",
            return_value=SimpleNamespace(related_anime_service=service),
        ):
            await _check_related_and_update_button(
                message, token, session, state, locale="en", plain_fallback=False
            )
        self.assertTrue(session.related_loaded)
        self.assertEqual(session.related_items, result.items)
        message.edit_reply_markup.assert_awaited_once()
        markup = message.edit_reply_markup.await_args.kwargs["reply_markup"]
        actions = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        self.assertIn(f"ci:{token}:related", actions)
        self.assertIn(f"ci:{token}:recommendations", actions)
        service.load_relations.assert_awaited_once()

    async def test_empty_or_failed_results_do_not_show_button(self) -> None:
        for result in (
            RelatedAnimeLoadResult(),
            RelatedAnimeLoadResult(errors={"MAL": "TimeoutError"}),
        ):
            state, token = self._setup()
            session = state.anime_sessions[token]
            service = SimpleNamespace(load_relations=AsyncMock(return_value=result))
            message = SimpleNamespace(edit_reply_markup=AsyncMock())
            with patch(
                "bots.telegram.card.runtime.service.get_app_context",
                return_value=SimpleNamespace(related_anime_service=service),
            ):
                await _check_related_and_update_button(
                    message,
                    token,
                    session,
                    state,
                    locale="en",
                    plain_fallback=False,
                )
            self.assertTrue(session.related_loaded)
            message.edit_reply_markup.assert_not_awaited()

    async def test_not_editing_a_card_after_navigation_away(self) -> None:
        state, token = self._setup()
        session = state.anime_sessions[token]
        session.main_view_active = False
        result = RelatedAnimeLoadResult(
            items=[AggregatedAnimeReference(external_ids={"mal": "2"})]
        )
        service = SimpleNamespace(load_relations=AsyncMock(return_value=result))
        message = SimpleNamespace(edit_reply_markup=AsyncMock())
        with patch(
            "bots.telegram.card.runtime.service.get_app_context",
            return_value=SimpleNamespace(related_anime_service=service),
        ):
            await _check_related_and_update_button(
                message, token, session, state, locale="en", plain_fallback=False
            )
        self.assertTrue(session.related_loaded)
        message.edit_reply_markup.assert_not_awaited()

    async def test_plain_fallback_keeps_full_menu(self) -> None:
        state, token = self._setup()
        session = state.anime_sessions[token]
        result = RelatedAnimeLoadResult(
            items=[AggregatedAnimeReference(external_ids={"mal": "2"})]
        )
        service = SimpleNamespace(load_relations=AsyncMock(return_value=result))
        message = SimpleNamespace(edit_reply_markup=AsyncMock())
        with patch(
            "bots.telegram.card.runtime.service.get_app_context",
            return_value=SimpleNamespace(related_anime_service=service),
        ):
            await _check_related_and_update_button(
                message, token, session, state, locale="en", plain_fallback=True
            )
        markup = message.edit_reply_markup.await_args.kwargs["reply_markup"]
        actions = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        self.assertIn(f"ci:{token}:related", actions)
        self.assertIn(f"av:{token}:description:0", actions)


if __name__ == "__main__":
    unittest.main()
