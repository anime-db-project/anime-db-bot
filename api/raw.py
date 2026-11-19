"""Source-separated, capability-specific anime API.

Never calls AnimeAggregator, SearchService.collect, DirectIdLookupService or
Telegram handlers. Results are the *individual adapter's source records*,
not upstream wire JSON or a cross-source merged card. Direct IDs stay scoped
to the named source; mapping expansion is explicit and one-hop only.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import re
from typing import Any, Mapping

from core.capabilities import SourceCapability
from core.identity import AnimeIdentity
from core.people_loading import PeopleManifest, without_portraits
from scripts.operator_console import _serializable
from sources.runtime.catalog import SOURCE_CATALOG
from api.operations import ApiOperationError

SOURCE_CAPABILITIES = (
    "search", "id", "metadata", "staff", "voices", "characters",
    "episodes", "episode", "timestamps", "themes", "trailers",
    "screenshots", "popularity", "description", "relations",
    "recommendations", "image",
)
DETAIL_CAPABILITIES = (
    "id", "metadata", "staff", "voices", "characters", "episodes", "episode",
    "timestamps", "themes", "trailers", "screenshots", "popularity",
    "description", "relations", "recommendations",
)
CAPABILITY_ALIASES = {
    "seiyuu": "voices", "seyu": "voices", "voice_actors": "voices",
    "lookup": "id", "details": "metadata",
    "related": "relations", "recommendation": "recommendations",
    "photos": "image", "photo": "image", "opening": "themes",
    "ending": "themes", "openings": "themes", "endings": "themes",
}
_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
_SUPPORTED_KEYS = tuple(str(item.key) for item in SOURCE_CATALOG)
_DEFAULT_ALL_CAPABILITIES = (
    "id", "metadata", "staff", "voices", "characters", "episodes",
    "timestamps", "themes", "trailers", "screenshots", "popularity",
    "description", "relations", "recommendations",
)


def _capability(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiOperationError(400, "capability must be a string")
    normalized = value.strip().lower().replace("-", "_")
    name = CAPABILITY_ALIASES.get(normalized, normalized)
    if name not in {*SOURCE_CAPABILITIES, "all", "catalog", "bundle"}:
        raise ApiOperationError(400, "Unknown source capability")
    return name


def _selector(value: Any) -> str:
    if value is None or value == "":
        return "all"
    if not isinstance(value, str):
        raise ApiOperationError(400, "source must be a string")
    return value.strip().lower()


def _ids(value: Any) -> dict[str, str]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        entries = [part.strip() for part in value.split(",") if part.strip()]
        if len(entries) > 12:
            raise ApiOperationError(400, "Too many source IDs")
        pairs = []
        for entry in entries:
            if ":" not in entry:
                raise ApiOperationError(400, "IDs must be source:id pairs")
            source, identifier = entry.split(":", 1)
            pairs.append((source, identifier))
    elif isinstance(value, Mapping):
        if len(value) > 12:
            raise ApiOperationError(400, "Too many source IDs")
        pairs = list(value.items())
    else:
        raise ApiOperationError(400, "ids must be an object or source:id list")
    result: dict[str, str] = {}
    for source, identifier in pairs:
        source = str(source).strip().lower()
        identifier = str(identifier).strip()
        if source not in _SUPPORTED_KEYS or not _ID_PATTERN.fullmatch(identifier):
            raise ApiOperationError(400, "Invalid source ID pair")
        existing = result.get(source)
        if existing is not None and existing != identifier:
            raise ApiOperationError(400, "Conflicting IDs for one source")
        result[source] = identifier
    return result


def _num(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ApiOperationError(400, f"{name} must be an integer")
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ApiOperationError(400, f"{name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ApiOperationError(400, f"{name} must be {minimum}-{maximum}")
    return number


def _flag(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if value is True or value in ("true", "1", "yes"):
        return True
    if value is False or value in ("false", "0", "no"):
        return False
    raise ApiOperationError(400, "Boolean option must be true or false")


def _requested_sources(context: Any, capability: str, selector: str) -> list[str]:
    metadata = {
        str(getattr(source, "source_key", "")).lower()
        for source in context.sources
    }
    search = {str(key).lower() for key in context.search_provider_map}
    episodes = {
        str(source.backend_key).lower() for source in context.episode_service.sources
    }
    segments = {
        str(source.backend_key).lower()
        for source in context.episode_segment_service.sources
    }
    trailers = {
        str(source.backend_key).lower() for source in context.trailer_service.sources
    }
    images = {key.lower() for key in context.image_search_service.providers}
    if capability == "search":
        available = search
    elif capability == "image":
        available = images
    elif capability in {"episodes", "episode"}:
        available = episodes
    elif capability == "timestamps":
        available = segments
    elif capability == "themes":
        available = {
            str(getattr(context.anime_theme_service.source, "backend_key", "animethemes"))
        }
    elif capability == "trailers":
        available = trailers
    else:
        available = metadata

    if selector in {"all", "*"}:
        return sorted(available)
    tokens = [token.strip() for token in selector.split(",") if token.strip()]
    if not tokens or len(tokens) > 12:
        raise ApiOperationError(400, "Select between 1 and 12 sources")
    for token in tokens:
        if token not in available:
            raise ApiOperationError(
                400, f"Unknown or disabled source for {capability}: {token}"
            )
    return list(dict.fromkeys(tokens))


def _identity(
    context: Any, data: Mapping[str, Any], *,
    source: str,
) -> AnimeIdentity:
    supplied = _ids(data.get("ids"))
    single = data.get("id")
    if single is not None and single != "":
        external_id = str(single).strip()
        if not _ID_PATTERN.fullmatch(external_id):
            raise ApiOperationError(400, "id must be numeric")
        # A backend such as Jikan uses its own backend key but MAL identity.
        identity_key = source
        if source not in _SUPPORTED_KEYS:
            backends = (
                *context.episode_service.sources,
                *context.trailer_service.sources,
                *context.episode_segment_service.sources,
            )
            identity_key = next(
                (
                    str(backend.source_key)
                    for backend in backends
                    if str(getattr(backend, "backend_key", "")) == source
                    and str(getattr(backend, "source_key", "")) in _SUPPORTED_KEYS
                ),
                source,
            )
        # Never extrapolate one unqualified number to other providers.
        if source in {"all", "*"} or "," in source or identity_key not in _SUPPORTED_KEYS:
            raise ApiOperationError(
                400, "A single id needs a concrete source with a known identity; "
                "use ids={source:id} for multiple providers"
            )
        if identity_key in supplied and supplied[identity_key] != external_id:
            raise ApiOperationError(400, "Conflicting id and ids values")
        supplied[identity_key] = external_id
    if not supplied:
        raise ApiOperationError(
            400, "Provide source + id, or explicit source-scoped ids"
        )
    if _flag(data.get("resolve"), default=False):
        # Only verified, one-hop disk-backed mapping edges; no title guessing.
        mapper = context.identity_resolver.mapping_resolver
        enriched = mapper.resolve(AnimeIdentity.from_mapping(supplied))
        for key, value in (enriched or {}).items():
            if key not in supplied and key in _SUPPORTED_KEYS and _ID_PATTERN.fullmatch(
                str(value)
            ):
                supplied[key] = str(value)
    return AnimeIdentity.from_mapping(supplied)


def _catalog(context: Any) -> dict[str, Any]:
    search = {str(key) for key in context.search_provider_map}
    metadata = {str(getattr(x, "source_key", "")) for x in context.sources}
    episodes = {
        str(x.backend_key): str(x.source_key)
        for x in context.episode_service.sources
    }
    segment_sources = [str(x.backend_key) for x in context.episode_segment_service.sources]
    trailer_sources = [str(x.backend_key) for x in context.trailer_service.sources]
    return {
        "representation": "source_adapter_record",
        "aggregated": False,
        "capabilities": list(SOURCE_CAPABILITIES),
        "metadata_sources": [
            {
                "key": str(entry.key),
                "display_name": entry.display_name,
                "capabilities": sorted(str(x) for x in entry.capabilities),
                "enabled": str(entry.key) in metadata,
                "search_enabled": str(entry.key) in search,
            }
            for entry in SOURCE_CATALOG
        ],
        "search_sources": sorted(search),
        "episode_backends": episodes,
        "timestamp_backends": segment_sources,
        "trailer_backends": trailer_sources,
        "image_providers": sorted(context.image_search_service.providers),
        "theme_backend": str(
            getattr(context.anime_theme_service.source, "backend_key", "animethemes")
        ),
    }


async def _single(
    context: Any, capability: str, key: str, identity: AnimeIdentity | None,
    data: Mapping[str, Any],
) -> tuple[str, Any]:
    """Return (status, model). Each call fetches only the requested capability."""
    if capability == "search":
        query = data.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise ApiOperationError(400, "query must contain 1-256 characters")
        provider = next(
            provider for provider_key, provider in context.search_provider_map.items()
            if str(provider_key) == key
        )
        page = await provider.search(
            query.strip(),
            page=_num(data.get("page"), "page", 1, 1, 100),
            limit=_num(data.get("limit"), "limit", 20, 1, 100),
        )
        return "ok", page

    assert identity is not None
    if capability in {"id", "metadata", "staff", "voices", "characters", "screenshots",
                      "popularity", "description", "relations", "recommendations"}:
        source = next(
            src for src in context.sources if str(src.source_key) == key
        )
        if not identity.has(key):
            return "missing_id", None
        if capability == "id":
            # ID-only requests do not fetch a full anime card.
            verified = None
            if _flag(data.get("verify"), default=False):
                verified = await source.get_details(identity.copy()) is not None
            return "ok", {
                "source": key,
                "source_id": identity.get(key),
                "exists": verified,
            }
        from sources.runtime.catalog import source_supports

        required = {
            "staff": SourceCapability.PEOPLE,
            "voices": SourceCapability.PEOPLE,
            "characters": SourceCapability.CHARACTERS,
            "screenshots": SourceCapability.SCREENSHOTS,
            "popularity": SourceCapability.POPULARITY,
            "description": SourceCapability.DESCRIPTION,
            "relations": SourceCapability.RELATIONS,
            "recommendations": SourceCapability.RECOMMENDATIONS,
        }.get(capability)
        if required is not None and not source_supports(key, required):
            return "unsupported", None
        if capability == "metadata":
            record = await source.get_details(identity.copy())
            return ("ok" if record is not None else "not_found"), record
        if capability in {"staff", "voices"}:
            detail = str(data.get("detail") or "manifest").lower()
            if detail not in {"manifest", "full"}:
                raise ApiOperationError(400, "detail must be manifest or full")
            if detail == "manifest":
                manifest_loader = getattr(source, f"load_{capability}_manifest", None)
                if callable(manifest_loader):
                    manifest = await manifest_loader(identity.copy())
                    if isinstance(manifest, PeopleManifest):
                        return "ok", {
                            "people": manifest.people,
                            "portrait_only": manifest.portrait_only,
                            "level": "manifest",
                        }
                    return "ok", {
                        "people": without_portraits(list(manifest or [])),
                        "level": "manifest",
                    }
            loader = getattr(source, f"load_{capability}", None)
            if not callable(loader):
                return "unsupported", None
            people = await loader(identity.copy())
            return "ok", {
                "people": people if detail == "full" else without_portraits(
                    list(people or [])
                ),
                "level": detail,
            }
        methods = {
            "characters": "load_characters",
            "screenshots": "load_screenshots",
            "popularity": "load_popularity",
            "description": "load_description",
            "relations": "load_relations",
            "recommendations": "load_recommendations",
        }
        loader = getattr(source, methods[capability], None)
        if not callable(loader):
            return "unsupported", None
        return "ok", await loader(identity.copy())

    if capability in {"episodes", "episode"}:
        backend = next(
            item for item in context.episode_service.sources if item.backend_key == key
        )
        if not context.episode_service._supports_identity(backend, identity):
            return "missing_id", None
        if capability == "episodes":
            return "ok", await context.episode_service.load_page(
                identity, backend=key, page=_num(data.get("page"), "page", 1, 1, 100)
            )
        record = await context.episode_service.load_episode(
            identity, _num(data.get("episode"), "episode", 1, 1, 100000),
            backend=key,
        )
        return ("ok" if record is not None else "not_found"), record

    if capability == "timestamps":
        backend = next(
            item for item in context.episode_segment_service.sources
            if item.backend_key == key
        )
        if not backend.supports_identity(identity):
            return "missing_id", None
        return "ok", await context.episode_segment_service.load_segments(
            identity, _num(data.get("episode"), "episode", 1, 1, 100000),
            backend=key,
        )

    if capability == "themes":
        service = context.anime_theme_service
        if not service.supports_identity(identity):
            return "missing_id", None
        return "ok", await service.load_episode_themes(
            identity, _num(data.get("episode"), "episode", 1, 1, 100000)
        )

    if capability == "trailers":
        backend = next(
            item for item in context.trailer_service.sources
            if item.backend_key == key
        )
        if not identity.has(str(backend.source_key)):
            return "missing_id", None
        return "ok", await backend.load_trailers(identity.copy())

    raise ApiOperationError(400, "Unsupported source capability")


async def _raw_image(context: Any, selector: str, data: Mapping[str, Any]) -> dict:
    from PIL import Image, UnidentifiedImageError
    from core.image_search import ImageSearchInput

    options = _requested_sources(context, "image", selector)
    content = data.get("content_base64")
    kind = data.get("content_type", "image/jpeg")
    if not isinstance(content, str) or not content or len(content) > 16777216:
        raise ApiOperationError(400, "Provide a bounded content_base64 image")
    if kind not in {"image/jpeg", "image/png", "image/webp"}:
        raise ApiOperationError(415, "Allowed image types: JPEG, PNG, WebP")
    try:
        image_bytes = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ApiOperationError(400, "Invalid image base64") from exc
    if len(image_bytes) > context.settings.sources.image_search_max_bytes:
        raise ApiOperationError(413, "Image exceeds size limit")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format not in {"PNG", "WEBP", "JPEG"}:
                raise ApiOperationError(415, "Unsupported image encoding")
            if image.width * image.height > context.settings.telegram.image_max_pixels:
                raise ApiOperationError(413, "Image exceeds pixel limit")
            image.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ApiOperationError(400, "Invalid image data") from exc

    payload = ImageSearchInput(
        content=image_bytes, content_type=kind, filename="uploaded-image"
    )

    async def fetch(key: str) -> tuple[str, dict]:
        try:
            result = await context.image_search_service.providers[key].search(payload)
            return key, {"status": "ok", "data": _serializable(result)}
        except Exception as exc:
            return key, {"status": "error", "error_type": type(exc).__name__}

    results = dict(await asyncio.gather(*(fetch(key) for key in options)))
    return {
        "capability": "image", "aggregated": False,
        "representation": "source_adapter_record",
        "sources": results,
    }


async def execute_raw(
    context: Any, capability: str, payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a single explicit capability, or a user-requested capability bundle."""
    data = payload or {}
    if not isinstance(data, Mapping):
        raise ApiOperationError(400, "Input must be an object")
    capability = _capability(capability)
    if capability == "catalog":
        return _catalog(context)
    selector = _selector(data.get("source", "all"))
    if capability == "image":
        return await _raw_image(context, selector, data)

    if capability in {"all", "bundle"}:
        requested = data.get("capabilities")
        if requested is None and capability == "all":
            requested = list(_DEFAULT_ALL_CAPABILITIES)
        if not isinstance(requested, list) or not 1 <= len(requested) <= 14:
            raise ApiOperationError(400, "Provide a list of 1-14 capabilities")
        selections = list(dict.fromkeys(_capability(value) for value in requested))
        if any(item not in DETAIL_CAPABILITIES for item in selections):
            raise ApiOperationError(
                400, "Bundles only support explicit anime-detail capabilities"
            )
        return {
            "capability": "bundle", "aggregated": False,
            "results": {
                item: await execute_raw(context, item, data)
                for item in selections
            },
        }

    options = _requested_sources(context, capability, selector)
    identity = (
        None if capability == "search"
        else _identity(context, data, source=selector)
    )
    # Request input is checked before any provider is contacted.
    if capability == "search":
        q = data.get("query")
        if not isinstance(q, str) or not 1 <= len(q.strip()) <= 256:
            raise ApiOperationError(400, "query must contain 1-256 characters")
        _num(data.get("page"), "page", 1, 1, 100)
        _num(data.get("limit"), "limit", 20, 1, 100)
    if capability in {"episodes"}:
        _num(data.get("page"), "page", 1, 1, 100)
    if capability in {"episode", "timestamps", "themes"}:
        _num(data.get("episode"), "episode", 1, 1, 100000)
    if capability in {"staff", "voices"} and str(
        data.get("detail", "manifest")
    ) not in {"manifest", "full"}:
        raise ApiOperationError(400, "detail must be manifest or full")
    slots = asyncio.Semaphore(4)

    async def fetch(key: str) -> tuple[str, dict]:
        async with slots:
            try:
                state, value = await _single(context, capability, key, identity, data)
                return key, {
                    "status": state,
                    "data": _serializable(value) if state == "ok" else None,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Error types only: source exception bodies may contain secrets.
                return key, {"status": "error", "error_type": type(exc).__name__}

    collected = dict(await asyncio.gather(*(fetch(key) for key in options)))
    return {
        "capability": capability,
        "aggregated": False,
        "representation": "source_adapter_record",
        "identity": dict(identity.external_ids) if identity else None,
        "sources": collected,
    }
