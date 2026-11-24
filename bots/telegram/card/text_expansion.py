"""Shared configurable text chunking for inline, single-message pagination."""

from __future__ import annotations

from bots.telegram.card.ui.constants import RICH_MESSAGE_LIMIT
from bots.telegram.card.views.rendering import split_text
from core.settings import get_settings


def long_text_parts(value: str, *, locale: str, heading: str = '') -> list[str]:
    """Return complete, ordered pages; the caller renders normal navigation."""
    settings = get_settings().telegram
    size = settings.long_text_chunk_size
    if heading and settings.long_text_include_ui:
        size = max(1, size - len(heading) - 2)
    parts = split_text(value, max(1, min(size, RICH_MESSAGE_LIMIT - 700 - len(heading), 4096 - (len(heading) + 2 if heading else 0))), locale=locale)
    return [heading + '\n\n' + part for part in parts] if heading else parts


def record_pages(records: list[str], *, heading: str = "") -> list[list[str]]:
    """Group intact records; a single oversize record is never cut in half."""
    settings = get_settings().telegram
    limit = min(settings.long_text_chunk_size, RICH_MESSAGE_LIMIT - 700)
    overhead = len(heading) + 2 if settings.long_text_include_ui else 0
    # Keep a separate hard cap for plain Telegram fallbacks.
    limit = min(limit - overhead, 4096 - (len(heading) + 2 if heading else 0))
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0
    for record in records:
        length = len(record) + bool(current)
        if current and (size + length > limit or len(current) >= 200):
            pages.append(current)
            current, size = [], 0
        size += len(record) + bool(current)
        current.append(record)
    if current:
        pages.append(current)
    return pages


def records_rich_html(value: str) -> str:
    """One full record per table cell; preserve the heading and paragraph gap."""
    import html
    from bots.telegram.card.views.rendering import plain_text_to_rich_html
    heading, sep, body = value.partition("\n\n")
    if not sep or not body:
        return plain_text_to_rich_html(value)
    rows = "".join(
        '<tr><td align="left" valign="top">' + html.escape(line.removeprefix("• ")) + "</td></tr>"
        for line in body.splitlines() if line.strip()
    )
    return plain_text_to_rich_html(heading) + "<p><br></p><table bordered striped compact>" + rows + "</table>"
