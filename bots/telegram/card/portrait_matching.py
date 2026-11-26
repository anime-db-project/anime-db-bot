"""Bounded visual corroboration of ambiguous character name matches."""

from __future__ import annotations
import asyncio
from io import BytesIO
from PIL import Image, ImageOps
from core.credit_identity import ambiguous_name_keys, credit_name_keys
from core.media_urls import usable_image_url
from core.models import CharacterRecord


def signature(payload: bytes):
    try:
        with Image.open(BytesIO(payload)) as img:
            if img.width * img.height > 10_000_000:
                return None
            image = ImageOps.exif_transpose(img).convert("RGB")
            ratio = image.width / image.height
            pixels = image.resize((16, 16), Image.Resampling.LANCZOS).tobytes()
            gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            values = list(gray.getdata())
            bits = 0
            for y in range(8):
                for x in range(8):
                    bits = (bits << 1) | int(values[y * 9 + x] > values[y * 9 + x + 1])
            return ratio, bits, pixels
    except Exception:
        return None


def visually_same(a, b):
    if a is None or b is None:
        return False
    ratio1, hash1, pixels1 = a
    ratio2, hash2, pixels2 = b
    # Require matching composition and colors. A different screenshot of
    # the same character does not qualify as a confirmed duplicate image.
    return (
        abs(ratio1 / ratio2 - 1) <= 0.25
        and (hash1 ^ hash2).bit_count() <= 5
        and sum(abs(x - y) for x, y in zip(pixels1, pixels2)) / len(pixels1) <= 16
    )


async def portrait_matcher(groups: list[list[CharacterRecord]]):
    from bots.telegram.rich_media import get_rich_media_delivery

    ambiguous = ambiguous_name_keys(groups)
    urls = list(
        dict.fromkeys(
            url
            for group in groups
            for record in group
            if credit_name_keys(record) & ambiguous
            for values in record.portraits.values()
            for url in values[:1]
            if usable_image_url(url)
        )
    )[:64]
    fingerprints = {}
    if urls:
        delivery = get_rich_media_delivery()
        deadline = asyncio.get_running_loop().time() + min(
            6, delivery.settings.total_timeout_seconds
        )

        async def load(url):
            data = await delivery._fetch(url, deadline)
            if data:
                fingerprints[url] = await asyncio.to_thread(signature, data)

        await asyncio.gather(*(load(url) for url in urls), return_exceptions=True)

    def matches(left, right):
        a = [fingerprints.get(u) for values in left.portraits.values() for u in values]
        b = [fingerprints.get(u) for values in right.portraits.values() for u in values]
        return any(visually_same(x, y) for x in a for y in b)

    return matches
