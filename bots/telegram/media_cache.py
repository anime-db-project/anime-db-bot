from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter
from functools import lru_cache
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto

from core.cache import BoundedTTLCache
from core.media_urls import usable_image_url
from core.settings import get_settings
from core.sqlite import managed_sqlite_connection
from services.users.settings import get_user_settings_store


def valid_url(url):
    if not isinstance(url, str) or len(url) > 4096 or not usable_image_url(url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and len(url) <= 4096
    )


class MediaIndex:
    """Persistent index, scoped to a single bot ID; never stores image bytes."""

    def __init__(self, repository):
        self.repository = repository
        with managed_sqlite_connection(repository._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_media_cache(
                bot_id BIGINT NOT NULL,url TEXT NOT NULL,file_id TEXT NOT NULL,
                digest TEXT,created_at BIGINT NOT NULL,chat_id BIGINT,message_id BIGINT,
                PRIMARY KEY(bot_id,url))""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS telegram_media_cache_digest ON telegram_media_cache(bot_id,digest)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS telegram_media_cache_age ON telegram_media_cache(bot_id,created_at)"
            )

    def lookup(self, bot_id, url, ttl):
        with managed_sqlite_connection(self.repository._connect()) as db:
            row = db.execute(
                "SELECT file_id,created_at FROM telegram_media_cache WHERE bot_id=? AND url=?",
                (bot_id, url),
            ).fetchone()
            if row and (not ttl or time.time() - row[1] < ttl):
                return row[0]
            return None

    def by_digest(self, bot_id, digest, ttl):
        with managed_sqlite_connection(self.repository._connect()) as db:
            row = db.execute(
                "SELECT file_id,chat_id,message_id,created_at FROM telegram_media_cache WHERE bot_id=? AND digest=? ORDER BY created_at DESC LIMIT 1",
                (bot_id, digest),
            ).fetchone()
            if row and (not ttl or time.time() - row[3] < ttl):
                return row[:3]
            return None

    def save(
        self,
        bot_id,
        url,
        file_id,
        limit,
        ttl,
        digest=None,
        chat_id=None,
        message_id=None,
    ):
        with managed_sqlite_connection(self.repository._connect()) as db:
            db.execute(
                """INSERT INTO telegram_media_cache VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(bot_id,url) DO UPDATE SET file_id=excluded.file_id,
                digest=CASE WHEN telegram_media_cache.file_id=excluded.file_id THEN COALESCE(excluded.digest,telegram_media_cache.digest) ELSE excluded.digest END,created_at=excluded.created_at,
                chat_id=COALESCE(excluded.chat_id,telegram_media_cache.chat_id),
                message_id=COALESCE(excluded.message_id,telegram_media_cache.message_id)""",
                (bot_id, url, file_id, digest, int(time.time()), chat_id, message_id),
            )
            if ttl:
                db.execute(
                    "DELETE FROM telegram_media_cache WHERE bot_id=? AND created_at<?",
                    (bot_id, int(time.time()) - ttl),
                )
            count = db.execute(
                "SELECT COUNT(*) FROM telegram_media_cache WHERE bot_id=?", (bot_id,)
            ).fetchone()[0]
            if count > limit:
                db.execute(
                    """DELETE FROM telegram_media_cache WHERE bot_id=? AND url IN
                    (SELECT url FROM telegram_media_cache WHERE bot_id=? ORDER BY created_at,url LIMIT ?)""",
                    (bot_id, bot_id, count - limit),
                )

    def forget(self, bot_id, url):
        with managed_sqlite_connection(self.repository._connect()) as db:
            row = db.execute(
                "SELECT file_id FROM telegram_media_cache WHERE bot_id=? AND url=?",
                (bot_id, url),
            ).fetchone()
            if row:
                db.execute(
                    "DELETE FROM telegram_media_cache WHERE bot_id=? AND file_id=?",
                    (bot_id, row[0]),
                )

    def counts(self, bot_id):
        with managed_sqlite_connection(self.repository._connect()) as db:
            return db.execute(
                "SELECT COUNT(*),COUNT(DISTINCT file_id) FROM telegram_media_cache WHERE bot_id=?",
                (bot_id,),
            ).fetchone()


@lru_cache(maxsize=4)
def _index(repository):
    return MediaIndex(repository)


class TelegramMediaCache:
    def __init__(self, settings):
        self.settings = settings
        self.hot = BoundedTTLCache(
            max_entries=4096,
            ttl_seconds=min(settings.media_cache_ttl_seconds or 604800, 604800),
            name="telegram_photo_ids",
        )
        self.stats = Counter()
        self.queue = asyncio.Queue(maxsize=settings.media_cache_queue_size)
        self.queued = set()
        self.prefetch_owners: dict[tuple, tuple] = {}
        self.prefetch_groups = BoundedTTLCache(
            max_entries=1024, ttl_seconds=3600, name="media_prefetch_scopes"
        )
        self.worker = None
        self.stage_lock = asyncio.Lock()
        self.digests = BoundedTTLCache(
            max_entries=4096,
            ttl_seconds=min(settings.media_cache_ttl_seconds or 604800, 604800),
            name="telegram_media_digest",
        )
        self.upload_slots = asyncio.Semaphore(
            settings.media_cache_max_concurrent_uploads
        )
        self.pace = asyncio.Lock()
        self.next_send = 0.0
        self.total_upload_seconds = 0.0

    def index(self):
        return _index(get_user_settings_store())

    async def lookup(self, bot_id, url):
        if not self.settings.media_cache_enabled or not valid_url(url):
            return None
        key = (bot_id, url)
        value = self.hot.get(key)
        if not value and self.settings.media_cache_persistent:
            try:
                value = await asyncio.to_thread(
                    lambda: self.index().lookup(
                        bot_id, url, self.settings.media_cache_ttl_seconds
                    )
                )
            except Exception:
                logging.debug("Telegram media index unavailable", exc_info=True)
            if value:
                self.hot.set(key, value)
        self.stats["hits" if value else "misses"] += 1
        return value

    async def remember(self, bot_id, url, file_id, **metadata):
        if not self.settings.media_cache_enabled or not valid_url(url) or not file_id:
            return
        self.hot.set((bot_id, url), file_id)
        if metadata.get("digest"):
            self.digests.set(
                (bot_id, metadata["digest"]),
                (file_id, metadata.get("chat_id"), metadata.get("message_id")),
            )
        if self.settings.media_cache_persistent:
            try:
                await asyncio.to_thread(
                    lambda: self.index().save(
                        bot_id,
                        url,
                        file_id,
                        self.settings.media_cache_max_entries,
                        self.settings.media_cache_ttl_seconds,
                        **metadata,
                    )
                )
            except Exception:
                logging.debug("Telegram media index write failed", exc_info=True)

    async def forget(self, bot_id, url):
        # Invalidate aliases as well: they can share the rejected file_id.
        # Clear process-local aliases; persistent aliases are invalidated by file_id.
        self.hot.clear()
        self.digests.clear()
        if self.settings.media_cache_persistent:
            try:
                await asyncio.to_thread(lambda: self.index().forget(bot_id, url))
            except Exception:
                logging.debug("Telegram media invalidation failed", exc_info=True)

    async def find_digest(self, bot_id, digest):
        alias = self.digests.get((bot_id, digest))
        if not alias and self.settings.media_cache_persistent:
            try:
                alias = await asyncio.to_thread(
                    lambda: self.index().by_digest(
                        bot_id, digest, self.settings.media_cache_ttl_seconds
                    )
                )
            except Exception:
                logging.debug("Media digest lookup failed", exc_info=True)
        return alias

    async def _paced(self, send, deadline, cost=1):
        async with self.pace:
            loop = asyncio.get_running_loop()
            for attempt in range(2):
                delay = max(0, self.next_send - loop.time())
                if loop.time() + delay >= deadline:
                    return None
                if delay:
                    await asyncio.sleep(delay)
                self.next_send = (
                    loop.time() + self.settings.media_cache_interval_seconds * cost
                )
                try:
                    async with asyncio.timeout_at(deadline):
                        return await send()
                except TelegramRetryAfter as exc:
                    self.stats["rate_limited"] += 1
                    self.next_send = max(
                        self.next_send, loop.time() + exc.retry_after + 0.25
                    )
                    if attempt or self.next_send >= deadline:
                        return None
        return None

    async def stage(self, bot, url, deadline):
        s = self.settings
        if (
            not s.media_cache_enabled
            or s.media_cache_chat_id >= 0
            or not valid_url(url)
        ):
            return None
        existing = await self.lookup(bot.id, url)
        if existing:
            return existing
        # Bound the number of in-flight downloads and serialize uploads by
        # content hash (also avoids duplicate uploads for different URLs).
        try:
            async with asyncio.timeout_at(deadline):
                async with self.stage_lock, self.upload_slots:
                    return await self._stage_one(bot, url, deadline)
        except TimeoutError:
            return None

    async def _stage_one(self, bot, url, deadline):
        from bots.telegram.rich_media import (
            get_rich_media_delivery,
            _extension,
            _verify_public_host,
        )

        s = self.settings
        manager = get_rich_media_delivery()
        digest = None
        payload = None
        start = time.monotonic()
        try:
            async with asyncio.timeout_at(deadline):
                existing = await self.lookup(bot.id, url)
                if existing:
                    return existing
                if s.media_cache_deduplication and (
                    manager.settings.ram_enabled or manager.settings.disk_enabled
                ):
                    payload = await manager._fetch(url, deadline)
                    if payload:
                        digest = hashlib.sha256(payload).hexdigest()
                existing = await self.lookup(bot.id, url)
                if existing:
                    return existing
                if digest:
                    alias = await self.find_digest(bot.id, digest)
                    if alias:
                        await self.remember(
                            bot.id,
                            url,
                            alias[0],
                            digest=digest,
                            chat_id=alias[1],
                            message_id=alias[2],
                        )
                        self.stats["deduplicated"] += 1
                        return alias[0]
                attempted_remote = False
                order = manager.settings.order
                # A content hash is trustworthy only for the bytes actually
                # uploaded. When deduplication has already downloaded a file,
                # prefer the configured local strategies for channel staging.
                if payload:
                    order = tuple(m for m in order if m in {"ram", "disk"}) + tuple(
                        m for m in order if m not in {"ram", "disk"}
                    )
                for mode in order:
                    if mode in {"direct", "url"}:
                        if attempted_remote:
                            continue
                        attempted_remote = True
                        try:
                            await asyncio.to_thread(_verify_public_host, url)
                        except (ValueError, OSError):
                            continue
                        file = url
                    elif mode == "ram" and manager.settings.ram_enabled:
                        file = (
                            BufferedInputFile(
                                payload,
                                filename="cache." + (_extension(payload) or "jpg"),
                            )
                            if payload
                            else await manager.prepare_photo_file(url, mode, deadline)
                        )
                    elif mode == "disk" and manager.settings.disk_enabled:
                        file = await manager.prepare_photo_file(url, mode, deadline)
                    else:
                        continue
                    if file is None:
                        continue
                    try:
                        sent = await self._paced(
                            lambda: bot.send_photo(
                                s.media_cache_chat_id,
                                file,
                                disable_notification=True,
                                request_timeout=10,
                            ),
                            deadline,
                        )
                        if sent is None:
                            break
                        if not sent.photo:
                            continue
                        photo = max(sent.photo, key=lambda p: p.width * p.height)
                        await self.remember(
                            bot.id,
                            url,
                            photo.file_id,
                            digest=digest if mode == "ram" and payload else None,
                            chat_id=s.media_cache_chat_id,
                            message_id=sent.message_id,
                        )
                        self.stats["uploads"] += 1
                        self.total_upload_seconds += time.monotonic() - start
                        if not s.media_cache_keep_messages:
                            try:
                                await bot.delete_message(
                                    s.media_cache_chat_id,
                                    sent.message_id,
                                    request_timeout=3,
                                )
                            except Exception:
                                logging.debug(
                                    "Cache message cleanup failed",
                                    exc_info=True,
                                )
                        return photo.file_id
                    except Exception:
                        logging.debug("Channel media fallback: %s", mode, exc_info=True)
        except Exception:
            logging.debug("Channel staging failed", exc_info=True)
        self.stats["errors"] += 1
        return None

    def _reorder_queue(self, *, drop=None, front=()):
        kept = []
        preferred = set(front)
        while not self.queue.empty():
            bot, url = self.queue.get_nowait()
            key = (bot.id, url)
            if drop is not None and drop(key):
                self.queue.task_done()
                self.queued.discard(key)
                self.prefetch_owners.pop(key, None)
                self.stats["queue_obsolete"] += 1
            else:
                kept.append((bot, url))
        kept.sort(key=lambda item: 0 if (item[0].id, item[1]) in preferred else 1)
        for item in kept:
            self.queue.put_nowait(item)
            self.queue.task_done()

    def cancel_prefetch(self, scope):
        if scope is None:
            return
        self.prefetch_groups.pop(scope)
        self._reorder_queue(
            drop=lambda key: self.prefetch_owners.get(key, (None, None))[0] == scope
        )

    def activate_prefetch(self, scope, group):
        if scope is None:
            return False
        if self.prefetch_groups.get(scope) == group:
            return False
        self.cancel_prefetch(scope)
        self.prefetch_groups.set(scope, group)
        return True

    def enqueue(self, bot, urls, *, scope=None, group=None):
        s = self.settings
        if (
            not s.media_cache_enabled
            or not s.media_cache_background_upload
            or s.media_cache_chat_id >= 0
        ):
            return
        if scope is not None and self.prefetch_groups.get(scope) != group:
            return
        priority = scope is None
        front = []
        for url in urls:
            key = (bot.id, url)
            if not valid_url(url) or self.hot.get(key):
                continue
            if key in self.queued:
                old = self.prefetch_owners.get(key)
                if priority or (old is not None and old != (scope, group)):
                    self.prefetch_owners.pop(key, None)
                if priority:
                    front.append(key)
                continue
            if self.queue.full() and priority:
                # Evict one oldest speculative entry, never another actual view.
                evicted = False

                def drop_one(key):
                    nonlocal evicted
                    if not evicted and key in self.prefetch_owners:
                        evicted = True
                        return True
                    return False

                self._reorder_queue(drop=drop_one)
            if self.queue.full():
                self.stats["queue_dropped"] += 1
                break
            self.queue.put_nowait((bot, url))
            self.queued.add(key)
            if scope is not None:
                self.prefetch_owners[key] = (scope, group)
            if priority:
                front.append(key)
        if front:
            self._reorder_queue(front=front)
        if self.queued and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self._work())

    def _wanted(self, bot, url):
        owner = self.prefetch_owners.get((bot.id, url))
        return owner is None or self.prefetch_groups.get(owner[0]) == owner[1]

    async def _batch(self, bot, urls, deadline):
        """Album uploads use exact locally prepared bytes; rejected albums
        return to the single-photo fallback path."""
        from bots.telegram.rich_media import get_rich_media_delivery, _extension

        manager = get_rich_media_delivery()
        s = self.settings
        if not manager.settings.ram_enabled or "ram" not in manager.settings.order:
            return
        groups = {}
        start = time.monotonic()
        async with asyncio.timeout_at(deadline):
            async with self.stage_lock:
                for url in urls:
                    if not self._wanted(bot, url) or await self.lookup(bot.id, url):
                        continue
                    async with self.upload_slots:
                        payload = await manager._fetch(url, deadline)
                    if not payload:
                        continue
                    digest = (
                        hashlib.sha256(payload).hexdigest()
                        if s.media_cache_deduplication
                        else None
                    )
                    if digest:
                        alias = await self.find_digest(bot.id, digest)
                        if alias:
                            await self.remember(
                                bot.id,
                                url,
                                alias[0],
                                digest=digest,
                                chat_id=alias[1],
                                message_id=alias[2],
                            )
                            self.stats["deduplicated"] += 1
                            continue
                    key = digest or url
                    if key in groups:
                        groups[key][2].append(url)
                    else:
                        groups[key] = (payload, digest, [url])
                groups = {
                    key: (
                        payload,
                        digest,
                        [url for url in aliases if self._wanted(bot, url)],
                    )
                    for key, (payload, digest, aliases) in groups.items()
                }
                groups = {key: value for key, value in groups.items() if value[2]}
                if len(groups) < 2:
                    return
                media = [
                    InputMediaPhoto(
                        media=BufferedInputFile(
                            payload, filename="cache." + (_extension(payload) or "jpg")
                        )
                    )
                    for payload, _, _ in groups.values()
                ]
                sent = await self._paced(
                    lambda: bot.send_media_group(
                        s.media_cache_chat_id,
                        media,
                        disable_notification=True,
                        request_timeout=20,
                    ),
                    deadline,
                    cost=len(media),
                )
                if not sent or len(sent) != len(media):
                    return
                for (_, digest, aliases), message in zip(groups.values(), sent):
                    if not message.photo:
                        continue
                    photo = max(message.photo, key=lambda p: p.width * p.height)
                    for url in aliases:
                        await self.remember(
                            bot.id,
                            url,
                            photo.file_id,
                            digest=digest,
                            chat_id=s.media_cache_chat_id,
                            message_id=message.message_id,
                        )
                    self.stats["deduplicated"] += len(aliases) - 1
                    self.stats["uploads"] += 1
                    if not s.media_cache_keep_messages:
                        try:
                            await bot.delete_message(
                                s.media_cache_chat_id,
                                message.message_id,
                                request_timeout=3,
                            )
                        except Exception:
                            logging.debug("Cache album cleanup failed", exc_info=True)
                self.stats["albums"] += 1
                self.total_upload_seconds += time.monotonic() - start

    async def _work(self):
        from bots.telegram.rich_media import background_media

        with background_media():
            await self._work_queue()

    async def _work_queue(self):
        while not self.queue.empty():
            batch = []
            # Up to 10 images, bounded by configured local memory budget.
            size = min(
                self.settings.media_cache_batch_size,
                max(
                    1,
                    self.settings.rich_media.ram_cache_bytes
                    // max(1, self.settings.rich_media.max_image_bytes),
                ),
            )
            for _ in range(size):
                if self.queue.empty():
                    break
                batch.append(self.queue.get_nowait())
            try:
                by_bot = {}
                for bot, url in batch:
                    by_bot.setdefault(bot.id, (bot, []))[1].append(url)
                for bot, urls in by_bot.values():
                    if len(urls) > 1:
                        try:
                            await self._batch(
                                bot, urls, asyncio.get_running_loop().time() + 40
                            )
                        except Exception:
                            logging.debug("Media cache album fallback", exc_info=True)
                    for url in urls:
                        if not self._wanted(bot, url):
                            continue
                        await self.stage(
                            bot, url, asyncio.get_running_loop().time() + 35
                        )
            except Exception:
                logging.debug("Media cache background task failed", exc_info=True)
            finally:
                for bot, url in batch:
                    self.queued.discard((bot.id, url))
                    self.prefetch_owners.pop((bot.id, url), None)
                    self.queue.task_done()

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.queued.clear()
        self.prefetch_owners.clear()
        self.prefetch_groups.clear()

    async def summary(self, bot_id):
        rows = unique = 0
        if self.settings.media_cache_enabled and self.settings.media_cache_persistent:
            try:
                rows, unique = await asyncio.to_thread(
                    lambda: self.index().counts(bot_id)
                )
            except Exception:
                pass
        return {
            **self.stats,
            "entries": rows,
            "files": unique,
            "queued": self.queue.qsize(),
            "average_upload_ms": round(
                1000 * self.total_upload_seconds / max(1, self.stats["uploads"])
            ),
        }


@lru_cache(maxsize=1)
def get_media_cache():
    return TelegramMediaCache(get_settings().telegram)
