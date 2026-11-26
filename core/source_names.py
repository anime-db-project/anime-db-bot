from __future__ import annotations

# One logical user-facing source backed by the official MAL API plus the local
# on-demand Jikan parser for MAL web-only capabilities.
MAL_SOURCE_NAME = "MAL (MyAnimeList) / Jikan"


def source_display_name(key: str) -> str:
    """One logical catalog label, independent of the transport backend."""
    return {
        "mal": MAL_SOURCE_NAME,
        "myanimelist": MAL_SOURCE_NAME,
        "anilist": "AniList",
        "shikimori": "Shikimori",
        "kitsu": "Kitsu",
        "anidb": "AniDB",
        "anisearch": "AniSearch",
        "imdb": "IMDb",
    }.get(key.casefold(), key.upper())
