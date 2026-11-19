"""Provider scope and direct-ID parsing independent of Telegram transport.

Shared by CLI, REST and GraphQL so callers cannot accidentally merge two
anime only because different providers assigned the same numeric ID.
"""
from __future__ import annotations

import re
from typing import Any

from search.registry import DEFAULT_SEARCH_PROVIDER_KEYS
from services.search.service import SearchService

ALL_PROVIDER_KEYS = tuple(str(key) for key in DEFAULT_SEARCH_PROVIDER_KEYS)
PROVIDER_ALIASES: dict[str, str] = {
    "mal": "mal",
    "myanimelist": "mal",
    "my-anime-list": "mal",
    "shikimori": "shikimori",
    "shiki": "shikimori",
    "шикимори": "shikimori",
    "anilist": "anilist",
    "ani": "anilist",
    "kitsu": "kitsu",
    "kit": "kitsu",
}
_DIRECT_ID_RE = re.compile(r"^(?:(?P<source>[a-zA-Z0-9_-]+):)?(?P<id>[0-9]+)$")
KNOWN_ID_SOURCES = (
    "mal", "shikimori", "anilist", "kitsu", "anidb", "anisearch"
)


def parse_direct_id(
    text: str,
    supported_sources: tuple[str, ...] | list[str] = KNOWN_ID_SOURCES,
) -> tuple[str | None, str] | None:
    found = _DIRECT_ID_RE.fullmatch(text.strip())
    if found is None:
        return None
    raw_source = (found.group("source") or "").casefold()
    identifier = found.group("id")
    if not raw_source:
        return None, identifier
    canonical = PROVIDER_ALIASES.get(raw_source, raw_source)
    if canonical not in set(supported_sources):
        return None
    return canonical, identifier


def parse_scope(text: str) -> tuple[str, ...] | None:
    value = text.strip().casefold()
    if value in {"all", "все"}:
        return ALL_PROVIDER_KEYS
    if value in {"default", "main", "основной"}:
        return tuple(str(key) for key in DEFAULT_SEARCH_PROVIDER_KEYS)
    parts = [
        part.strip() for part in value.replace("+", ",").split(",")
        if part.strip()
    ]
    if not parts:
        return None
    result: list[str] = []
    for part in parts:
        key = PROVIDER_ALIASES.get(part, part if part in ALL_PROVIDER_KEYS else None)
        if key is None:
            return None
        if key not in result:
            result.append(key)
    return tuple(key for key in ALL_PROVIDER_KEYS if key in result)


def scoped_search_service(
    provider_keys: tuple[str, ...],
    default_service: SearchService,
    provider_map: dict[str, Any],
) -> SearchService:
    """Only request configured upstream providers; do not create global state."""
    provider_keys = tuple(k for k in ALL_PROVIDER_KEYS if k in provider_keys)
    if not provider_keys:
        raise ValueError("No supported providers selected")
    if set(provider_keys) == set(default_service.settings.default_provider_keys):
        return default_service
    if any(key not in provider_map for key in provider_keys):
        raise ValueError("The selected search provider is not configured")
    return SearchService(
        tuple(provider_map[key] for key in provider_keys),
        settings=default_service.settings,
    )
