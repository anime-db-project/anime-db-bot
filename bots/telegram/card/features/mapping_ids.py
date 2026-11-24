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
    templates = {
        "MAL": "https://myanimelist.net/anime/{}",
        "MYANIMELIST": "https://myanimelist.net/anime/{}",
        "ANILIST": "https://anilist.co/anime/{}",
        "ANIDB": "https://anidb.net/anime/{}",
        "ANISEARCH": "https://www.anisearch.com/anime/{}",
        "SHIKIMORI": "https://shikimori.one/animes/{}",
        "KITSU": "https://kitsu.app/anime/{}",
        "BANGUMI": "https://bgm.tv/subject/{}",
        "IMDB": "https://www.imdb.com/title/{}",
        "TMDB": "https://www.themoviedb.org/tv/{}",
        "TMDB TV": "https://www.themoviedb.org/tv/{}",
        "TMDB MOVIE": "https://www.themoviedb.org/movie/{}",
        "TVDB": "https://thetvdb.com/?id={}&tab=series",
        "WIKIDATA": "https://www.wikidata.org/wiki/{}",
    }
    template = templates.get(provider)
    return template.format(quote(value, safe="")) if template and value else None


def mapping_ids_rich(page: str) -> InputRichMessage:
    heading, _, body = page.partition("\n\n")
    rows = []
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            rows.append(f'<tr><td colspan="2">{html.escape(line)}</td></tr>')
            continue
        url = _id_url(key, value)
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
            rich_message=mapping_ids_rich(pages[page]),
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception("Rich AMDB ID view failed; using plain text")
        await message.edit_text(text=pages[page], reply_markup=keyboard)
