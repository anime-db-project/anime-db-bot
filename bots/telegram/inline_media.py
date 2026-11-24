from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)
from core.cache import BoundedTTLCache
from core.settings import get_settings
from bots.telegram.rich_media import get_rich_media_delivery, _extension

# Telegram file IDs are specific to a bot. The normal preview renderer also
# supplies successful IDs, so the inline path can reuse already-uploaded photos.
_ids = BoundedTTLCache(
    max_entries=4096, ttl_seconds=7 * 86400, name="telegram_photo_ids"
)
_failures = BoundedTTLCache(
    max_entries=1024, ttl_seconds=300, name="inline_photo_failures"
)
_upload_slots = asyncio.Semaphore(2)
_IMG = re.compile(r'<img[^>]*?src="([^"]+)"[^>]*?/?>', re.I)


def remember_photo(bot_id, url, file_id):
    if url and file_id:
        _ids.set((bot_id, url), file_id)


async def photo_id(bot, url, deadline, *, upload=False):
    key = (bot.id, url)
    known = _ids.get(key)
    if known:
        return known
    settings = get_settings().telegram
    target = settings.inline_media_cache_chat_id
    if not upload or not target or _failures.get(key):
        return None
    try:
        async with asyncio.timeout_at(deadline):
            async with _upload_slots:
                known = _ids.get(key)
                if known:
                    return known
                manager = get_rich_media_delivery()
                payload = await manager._fetch(url, deadline)
                if not payload:
                    return None
                ext = _extension(payload)
                if not ext:
                    return None
                sent = await bot.send_photo(
                    target,
                    BufferedInputFile(payload, filename="inline." + ext),
                    disable_notification=True,
                    request_timeout=8,
                )
                if sent.photo:
                    known = max(
                        sent.photo, key=lambda photo: photo.width * photo.height
                    ).file_id
                    remember_photo(bot.id, url, known)
                # Only the exact message we created in the explicitly configured cache chat.
                try:
                    await bot.delete_message(target, sent.message_id, request_timeout=4)
                except Exception:
                    logging.debug("Could not delete temporary inline cache photo")
                return known
    except Exception:
        _failures.set(key, True)
        logging.debug("Inline photo preparation failed", exc_info=True)
        return None


async def prepare_inline_rich(bot, rich, *, upload=False, use_cache=True):
    """Produce rich content with only existing Telegram files; never inline uploads."""
    body = rich.html or ""
    old = {
        item.id: item.media.media
        for item in (rich.media or ())
        if isinstance(item.media, InputMediaPhoto)
    }
    urls = []
    for match in _IMG.finditer(body):
        raw = html.unescape(match.group(1))
        value = (
            old.get(raw.removeprefix("tg://photo?id="))
            if raw.startswith("tg://photo?id=")
            else raw
        )
        if (
            isinstance(value, str)
            and value.startswith("https://")
            and value not in urls
        ):
            urls.append(value)
    settings = get_settings().telegram.rich_media
    selected = urls[: settings.max_images]
    deadline = asyncio.get_running_loop().time() + settings.total_timeout_seconds
    ids = {}
    if use_cache:
        values = await asyncio.gather(
            *(photo_id(bot, url, deadline, upload=upload) for url in selected)
        )
        ids = dict(zip(selected, values))
    attached = []
    missing = False

    def replace(match):
        nonlocal missing
        raw = html.unescape(match.group(1))
        value = (
            old.get(raw.removeprefix("tg://photo?id="))
            if raw.startswith("tg://photo?id=")
            else raw
        )
        file_id = None
        if use_cache and isinstance(value, str):
            if value.startswith("https://"):
                file_id = ids.get(value)
            elif raw.startswith("tg://photo?id=") and not value.startswith(
                ("http:", "attach:", "file:", "tg:")
            ):
                file_id = value
        if file_id:
            tag = "inline_photo_" + str(len(attached))
            attached.append(
                InputRichMessageMedia(id=tag, media=InputMediaPhoto(media=file_id))
            )
            return '<img src="tg://photo?id=' + tag + '"/>'
        missing = True
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return '<p><a href="' + html.escape(value, quote=True) + '">↗</a></p>'
        return ""

    body = _IMG.sub(replace, body)
    if missing:
        body = re.sub(r"</?tg-slideshow[^>]*>", "", body, flags=re.I)
    return rich.model_copy(update={"html": body, "media": attached or None})
