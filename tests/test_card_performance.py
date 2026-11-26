from __future__ import annotations
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InputRichMessage
from bots.telegram.media_cache import TelegramMediaCache
from bots.telegram.rich_media import (
    RichMediaDelivery,
    deliver_rich_media,
    bind_remote_photos,
    _limit_photos,
)
from core.async_snapshot import AsyncSnapshotCache
from core.settings import get_settings, RichMediaSettings
from services.runtime.rate_limits import (
    UpstreamRateLimiter,
    background_upstream,
    BackgroundQuotaUnavailable,
)


class CardPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_singleflight_and_mutation_isolation(self):
        cache = AsyncSnapshotCache(entries=2, ttl=30, name="test")
        started = asyncio.Event()
        release = asyncio.Event()

        async def load():
            started.set()
            await release.wait()
            return {"values": [1]}

        loader = AsyncMock(side_effect=load)
        a = asyncio.create_task(cache.get("id", loader))
        await started.wait()
        b = asyncio.create_task(cache.get("id", loader))
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(a, b)
        first["values"].append(2)
        self.assertEqual(second, {"values": [1]})
        self.assertEqual(await cache.get("id", loader), second)
        loader.assert_awaited_once()
        self.assertFalse(cache.pending)

    async def test_snapshot_cancel_one_waiter_preserves_other(self):
        cache = AsyncSnapshotCache(entries=2, ttl=30, name="test")
        started = asyncio.Event()
        release = asyncio.Event()

        async def load():
            started.set()
            await release.wait()
            return [1]

        loader = AsyncMock(side_effect=load)
        a = asyncio.create_task(cache.get("id", loader))
        await started.wait()
        b = asyncio.create_task(cache.get("id", loader))
        await asyncio.sleep(0)
        a.cancel()
        await asyncio.gather(a, return_exceptions=True)
        release.set()
        self.assertEqual(await b, [1])
        loader.assert_awaited_once()

    async def test_photo_cap_applies_to_direct_and_bound_payload(self):
        manager = RichMediaDelivery(RichMediaSettings(max_images=2))
        rich = InputRichMessage(
            html="<tg-slideshow>"
            + "".join(f'<img src="https://example.org/{i}.jpg"/>' for i in range(6))
            + "</tg-slideshow><table><tr><td>Metadata</td></tr></table>"
        )
        send = AsyncMock()
        with patch(
            "bots.telegram.rich_media.get_rich_media_delivery", return_value=manager
        ):
            await deliver_rich_media(send, rich)
        delivered = send.await_args.args[0]
        self.assertEqual(delivered.html.count("<img"), 2)
        self.assertIn("Metadata", delivered.html)
        bound = _limit_photos(bind_remote_photos(rich), 2)
        self.assertEqual(len(bound.media), 2)
        self.assertEqual(bound.html.count("<img"), 2)

    async def test_slow_send_has_hard_rich_budget_and_keeps_text(self):
        manager = RichMediaDelivery(RichMediaSettings(total_timeout_seconds=0.5))
        rich = InputRichMessage(
            html='<img src="https://example.org/slow.jpg"/><table><tr><td>Metadata</td></tr></table>'
        )

        async def send(content):
            if "<img" in content.html:
                await asyncio.sleep(10)

        sender = AsyncMock(side_effect=send)
        with patch(
            "bots.telegram.rich_media.get_rich_media_delivery", return_value=manager
        ):
            await asyncio.wait_for(deliver_rich_media(sender, rich), timeout=0.8)
        self.assertEqual(sender.await_count, 2)
        self.assertNotIn("<img", sender.await_args.args[0].html)
        self.assertIn("Metadata", sender.await_args.args[0].html)

    async def test_duplicate_download_singleflight(self):
        manager = RichMediaDelivery(RichMediaSettings())
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(url, deadline):
            started.set()
            await release.wait()
            return b"image"

        deadline = asyncio.get_running_loop().time() + 2
        with patch.object(
            manager, "_fetch_inner", new=AsyncMock(side_effect=fetch)
        ) as inner:
            a = asyncio.create_task(manager._fetch("url", deadline))
            await started.wait()
            b = asyncio.create_task(manager._fetch("url", deadline))
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(await asyncio.gather(a, b), [b"image", b"image"])
            inner.assert_awaited_once()

    def _media_settings(self, **kwargs):
        return replace(
            get_settings().telegram,
            media_cache_enabled=True,
            media_cache_background_upload=True,
            media_cache_chat_id=-100123,
            media_cache_persistent=False,
            **kwargs,
        )

    async def test_foreground_priority_retains_previous_prefetch(self):
        cache = TelegramMediaCache(
            self._media_settings(media_cache_queue_size=8, media_cache_idle_seconds=0)
        )
        bot = SimpleNamespace(id=1)
        scope = ("chat", 1)
        urls = [f"https://example.org/{i}.jpg" for i in range(9)]
        cache.worker = asyncio.create_task(asyncio.sleep(10))
        try:
            cache.activate_prefetch(scope, "old")
            cache.enqueue(bot, urls[:3], scope=scope, group="old")
            cache.activate_prefetch(scope, "new")
            self.assertEqual(cache.queue.qsize(), 3)
            cache.enqueue(bot, urls[3:6], scope=scope, group="new")
            cache.enqueue(bot, [urls[6]], scope=scope, group="new")
            self.assertEqual(cache.queue.qsize(), 6)
            cache.enqueue(bot, [urls[6], urls[7]])
            cache.enqueue(bot, [urls[8]])
            self.assertEqual(cache.queue.qsize(), 8)
            # Even on a full queue, actual use can promote an old task.
            cache.enqueue(bot, [urls[0]])
            self.assertEqual(cache.queue.qsize(), 8)
            items = []
            while not cache.queue.empty():
                _, url = cache.queue.get_nowait()
                items.append(url)
                cache.queue.task_done()
            self.assertEqual(items[:3], [urls[0], urls[6], urls[7]])
            self.assertEqual(items[3], urls[3])
            self.assertEqual(set(items), set(urls[:8]))
            self.assertEqual(cache.stats["prefetch_deferred"], 1)
        finally:
            await cache.close()

    async def test_worker_waits_for_all_interactive_updates_then_drains_old_jobs(self):
        cache = TelegramMediaCache(
            self._media_settings(
                media_cache_queue_size=10,
                media_cache_batch_size=1,
                media_cache_idle_seconds=0.02,
            )
        )
        bot = SimpleNamespace(id=1)
        urls = [f"https://example.org/{i}.jpg" for i in range(3)]
        scope = ("chat", 1)
        with patch.object(cache, "stage", new=AsyncMock()) as stage:
            with cache.interactive_activity():
                cache.activate_prefetch(scope, "old")
                cache.enqueue(bot, urls[:1], scope=scope, group="old")
                with cache.interactive_activity():
                    cache.activate_prefetch(scope, "new")
                    cache.enqueue(bot, urls[1:2], scope=scope, group="new")
                    cache.enqueue(bot, urls[2:])
                    await asyncio.sleep(0)
                    stage.assert_not_awaited()
                await asyncio.sleep(0.03)
                stage.assert_not_awaited()
                self.assertEqual(cache.queue.qsize(), 3)
            stage.assert_not_awaited()
            await asyncio.wait_for(cache.queue.join(), 1)
            self.assertEqual(
                [call.args[1] for call in stage.await_args_list],
                [urls[2], urls[1], urls[0]],
            )
            self.assertEqual(cache.stats["idle_pauses"], 1)
        await cache.close()

    async def test_zero_idle_delay_allows_background_during_update(self):
        cache = TelegramMediaCache(
            self._media_settings(media_cache_idle_seconds=0, media_cache_batch_size=1)
        )
        bot = SimpleNamespace(id=1)
        with patch.object(cache, "stage", new=AsyncMock()) as stage:
            with cache.interactive_activity():
                cache.enqueue(bot, ["https://example.org/a.jpg"])
                await asyncio.wait_for(cache.queue.join(), 1)
                stage.assert_awaited_once()
        await cache.close()

    async def test_background_reserves_quota_for_interactive_requests(self):
        settings = replace(
            get_settings().rate_limits,
            upstream_per_source={"test": 4},
            upstream_per_operation={},
        )
        limiter = UpstreamRateLimiter(settings)
        with background_upstream():
            for _ in range(3):
                await limiter.acquire("test")
            with self.assertRaises(BackgroundQuotaUnavailable):
                await limiter.acquire("test")
        await asyncio.wait_for(limiter.acquire("test"), timeout=0.2)
