from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import logging
import os
import re
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)

from core.cache import BoundedTTLCache
from core.settings import ROOT_DIR, RichMediaSettings, get_settings

_IMAGE_URL = re.compile(r'(<img\s+[^>]*?\bsrc=")(https?://[^"]+)(")', re.IGNORECASE)
_IMAGE_TAG = re.compile(r'<img\s+[^>]*?\bsrc="(https?://[^"]+)"[^>]*?/?>', re.IGNORECASE)
_EMPTY_SLIDESHOW = re.compile(
    r"<tg-slideshow>\s*</tg-slideshow>", re.IGNORECASE
)
_SINGLE_SLIDESHOW = re.compile(
    r"<tg-slideshow>\s*(<img[^>]+>)\s*</tg-slideshow>",
    re.IGNORECASE,
)
_MEDIA_FAILURES = (
    "rich_message_photo_no_media_found",
    "failed to get http url content",
    "wrong type of the web page content",
    "photo_invalid_dimensions",
    "photo_content_type_invalid",
    "image_process_failed",
    "failed to get url content",
)


def media_error(error: BaseException) -> bool:
    """Do not treat unrelated Telegram request errors as photo failures."""
    return isinstance(error, TelegramBadRequest) and any(
        message in str(error).casefold() for message in _MEDIA_FAILURES
    )


def _remote_urls(message: InputRichMessage) -> list[str]:
    result: list[str] = []
    for match in _IMAGE_TAG.finditer(message.html or ""):
        value = html.unescape(match.group(1))
        if value not in result:
            result.append(value)
    return result


def _image_free(message: InputRichMessage) -> InputRichMessage:
    body = _IMAGE_TAG.sub("", message.html or "")
    body = _EMPTY_SLIDESHOW.sub("", body)
    return message.model_copy(update={"html": body, "media": None})


def bind_remote_photos(message: InputRichMessage) -> InputRichMessage:
    """Use the Bot API URL-backed rich-media representation."""
    source = message.html
    if not source or "<img" not in source.lower():
        return message
    existing = list(message.media or ())
    used_ids = {item.id for item in existing}
    identifiers: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        url = html.unescape(match.group(2))
        identifier = identifiers.get(url)
        if identifier is None:
            index = len(identifiers)
            identifier = f"adb_photo_{index}"
            while identifier in used_ids:
                index += 1
                identifier = f"adb_photo_{index}"
            identifiers[url] = identifier
            used_ids.add(identifier)
            existing.append(
                InputRichMessageMedia(
                    id=identifier, media=InputMediaPhoto(media=url)
                )
            )
        return f'{match.group(1)}tg://photo?id={identifier}{match.group(3)}'

    body = _IMAGE_URL.sub(replace, source)
    if not identifiers:
        return message
    return message.model_copy(update={"html": body, "media": existing})


def _with_media(
    message: InputRichMessage,
    files: dict[str, BufferedInputFile | FSInputFile],
) -> InputRichMessage | None:
    existing = list(message.media or ())
    photos: list[InputRichMessageMedia] = []
    references: dict[str, str] = {}
    used_ids = {item.id for item in existing}
    for index, (url, uploaded) in enumerate(files.items()):
        identifier = f"adb_upload_{index}"
        while identifier in used_ids:
            identifier += "_"
        used_ids.add(identifier)
        references[url] = identifier
        photos.append(
            InputRichMessageMedia(
                id=identifier, media=InputMediaPhoto(media=uploaded)
            )
        )
    if not photos:
        return None

    def replace(match: re.Match[str]) -> str:
        url = html.unescape(match.group(1))
        identifier = references.get(url)
        if identifier is None:
            return ""
        return f'<img src="tg://photo?id={identifier}"/>'

    body = _IMAGE_TAG.sub(replace, message.html or "")
    body = _EMPTY_SLIDESHOW.sub("", body)
    body = _SINGLE_SLIDESHOW.sub(r"\1", body)
    return message.model_copy(update={"html": body, "media": existing + photos})


def _extension(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def _verify_public_host(url: str) -> None:
    target = urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username:
        raise ValueError("image URL must use public HTTPS")
    if target.port not in (None, 443):
        raise ValueError("image URL has a non-standard port")
    try:
        addresses = socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("image host does not resolve") from exc
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise ValueError("image host does not resolve exclusively to public IPs")


class RichMediaDelivery:
    """Bounded per-request rich-media fallback manager.

    Remote strategies never download media locally. RAM and disk methods
    download only the selected images. Failed photos are removed from the
    markup, never replaced by a sendMessage fallback.
    """

    def __init__(self, settings: RichMediaSettings) -> None:
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.max_downloads)
        # Sending a media-rich response retains all uploaded buffers until
        # Telegram finishes the request. Cap whole local deliveries, not only
        # individual downloads, or many simultaneous users can exhaust RAM.
        self._local_deliveries = asyncio.Semaphore(settings.max_local_deliveries)
        self._memory = BoundedTTLCache[str, bytes](
            max_entries=2048,
            ttl_seconds=float(settings.cache_ttl_seconds),
            max_weight=max(1, settings.ram_cache_bytes),
            weigher=len,
            name="rich_media_ram",
        )
        self._bad_urls = BoundedTTLCache[str, bool](
            max_entries=2048, ttl_seconds=120.0, name="rich_media_bad_urls"
        )
        self._bad_strategies = BoundedTTLCache[tuple[str, tuple[str, ...]], bool](
            max_entries=2048, ttl_seconds=180.0, name="rich_media_failures"
        )
        self._disk_last_prune = 0.0

    async def _fetch(self, url: str, deadline: float) -> bytes | None:
        if self._bad_urls.get(url):
            return None
        if self.settings.ram_enabled and self.settings.ram_cache_bytes:
            cached = self._memory.get(url)
            if cached is not None:
                return cached
        try:
            async with self._slots:
                if self.settings.ram_enabled and self.settings.ram_cache_bytes:
                    cached = self._memory.get(url)
                    if cached is not None:
                        return cached
                timeout = min(
                    self.settings.download_timeout_seconds,
                    max(0.05, deadline - asyncio.get_running_loop().time()),
                )
                if timeout < 0.1:
                    return None
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    current = url
                    for _ in range(4):
                        await asyncio.to_thread(_verify_public_host, current)
                        async with client.stream("GET", current) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                redirect = response.headers.get("location")
                                if not redirect:
                                    raise ValueError("missing redirect location")
                                current = urljoin(current, redirect)
                                continue
                            response.raise_for_status()
                            content_type = response.headers.get("content-type", "")
                            if not content_type.casefold().startswith("image/"):
                                raise ValueError("non-image content")
                            size_header = response.headers.get("content-length", "")
                            if size_header.isdigit() and int(size_header) > self.settings.max_image_bytes:
                                raise ValueError("image is larger than configured limit")
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(data) + len(chunk) > self.settings.max_image_bytes:
                                    raise ValueError("image exceeds max image bytes")
                                data.extend(chunk)
                            payload = bytes(data)
                            if not _extension(payload):
                                raise ValueError("unsupported or invalid image bytes")
                            if self.settings.ram_enabled and self.settings.ram_cache_bytes:
                                self._memory.set(url, payload)
                            return payload
                    raise ValueError("too many image redirects")
        except (ValueError, httpx.HTTPError, OSError, TimeoutError) as exc:
            logging.debug(
                "Rich media source failed",
                extra={
                    "event": "rich_media_fetch",
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                },
            )
            self._bad_urls.set(url, True)
            return None

    def _disk_directory(self) -> Path:
        root = ROOT_DIR.resolve()
        directory = self.settings.disk_dir.resolve()
        if not directory.is_relative_to(root):
            raise ValueError("RICH_MEDIA_DISK_DIR must be inside the project")
        return directory

    def _file_candidates(self, url: str) -> list[Path]:
        path = self._disk_directory()
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return [path / f"{key}.{ext}" for ext in ("jpg", "png", "gif", "webp")]

    def _prune_disk(self, directory: Path) -> None:
        now = time.time()
        if now - self._disk_last_prune < 60:
            return
        self._disk_last_prune = now
        items = []
        total = 0
        if not directory.is_dir():
            return
        for item in directory.iterdir():
            if (
                item.is_symlink()
                or not item.is_file()
                or not re.fullmatch(r"[a-f0-9]{64}\.(jpg|png|gif|webp)", item.name)
            ):
                continue
            stat = item.stat()
            if now - stat.st_mtime > self.settings.cache_ttl_seconds:
                item.unlink(missing_ok=True)
                continue
            total += stat.st_size
            items.append((stat.st_mtime, stat.st_size, item))
        for _, size, item in sorted(items):
            if total <= self.settings.disk_cache_bytes:
                break
            item.unlink(missing_ok=True)
            total -= size

    def _read_disk(self, url: str) -> Path | None:
        directory = self._disk_directory()
        self._prune_disk(directory)
        for candidate in self._file_candidates(url):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            stat = candidate.stat()
            if (
                stat.st_size <= 0
                or stat.st_size > self.settings.max_image_bytes
                or time.time() - stat.st_mtime > self.settings.cache_ttl_seconds
            ):
                continue
            os.utime(candidate, None)
            return candidate
        return None

    def _write_disk(self, url: str, data: bytes) -> Path | None:
        if not self.settings.disk_cache_bytes:
            return None
        directory = self._disk_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        ext = _extension(data)
        if ext is None:
            return None
        target = directory / (
            hashlib.sha256(url.encode("utf-8")).hexdigest() + f".{ext}"
        )
        if not target.is_file():
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                    tmp = Path(handle.name)
                    os.chmod(tmp, 0o600)
                    handle.write(data)
                os.replace(tmp, target)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
        self._disk_last_prune = 0
        self._prune_disk(directory)
        return target if target.is_file() else None

    async def _prepare_local(
        self,
        message: InputRichMessage,
        mode: str,
        urls: list[str],
        deadline: float,
    ) -> InputRichMessage | None:
        selected = urls[: self.settings.max_images]

        async def prepare(url: str) -> tuple[str, BufferedInputFile | FSInputFile | None]:
            if mode == "disk":
                stored = await asyncio.to_thread(self._read_disk, url)
                if stored is not None:
                    return url, FSInputFile(stored)
            payload = await self._fetch(url, deadline)
            if payload is None:
                return url, None
            if mode == "disk":
                stored = await asyncio.to_thread(self._write_disk, url, payload)
                if stored is not None:
                    return url, FSInputFile(stored)
                return url, None
            ext = _extension(payload) or "jpg"
            return url, BufferedInputFile(payload, filename=f"photo.{ext}")

        results = await asyncio.gather(*(prepare(url) for url in selected), return_exceptions=True)
        files: dict[str, BufferedInputFile | FSInputFile] = {}
        for item in results:
            if not isinstance(item, BaseException):
                url, uploaded = item
                if uploaded is not None:
                    files[url] = uploaded
        return _with_media(message, files)

    async def deliver(
        self,
        send: Callable[[InputRichMessage], Awaitable[object]],
        message: InputRichMessage,
    ) -> None:
        urls = _remote_urls(message)
        if not urls:
            await send(message)
            return

        deadline = asyncio.get_running_loop().time() + self.settings.total_timeout_seconds
        for mode in self.settings.order:
            if mode == "ram" and not self.settings.ram_enabled:
                continue
            if mode == "disk" and not self.settings.disk_enabled:
                continue
            if asyncio.get_running_loop().time() >= deadline:
                break
            cache_key = (mode, tuple(urls))
            if self._bad_strategies.get(cache_key):
                continue
            try:
                if mode == "direct":
                    variant = message
                elif mode == "url":
                    variant = bind_remote_photos(message)
                elif mode in {"ram", "disk"}:
                    # Keep the download buffers and outgoing multipart body
                    # under one global semaphore until Telegram accepts them.
                    async with self._local_deliveries:
                        if asyncio.get_running_loop().time() >= deadline:
                            break
                        variant = await self._prepare_local(message, mode, urls, deadline)
                        if variant is None:
                            continue
                        await send(variant)
                        return
                else:
                    continue
                await send(variant)
                return
            except TelegramBadRequest as exc:
                if not media_error(exc):
                    raise
                self._bad_strategies.set(cache_key, True)
                logging.warning(
                    "Rich media method rejected; trying next fallback",
                    extra={
                        "event": "rich_media_fallback",
                        "method": mode,
                        "outcome": "retry",
                        "size": len(urls),
                        "error_type": type(exc).__name__,
                    },
                )

        # An image failure is NEVER a reason to downgrade the whole message
        # to sendMessage: the remaining tables and embedded buttons still work.
        await send(_image_free(message))


@lru_cache(maxsize=1)
def get_rich_media_delivery() -> RichMediaDelivery:
    return RichMediaDelivery(get_settings().telegram.rich_media)


async def deliver_rich_media(
    send: Callable[[InputRichMessage], Awaitable[object]],
    message: InputRichMessage,
) -> None:
    await get_rich_media_delivery().deliver(send, message)
