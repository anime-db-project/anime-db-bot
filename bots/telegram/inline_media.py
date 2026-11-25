from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram.types import (
    Message,
    InputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)
from core.cache import BoundedTTLCache
from core.settings import get_settings
from bots.telegram.rich_media import get_rich_media_delivery
from bots.telegram.media_cache import get_media_cache
from aiogram.methods.base import TelegramMethod
from aiogram.exceptions import TelegramBadRequest
from typing import ClassVar
from dataclasses import dataclass
import time

# Telegram file IDs are specific to a bot. The normal preview renderer also
# supplies successful IDs, so the inline path can reuse already-uploaded photos.
_failures = BoundedTTLCache(
    max_entries=1024, ttl_seconds=300, name="inline_photo_failures"
)
_upload_slots = asyncio.Semaphore(2)
_direct_failures = BoundedTTLCache(
    max_entries=1024, ttl_seconds=180, name="inline_direct_failures"
)
_IMG = re.compile(r'<img[^>]*?src="([^"]+)"[^>]*?/?>', re.I)


def remember_photo(bot_id, url, file_id):
    cache = get_media_cache()
    if cache.settings.media_cache_enabled and url and file_id:
        cache.hot.set((bot_id, url), file_id)


@dataclass(frozen=True)
class InlineUploadContext:
    callback_id: str
    user_id: int
    started: float


class SendTemporaryPhoto(TelegramMethod[Message]):
    """Bot API permits a callback-bound ephemeral response with no chat_id.

    The installed aiogram version still requires chat_id on SendPhoto.
    """

    __returning__: ClassVar[type] = Message
    __api_method__: ClassVar[str] = "sendPhoto"
    photo: str | InputFile
    ephemeral_message_parameters: dict[str, str | bool]
    disable_notification: bool = True


async def _stage(bot, target, file, context):
    if target:
        sent = await bot.send_photo(
            target, file, disable_notification=True, request_timeout=5
        )
    else:
        if context is None or time.monotonic() - context.started >= 14:
            return None
        sent = await bot(
            SendTemporaryPhoto(
                photo=file,
                ephemeral_message_parameters={
                    "callback_query_id": context.callback_id,
                    "replace_callback_query_message": False,
                },
            ),
            request_timeout=5,
        )
    try:
        return (
            max(sent.photo, key=lambda p: p.width * p.height).file_id
            if sent.photo
            else None
        )
    finally:
        # Never delete the original inline message, only the staging upload.
        try:
            if target:
                await bot.delete_message(target, sent.message_id, request_timeout=3)
            elif sent.ephemeral_message_id:
                await bot.delete_ephemeral_message(
                    sent.chat.id,
                    context.user_id,
                    sent.ephemeral_message_id,
                    request_timeout=3,
                )
        except Exception:
            logging.debug("Inline staging cleanup failed", exc_info=True)


async def photo_id(bot, url, deadline, *, upload=False, context=None):
    key = (bot.id, url)
    cache = get_media_cache()
    known = await cache.lookup(bot.id, url)
    if known:
        return known
    settings = get_settings().telegram
    if (
        upload
        and cache.settings.media_cache_enabled
        and cache.settings.media_cache_chat_id < 0
    ):
        known = await cache.stage(bot, url, deadline)
        if known:
            return known
    target = settings.inline_media_cache_chat_id
    ephemeral = (
        getattr(settings, "inline_media_ephemeral_enabled", False)
        and context is not None
    )
    if not upload or not (target or ephemeral) or _failures.get(key):
        return None
    try:
        async with asyncio.timeout_at(deadline):
            async with _upload_slots:
                known = await cache.lookup(bot.id, url)
                if known:
                    return known
                manager = get_rich_media_delivery()
                for mode in manager.settings.order:
                    if mode == "ram" and not manager.settings.ram_enabled:
                        continue
                    if mode == "disk" and not manager.settings.disk_enabled:
                        continue
                    if mode not in {"direct", "url", "ram", "disk"}:
                        continue
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    try:
                        if mode in {"direct", "url"}:
                            # On a standalone photo send both remote strategies
                            # are equivalent. Try the URL only once.
                            if (
                                mode == "url"
                                and "direct"
                                in manager.settings.order[
                                    : manager.settings.order.index(mode)
                                ]
                            ):
                                continue
                            file = url
                        else:
                            file = await manager.prepare_photo_file(url, mode, deadline)
                        if file is None:
                            continue
                        known = await _stage(
                            bot, target, file, context if ephemeral else None
                        )
                        if known:
                            remember_photo(bot.id, url, known)
                            await cache.remember(bot.id, url, known)
                            logging.debug("Inline media staged via %s", mode)
                            return known
                    except (TelegramBadRequest, FileNotFoundError):
                        logging.debug(
                            "Inline staging fallback: %s", mode, exc_info=True
                        )
    except Exception:
        logging.debug("Inline photo preparation failed", exc_info=True)
    _failures.set(key, True)
    return None


def has_remote_photos(rich):
    if any(
        isinstance(item.media, InputMediaPhoto)
        and isinstance(item.media.media, str)
        and item.media.media.startswith(("https://", "http://"))
        for item in (rich.media or ())
    ):
        return True
    return any(
        html.unescape(m.group(1)).startswith(("https://", "http://"))
        for m in _IMG.finditer(rich.html or "")
    )


def remote_key(bot, rich):
    urls = []
    for item in rich.media or ():
        if (
            isinstance(item.media, InputMediaPhoto)
            and isinstance(item.media.media, str)
            and item.media.media.startswith(("https://", "http://"))
        ):
            urls.append(item.media.media)
    urls.extend(
        html.unescape(m.group(1))
        for m in _IMG.finditer(rich.html or "")
        if html.unescape(m.group(1)).startswith(("https://", "http://"))
    )
    return bot.id, tuple(sorted(set(urls)))


def direct_rejected(bot, rich):
    return bool(_direct_failures.get(remote_key(bot, rich)))


def remember_direct_failure(bot, rich):
    _direct_failures.set(remote_key(bot, rich), True)


async def forget_remote_ids(bot, rich):
    for url in remote_key(bot, rich)[1]:
        await get_media_cache().forget(bot.id, url)


async def prepare_inline_rich(
    bot, rich, *, upload=False, use_cache=True, remote=False, context=None
):
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
            *(
                photo_id(bot, url, deadline, upload=upload, context=context)
                for url in selected
            )
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
        if raw.startswith("tg://emoji?"):
            return match.group(0)
        if (
            remote
            and isinstance(value, str)
            and value.startswith(("https://", "http://"))
        ):
            return '<img src="' + html.escape(value, quote=True) + '"/>'
        missing = True
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return '<p><a href="' + html.escape(value, quote=True) + '">↗</a></p>'
        return ""

    body = _IMG.sub(replace, body)
    if missing:
        body = re.sub(r"</?tg-slideshow[^>]*>", "", body, flags=re.I)
    return rich.model_copy(update={"html": body, "media": attached or None})
