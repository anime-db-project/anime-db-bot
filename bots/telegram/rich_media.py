from __future__ import annotations

import html
import re

from aiogram.types import InputMediaPhoto, InputRichMessage, InputRichMessageMedia

# Rich-message media links are explicitly bound to photo entries. Passing an
# external URL directly in <img src> can fail in the local Telegram Bot API.
_IMAGE_URL = re.compile(r'(<img\\s+[^>]*?\\bsrc=")(https?://[^"]+)(")', re.IGNORECASE)


def bind_remote_photos(rich_message: InputRichMessage) -> InputRichMessage:
    """Bind external photo URLs without downloading or storing image files.

    Telegram receives the original HTTPS URLs in InputRichMessage.media,
    referenced from the HTML using tg://photo?id=... as documented by Bot API.
    """
    source = rich_message.html
    if not source or "<img" not in source.lower():
        return rich_message

    existing = list(rich_message.media or ())
    used_ids = {media.id for media in existing}
    identifiers: dict[str, str] = {}

    def resolve(match: re.Match[str]) -> str:
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
                    id=identifier,
                    media=InputMediaPhoto(media=url),
                )
            )
        return f'{match.group(1)}tg://photo?id={identifier}{match.group(3)}'

    rewritten = _IMAGE_URL.sub(resolve, source)
    if not identifiers:
        return rich_message
    return rich_message.model_copy(update={"html": rewritten, "media": existing})
