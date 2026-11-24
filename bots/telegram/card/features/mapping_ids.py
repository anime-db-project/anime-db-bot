from __future__ import annotations

import logging
import html
from urllib.parse import quote
from aiogram.types import InputRichMessage
from bots.telegram.card.text_expansion import record_pages
from collections.abc import Mapping

from aiogram.types import Message

from bots.telegram.card.ui.constants import STANDARD_MESSAGE_LIMIT
from bots.telegram.card.ui.keyboards import navigation_keyboard
from bots.telegram.card.views.rendering import rich
from core.models import AggregatedAnime
from localization import text
from mappings.resolver import MappingResolver

PAGE_LIMIT = STANDARD_MESSAGE_LIMIT - 200


def mapping_ids_pages(
    amdb_id: str,
    source_ids: Mapping[str, tuple[str, ...]],
    *,
    locale: str,
) -> list[str]:
    """Paginate all of an AMDB title's source-native IDs without dropping rows."""
    title = text("card", "all_ids_title", locale=locale)
    if not source_ids:
        return [title + "\n\n" + text("card", "all_ids_empty", locale=locale)]

    rows = [f"AMDB: {amdb_id.removeprefix('amdb:')}"]
    for provider, ids in sorted(source_ids.items()):
        if provider == "amdb":
            continue
        for external_id in ids:
            rows.append(f"{provider.upper().replace('_', ' ')}: {external_id.replace('_', ' ')}")

    return [title + "\n\n" + "\n".join(group) for group in record_pages(rows, heading=title)]


def _id_url(provider: str, value: str) -> str | None:
    from core.mapping_links import mapping_id_url
    from core.settings import get_settings
    return mapping_id_url(provider, value, get_settings().telegram.mapping_id_urls)


def mapping_ids_rich(page: str, source_ids=None) -> InputRichMessage:
    heading, _, body = page.partition("\n\n")
    rows = []
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            rows.append(f'<tr><td colspan="2">{html.escape(line)}</td></tr>')
            continue
        original = value
        if source_ids is not None:
            candidates = [v for v in source_ids.get(key.lower().replace(" ", "_"), ()) if v.replace("_", " ") == value]
            if len(candidates) == 1:
                original = candidates[0]
        url = _id_url(key, original)
        if url is None and source_ids is not None:
            # Internal numeric IDs have no public page: use the unique sibling
            # slug when the entity has exactly one unambiguous alternative.
            alias = {"KAIZE ID": "kaize", "NAUTILJON ID": "nautiljon", "LETTERBOXD UID": "letterboxd_slug"}.get(key)
            siblings = source_ids.get(alias, ()) if alias else ()
            if len(siblings) == 1:
                url = _id_url(alias, siblings[0])
        escaped = html.escape(value)
        rendered = f'<a href="{html.escape(url, quote=True)}">{escaped}</a>' if url else escaped
        rows.append(f"<tr><th>{html.escape(key)}</th><td>{rendered}</td></tr>")
    return InputRichMessage(html=f"<h2>{html.escape(heading)}</h2><p><br></p><table bordered striped compact>"+"".join(rows)+"</table>")


async def edit_mapping_ids_view(
    message: Message,
    token: str,
    anime: AggregatedAnime,
    *,
    mapping_resolver: MappingResolver,
    page: int,
    locale: str,
) -> None:
    """Replace the current card with IDs from its verified AMDB anchor."""
    source_ids = mapping_resolver.ids_for_anchor(anime.amdb_id) if anime.amdb_id else {}
    pages = mapping_ids_pages(anime.amdb_id or "", source_ids, locale=locale)
    page = max(0, min(int(page), len(pages) - 1))
    keyboard = navigation_keyboard(
        token, anime, "all_ids", page, len(pages), locale=locale
    )
    try:
        await message.edit_text(
            rich_message=mapping_ids_rich(pages[page], source_ids),
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception("Rich AMDB ID view failed; using plain text")
        await message.edit_text(text=pages[page], reply_markup=keyboard)
