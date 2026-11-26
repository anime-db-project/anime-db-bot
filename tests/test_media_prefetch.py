from __future__ import annotations
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bots.telegram.card.media_prefetch import prefetch_gallery, enqueue_portraits
from bots.telegram.card.runtime.state import AnimeCardSession
from core.models import AggregatedAnime


class GalleryPrefetchTests(unittest.TestCase):
    def test_only_current_window_and_crossing_page_are_queued(self):
        session = AnimeCardSession(AggregatedAnime(title="Test"), 1)
        message = SimpleNamespace(bot=SimpleNamespace(id=12))
        rows = [[f"https://example.org/{i}.jpg"] for i in range(141)]
        with patch(
            "bots.telegram.card.media_prefetch.enqueue_portraits", return_value=True
        ) as enqueue:
            prefetch_gallery(message, session, "screenshots", None, rows)
            self.assertEqual(len(enqueue.call_args.args[1]), 50)
            prefetch_gallery(
                message, session, "screenshots", None, rows, index=40, visible_count=8
            )
            enqueue.assert_called_once()
            prefetch_gallery(
                message, session, "screenshots", None, rows, index=48, visible_count=8
            )
            self.assertEqual(enqueue.call_count, 2)
            self.assertEqual(enqueue.call_args.args[1], rows[50:100])
            prefetch_gallery(message, session, "screenshots", None, rows, index=120)
            self.assertEqual(enqueue.call_args.args[1], rows[100:])
            prefetch_gallery(message, session, "screenshots", "AniList", rows)
            self.assertEqual(enqueue.call_count, 4)

    def test_primary_images_precede_alternatives_and_dedupe(self):
        from collections import Counter
        from unittest.mock import Mock

        cache = Mock()
        cache.settings = SimpleNamespace(
            media_cache_enabled=True,
            media_cache_background_upload=True,
            media_cache_chat_id=-100123,
        )
        cache.stats = Counter()
        with patch("bots.telegram.media_cache.get_media_cache", return_value=cache):
            self.assertTrue(
                enqueue_portraits(
                    SimpleNamespace(id=1), [["one", "alt"], ["two", "alt"]]
                )
            )
        self.assertEqual(cache.enqueue.call_args.args[1], ["one", "two", "alt"])

    def test_queue_full_batch_can_be_retried(self):
        session = AnimeCardSession(AggregatedAnime(title="Test"), 1)
        message = SimpleNamespace(bot=SimpleNamespace(id=12))
        with patch(
            "bots.telegram.card.media_prefetch.enqueue_portraits",
            side_effect=[False, True],
        ) as enqueue:
            for _ in range(3):
                prefetch_gallery(message, session, "characters", None, [["one"]])
        self.assertEqual(enqueue.call_count, 2)
