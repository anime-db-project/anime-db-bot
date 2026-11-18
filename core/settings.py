from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, MutableMapping

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_CONFIG_FILES = (
    "telegram.env",
    "sources.env",
    "timestamps.env",
    "rate-limits.env",
    "mappings.env",
    "storage.env",
    "logging.env",
    "runtime.env",
)
_ADMIN_SPLIT_RE = re.compile(r"[\s,;]+")
_ENV_KEY_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


class ConfigFileError(RuntimeError):
    """Raised when project environment files are ambiguous or malformed."""


def _declared_env_keys(path: Path) -> tuple[str, ...]:
    keys: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = _ENV_KEY_RE.match(line)
        if match is None:
            continue
        key = match.group(1)
        if key in seen:
            raise ConfigFileError(
                f"Duplicate configuration key {key!r} inside {path} "
                f"at line {line_number}."
            )
        seen.add(key)
        keys.append(key)
    return tuple(keys)


def project_env_files(root_dir: Path = ROOT_DIR) -> tuple[Path, ...]:
    """Return only the explicitly supported project configuration files."""
    paths: list[Path] = []
    legacy = root_dir / ".env"
    if legacy.is_file():
        paths.append(legacy)
    split_dir = root_dir / ".env.d"
    paths.extend(
        path for name in ENV_CONFIG_FILES if (path := split_dir / name).is_file()
    )
    return tuple(paths)


def project_env_values(root_dir: Path = ROOT_DIR) -> dict[str, str]:
    """Load project config with duplicate-key rejection across all config files."""
    merged: dict[str, str] = {}
    origins: dict[str, Path] = {}
    for path in project_env_files(root_dir):
        keys = _declared_env_keys(path)
        parsed = dotenv_values(path)
        for key in keys:
            previous = origins.get(key)
            if previous is not None:
                raise ConfigFileError(
                    f"Duplicate configuration key {key!r} in {previous} and {path}. "
                    "Keep each project setting in exactly one config file."
                )
            value = parsed.get(key)
            merged[key] = "" if value is None else str(value)
            origins[key] = path
    return merged


def load_project_environment(
    root_dir: Path = ROOT_DIR,
    environ: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Load project files without overriding real process environment variables."""
    target = os.environ if environ is None else environ
    for key, value in project_env_values(root_dir).items():
        target.setdefault(key, value)
    return target


load_project_environment()


def _raw(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or "").strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _raw(env, name, "1" if default else "0").casefold()
    return value not in {"", "0", "false", "no", "off"}


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(_raw(env, name, str(default))))
    except ValueError:
        return max(minimum, int(default))


def _float(
    env: Mapping[str, str],
    name: str,
    default: float,
    minimum: float = 0.0,
) -> float:
    try:
        return max(minimum, float(_raw(env, name, str(default))))
    except ValueError:
        return max(minimum, float(default))


def _strict_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int = 1000000,
) -> int:
    """Reject malformed resource limits instead of silently changing them."""
    raw = _raw(env, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _strict_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    minimum: float = 0.0,
    maximum: float = 3600.0,
) -> float:
    raw = _raw(env, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be finite and between {minimum} and {maximum}")
    return value


def _path(env: Mapping[str, str], name: str, default: str) -> Path:
    value = _raw(env, name, default) or default
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = _raw(env, name)
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _user_ids(value: str, *, setting: str) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for token in _ADMIN_SPLIT_RE.split((value or "").strip()):
        if not token:
            continue
        if not token.isdecimal() or int(token) <= 0:
            raise ValueError(
                f"{setting} must contain positive numeric account IDs; got {token!r}"
            )
        user_id = int(token)
        if user_id not in seen:
            seen.add(user_id)
            result.append(user_id)
    return tuple(result)


def _admin_ids(value: str) -> tuple[int, ...]:
    return _user_ids(value, setting="ADMIN_TELEGRAM_USER_IDS")


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str
    structured: bool
    redact_secrets: bool
    directory: Path
    max_bytes: int
    backups: int
    retention_days: int = 30


@dataclass(frozen=True, slots=True)
class RichMediaSettings:
    # Strategies are attempted in this exact order. Removing a strategy
    # disables it; "text_only" is an unconditional final rich fallback.
    order: tuple[str, ...] = ("direct", "url", "ram", "disk")
    ram_enabled: bool = True
    disk_enabled: bool = True
    max_images: int = 8
    max_image_bytes: int = 4 * 1024 * 1024
    ram_cache_bytes: int = 16 * 1024 * 1024
    disk_cache_bytes: int = 256 * 1024 * 1024
    cache_ttl_seconds: int = 86400
    download_timeout_seconds: float = 3.0
    total_timeout_seconds: float = 7.0
    max_downloads: int = 8
    max_local_deliveries: int = 2
    disk_dir: Path = ROOT_DIR / "data/runtime/rich-media"
    failure_url_ttl_seconds: float = 120.0
    failure_method_ttl_seconds: float = 180.0
    failure_cache_max_entries: int = 2048
    ram_cache_max_entries: int = 2048
    max_redirects: int = 4
    disk_prune_interval_seconds: float = 60.0


def _search_providers(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _raw(
        env, "SEARCH_DEFAULT_PROVIDERS", "shikimori,mal,anilist,kitsu"
    )
    requested = tuple(
        token.strip().casefold() for token in raw.split(",") if token.strip()
    )
    supported = {"shikimori", "mal", "anilist", "kitsu"}
    if not requested or any(key not in supported for key in requested):
        raise ValueError(
            "SEARCH_DEFAULT_PROVIDERS must list at least one of "
            "shikimori,mal,anilist,kitsu"
        )
    if len(set(requested)) != len(requested):
        raise ValueError("SEARCH_DEFAULT_PROVIDERS cannot contain duplicates")
    return requested


def _rich_media_order(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _raw(env, "RICH_MEDIA_ORDER", "direct,url,ram,disk")
    values = [value.strip().casefold() for value in raw.split(",") if value.strip()]
    allowed = {"direct", "url", "ram", "disk"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(
            f"RICH_MEDIA_ORDER: unsupported strategies: {', '.join(sorted(unknown))}"
        )
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str
    bot_api_url: str
    bot_api_local: bool
    session_ttl_seconds: int
    anime_input_ttl_seconds: int
    admin_user_ids: tuple[int, ...]
    bot_api_local_file_root: Path | None = None
    allowed_user_ids: tuple[int, ...] = ()
    denied_reply_cooldown_seconds: float = 60.0
    denied_global_replies_per_minute: int = 20
    denied_max_tracked_users: int = 4096
    max_concurrent_updates: int = 64
    max_card_sessions: int = 256
    max_people_sessions: int = 256
    max_search_sessions: int = 192
    rich_media: RichMediaSettings = field(default_factory=RichMediaSettings)
    max_scope_sessions: int = 400
    max_pending_inputs: int = 500
    max_image_inputs: int = 100
    image_download_timeout_seconds: float = 45.0
    image_max_pixels: int = 50000000
    image_max_dimension: int = 4096
    search_expansion_timeout_seconds: float = 8.0
    initial_timecode_wait_seconds: float = 0.15
    admin_pending_sessions: int = 128
    admin_pending_ttl_seconds: int = 300
    denied_message: str = "Доступ к боту ограничен."


@dataclass(frozen=True, slots=True)
class StorageSettings:
    user_settings_db_path: Path
    bans_db_path: Path
    image_search_db_path: Path
    backend: str = "sqlite"
    postgres_dsn: str = ""
    postgres_pool_size: int = 5
    user_locale_cache_max_entries: int = 20000
    user_locale_cache_ttl_seconds: float = 120.0
    user_seen_write_interval_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class CacheBackendSettings:
    backend: str = "memory"
    redis_url: str = ""
    redis_key_prefix: str = "anime-db-bot"
    redis_timeout_seconds: float = 0.25
    redis_max_connections: int = 10
    redis_failure_cooldown_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class HttpSettings:
    max_concurrent_requests: int = 48
    max_connections: int = 48
    max_keepalive_connections: int = 24
    keepalive_expiry_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class SearchSettings:
    cache_ttl_seconds: float
    cache_max_entries: int
    collection_page_size: int
    collection_max_pages: int
    collection_max_items: int
    relevance_threshold: int
    irrelevant_tail_pages: int
    expansion_max_seeds: int
    expansion_max_queries: int
    expansion_max_pages: int
    cache_max_bytes: int = 4 * 1024 * 1024
    provider_service_cache_entries: int = 6
    default_provider_keys: tuple[str, ...] = (
        "shikimori", "mal", "anilist", "kitsu"
    )
    expansion_enabled: bool = True


@dataclass(frozen=True, slots=True)
class MappingSettings:
    db_path: Path
    manifest_path: Path
    release_repository: str
    auto_update: bool
    check_interval_seconds: float
    request_timeout_seconds: float
    keep_previous: bool


@dataclass(frozen=True, slots=True)
class SourceSettings:
    mal_client_id: str
    mal_api_url: str
    jikan_api_url: str
    jikan_port: int
    jikan_request_timeout_seconds: float
    jikan_supplement_cache_ttl_seconds: float
    jikan_people_cache_ttl_seconds: float
    jikan_update_idle_wait_seconds: int
    anidb_client: str
    anidb_client_version: int
    anidb_http_api_url: str
    anidb_request_timeout_seconds: float
    anidb_cache_dir: Path
    anidb_cache_ttl_seconds: float
    anisearch_api_url: str
    anisearch_app_id: str
    anisearch_app_secret: str
    anisearch_request_timeout_seconds: float
    anisearch_cache_ttl_seconds: float
    skipdb_api_url: str
    skipdb_site_url: str
    skipdb_request_timeout_seconds: float
    skipdb_cache_ttl_seconds: float
    anime_skip_api_url: str
    anime_skip_client_id: str
    anime_skip_request_timeout_seconds: float
    anime_skip_cache_ttl_seconds: float
    animethemes_api_url: str
    animethemes_request_timeout_seconds: float
    animethemes_cache_ttl_seconds: float
    bahamut_api_url: str
    bahamut_request_timeout_seconds: float
    baha_anime_skip_data_url: str
    baha_anime_skip_db_path: Path
    shikimori_api_url: str
    shikimori_site_url: str
    shikimori_people_cache_ttl_seconds: float
    shikimori_people_character_batch_size: int
    shikimori_rest_min_interval_seconds: float
    people_portrait_batch_size: int
    anilist_api_url: str
    anilist_access_token: str
    anilist_people_max_pages: int
    anilist_people_cache_ttl_seconds: float
    kitsu_api_url: str
    kitsu_request_timeout_seconds: float
    kitsu_cache_ttl_seconds: float
    trace_moe_api_url: str
    trace_moe_api_key: str
    trace_moe_request_timeout_seconds: float
    trace_moe_quota_cache_ttl_seconds: float
    trace_moe_cut_borders: bool
    whatanimeisthis_api_url: str
    whatanimeisthis_api_key: str
    whatanimeisthis_request_timeout_seconds: float
    whatanimeisthis_guest_daily_limit: int
    whatanimeisthis_api_daily_limit: int
    image_search_max_bytes: int
    imdb_ratings_backend: str
    imdb_ratings_db_path: Path
    imdb_api_endpoint: str
    imdb_api_key: str
    imdb_api_data_set_id: str
    imdb_api_revision_id: str
    imdb_api_asset_id: str
    imdb_aws_access_key_id: str
    imdb_aws_secret_access_key: str
    imdb_aws_session_token: str
    imdb_aws_region: str
    imdb_request_timeout_seconds: float
    anisearch_cache_max_bytes: int = 16 * 1024 * 1024
    anisearch_cache_max_entries: int = 750
    anisearch_interactive_wait_seconds: float = 0.75
    jikan_cache_max_entries: int = 1000
    people_manifest_concurrency: int = 12
    people_portrait_concurrency: int = 8


@dataclass(frozen=True, slots=True)
class SourceHealthSettings:
    failure_threshold: int
    cooldown_seconds: float


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    user_per_minute: int
    user_per_hour: int
    upstream_default_per_minute: int
    upstream_per_source: dict[str, int]
    upstream_per_operation: dict[tuple[str, str], int]

    def source_limit(self, source: str) -> int:
        return self.upstream_per_source.get(
            (source or "").strip().casefold(),
            self.upstream_default_per_minute,
        )

    def operation_limit(self, source: str, operation: str) -> int | None:
        return self.upstream_per_operation.get(
            ((source or "").strip().casefold(), (operation or "").strip().casefold())
        )


@dataclass(frozen=True, slots=True)
class AppSettings:
    logging: LoggingSettings
    telegram: TelegramSettings
    storage: StorageSettings
    search: SearchSettings
    mappings: MappingSettings
    sources: SourceSettings
    source_health: SourceHealthSettings
    rate_limits: RateLimitSettings
    cache: CacheBackendSettings = field(default_factory=CacheBackendSettings)
    http: HttpSettings = field(default_factory=HttpSettings)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppSettings":
        values: Mapping[str, str] = os.environ if env is None else env

        upstream_default = _int(
            values,
            "UPSTREAM_RATE_LIMIT_DEFAULT_PER_MINUTE",
            120,
            1,
        )
        source_defaults = {
            "anilist": 80,
            "kitsu": 60,
            "shikimori": 240,
            "mal": 120,
            "jikan": 30,
            "anidb": 20,
            "anisearch": 12,
            "skipdb": 30,
            "anime_skip": 60,
            "animethemes": 30,
            "trace_moe": 30,
            "whatanimeisthis": 10,
            "imdb": 30,
        }
        upstream_per_source = {
            source: _int(
                values,
                f"UPSTREAM_RATE_LIMIT_{source.upper()}_PER_MINUTE",
                default,
                1,
            )
            for source, default in source_defaults.items()
        }
        # Anime Skip client IDs have a hard server-side ceiling of 60 RPM. Local
        # configuration may reserve headroom, but must never raise that ceiling.
        upstream_per_source["anime_skip"] = min(60, upstream_per_source["anime_skip"])
        operation_defaults = {
            ("anilist", "people"): 20,
            ("kitsu", "search"): 60,
            ("kitsu", "episodes"): 60,
            ("kitsu", "trailers"): 60,
            ("shikimori", "people"): 60,
            ("jikan", "metadata"): 20,
            ("jikan", "people"): 15,
            ("jikan", "trailers"): 15,
            ("jikan", "episodes"): 20,
            ("anidb", "episodes"): 20,
            ("anisearch", "people"): 12,
            ("anisearch", "episodes"): 12,
            ("skipdb", "identity"): 30,
            ("skipdb", "episodes"): 30,
            ("skipdb", "episode"): 120,
            ("anime_skip", "episode_segments"): 60,
            ("animethemes", "themes"): 30,
            ("trace_moe", "quota"): 30,
            ("trace_moe", "image_search"): 20,
            ("whatanimeisthis", "image_search"): 10,
            ("imdb", "ratings"): 30,
        }
        upstream_per_operation = {
            (source, operation): _int(
                values,
                f"UPSTREAM_RATE_LIMIT_{source.upper()}_{operation.upper()}_PER_MINUTE",
                default,
                1,
            )
            for (source, operation), default in operation_defaults.items()
        }

        database_backend = _raw(values, "DATABASE_BACKEND", "sqlite").casefold()
        if database_backend not in {"sqlite", "postgres"}:
            raise ValueError("DATABASE_BACKEND must be sqlite or postgres")
        postgres_dsn = _raw(values, "DATABASE_URL")
        if database_backend == "postgres" and not postgres_dsn:
            raise ValueError("DATABASE_URL is required when DATABASE_BACKEND=postgres")
        cache_backend = _raw(values, "CACHE_BACKEND", "memory").casefold()
        if cache_backend not in {"memory", "redis"}:
            raise ValueError("CACHE_BACKEND must be memory or redis")
        redis_url = _raw(values, "REDIS_URL")
        if cache_backend == "redis" and not redis_url:
            raise ValueError("REDIS_URL is required when CACHE_BACKEND=redis")

        return cls(
            logging=LoggingSettings(
                level=(_raw(values, "LOG_LEVEL", "INFO") or "INFO").upper(),
                structured=_bool(values, "LOG_STRUCTURED", True),
                redact_secrets=_bool(values, "LOG_REDACT_SECRETS", True),
                directory=_path(values, "LOG_DIR", "logs"),
                max_bytes=_int(values, "LOG_MAX_BYTES", 10485760, 1),
                backups=_int(values, "LOG_BACKUPS", 5, 0),
                retention_days=_int(values, "LOG_RETENTION_DAYS", 30, 1),
            ),
            telegram=TelegramSettings(
                bot_token=_raw(values, "BOT_TOKEN"),
                bot_api_url=_raw(values, "TELEGRAM_BOT_API_URL"),
                bot_api_local=_bool(values, "TELEGRAM_BOT_API_LOCAL", False),
                session_ttl_seconds=_int(
                    values, "TELEGRAM_SESSION_TTL_SECONDS", 3600, 30
                ),
                anime_input_ttl_seconds=_int(
                    values, "ANIME_INPUT_TTL_SECONDS", 300, 30
                ),
                admin_user_ids=_admin_ids(_raw(values, "ADMIN_TELEGRAM_USER_IDS")),
                allowed_user_ids=_user_ids(
                    _raw(values, "TELEGRAM_ALLOWED_USER_IDS"),
                    setting="TELEGRAM_ALLOWED_USER_IDS",
                ),
                denied_reply_cooldown_seconds=_float(
                    values, "TELEGRAM_DENIED_REPLY_COOLDOWN_SECONDS", 60.0
                ),
                denied_global_replies_per_minute=_int(
                    values, "TELEGRAM_DENIED_GLOBAL_REPLIES_PER_MINUTE", 20
                ),
                denied_max_tracked_users=_int(
                    values, "TELEGRAM_DENIED_MAX_TRACKED_USERS", 4096, 1
                ),
                denied_message=_raw(
                    values, "TELEGRAM_DENIED_MESSAGE", "Доступ к боту ограничен."
                ) or "Доступ к боту ограничен.",
                bot_api_local_file_root=_optional_path(
                    values, "TELEGRAM_BOT_API_LOCAL_FILE_ROOT"
                ),
                max_concurrent_updates=_int(
                    values, "TELEGRAM_MAX_CONCURRENT_UPDATES", 64, 1
                ),
                max_card_sessions=_int(values, "TELEGRAM_MAX_CARD_SESSIONS", 256, 1),
                max_people_sessions=_int(
                    values, "TELEGRAM_MAX_PEOPLE_SESSIONS", 256, 1
                ),
                max_search_sessions=_int(
                    values, "TELEGRAM_MAX_SEARCH_SESSIONS", 192, 1
                ),
                max_scope_sessions=_strict_int(
                    values, "TELEGRAM_MAX_SCOPE_SESSIONS", 400, 1, 100000
                ),
                max_pending_inputs=_strict_int(
                    values, "TELEGRAM_MAX_PENDING_INPUTS", 500, 1, 100000
                ),
                max_image_inputs=_strict_int(
                    values, "TELEGRAM_MAX_IMAGE_INPUTS", 100, 1, 100000
                ),
                image_download_timeout_seconds=_strict_float(
                    values, "TELEGRAM_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", 45.0, 1.0, 300.0
                ),
                image_max_pixels=_strict_int(
                    values, "TELEGRAM_IMAGE_MAX_PIXELS", 50000000, 100000, 100000000
                ),
                image_max_dimension=_strict_int(
                    values, "TELEGRAM_IMAGE_MAX_DIMENSION", 4096, 256, 16384
                ),
                search_expansion_timeout_seconds=_strict_float(
                    values, "TELEGRAM_SEARCH_EXPANSION_TIMEOUT_SECONDS", 8.0, 0.1, 120.0
                ),
                initial_timecode_wait_seconds=_strict_float(
                    values, "TELEGRAM_INITIAL_TIMECODE_WAIT_SECONDS", 0.15, 0.0, 10.0
                ),
                admin_pending_sessions=_strict_int(
                    values, "TELEGRAM_ADMIN_PENDING_SESSIONS", 128, 1, 10000
                ),
                admin_pending_ttl_seconds=_strict_int(
                    values, "TELEGRAM_ADMIN_PENDING_TTL_SECONDS", 300, 30, 86400
                ),
                rich_media=RichMediaSettings(
                    order=_rich_media_order(values),
                    ram_enabled=_bool(values, "RICH_MEDIA_RAM_ENABLED", True),
                    disk_enabled=_bool(values, "RICH_MEDIA_DISK_ENABLED", True),
                    max_images=_int(values, "RICH_MEDIA_MAX_IMAGES", 8, 1),
                    max_image_bytes=_int(
                        values, "RICH_MEDIA_MAX_IMAGE_BYTES", 4194304, 65536
                    ),
                    ram_cache_bytes=_int(
                        values, "RICH_MEDIA_RAM_CACHE_BYTES", 16777216, 0
                    ),
                    disk_cache_bytes=_int(
                        values, "RICH_MEDIA_DISK_CACHE_BYTES", 268435456, 0
                    ),
                    cache_ttl_seconds=_int(
                        values, "RICH_MEDIA_CACHE_TTL_SECONDS", 86400, 60
                    ),
                    download_timeout_seconds=_float(
                        values, "RICH_MEDIA_DOWNLOAD_TIMEOUT_SECONDS", 3.0, 0.1
                    ),
                    total_timeout_seconds=_float(
                        values, "RICH_MEDIA_TOTAL_TIMEOUT_SECONDS", 7.0, 0.5
                    ),
                    max_downloads=_int(values, "RICH_MEDIA_MAX_DOWNLOADS", 8, 1),
                    max_local_deliveries=_int(
                        values, "RICH_MEDIA_MAX_LOCAL_DELIVERIES", 2, 1
                    ),
                    failure_url_ttl_seconds=_strict_float(
                        values, "RICH_MEDIA_FAILED_URL_TTL_SECONDS", 120.0, 0.0, 86400.0
                    ),
                    failure_method_ttl_seconds=_strict_float(
                        values, "RICH_MEDIA_FAILED_METHOD_TTL_SECONDS", 180.0, 0.0, 86400.0
                    ),
                    failure_cache_max_entries=_strict_int(
                        values, "RICH_MEDIA_FAILURE_CACHE_ENTRIES", 2048, 1, 100000
                    ),
                    ram_cache_max_entries=_strict_int(
                        values, "RICH_MEDIA_RAM_CACHE_ENTRIES", 2048, 1, 100000
                    ),
                    max_redirects=_strict_int(
                        values, "RICH_MEDIA_MAX_REDIRECTS", 4, 1, 10
                    ),
                    disk_prune_interval_seconds=_strict_float(
                        values, "RICH_MEDIA_DISK_PRUNE_INTERVAL_SECONDS", 60.0, 0.0, 3600.0
                    ),
                    disk_dir=_path(
                        values, "RICH_MEDIA_DISK_DIR", "data/runtime/rich-media"
                    ),
                ),
            ),
            storage=StorageSettings(
                user_settings_db_path=_path(
                    values,
                    "USER_SETTINGS_DB_PATH",
                    "data/user_settings.sqlite3",
                ),
                bans_db_path=_path(values, "BANS_DB_PATH", "data/bans.sqlite3"),
                image_search_db_path=_path(
                    values, "IMAGE_SEARCH_DB_PATH", "data/image_search.sqlite3"
                ),
                backend=database_backend,
                postgres_dsn=postgres_dsn,
                postgres_pool_size=_int(values, "DATABASE_POOL_SIZE", 5, 1),
                user_locale_cache_max_entries=_strict_int(
                    values, "USER_LOCALE_CACHE_MAX_ENTRIES", 20000, 1, 1000000
                ),
                user_locale_cache_ttl_seconds=_strict_float(
                    values, "USER_LOCALE_CACHE_TTL_SECONDS", 120.0, 0.0, 86400.0
                ),
                user_seen_write_interval_seconds=_strict_float(
                    values, "USER_SEEN_WRITE_INTERVAL_SECONDS", 300.0, 0.0, 86400.0
                ),
            ),
            cache=CacheBackendSettings(
                backend=cache_backend,
                redis_url=redis_url,
                redis_key_prefix=_raw(values, "REDIS_KEY_PREFIX", "anime-db-bot")
                or "anime-db-bot",
                redis_timeout_seconds=_float(
                    values, "REDIS_TIMEOUT_SECONDS", 0.25, 0.05
                ),
                redis_max_connections=_strict_int(
                    values, "REDIS_MAX_CONNECTIONS", 10, 1, 10000
                ),
                redis_failure_cooldown_seconds=_strict_float(
                    values, "REDIS_FAILURE_COOLDOWN_SECONDS", 20.0, 0.0, 3600.0
                ),
            ),
            http=HttpSettings(
                max_concurrent_requests=_strict_int(
                    values, "HTTP_MAX_CONCURRENT_REQUESTS", 48, 1, 10000
                ),
                max_connections=_strict_int(
                    values, "HTTP_MAX_CONNECTIONS", 48, 1, 10000
                ),
                max_keepalive_connections=_strict_int(
                    values, "HTTP_MAX_KEEPALIVE_CONNECTIONS", 24, 0, 10000
                ),
                keepalive_expiry_seconds=_strict_float(
                    values, "HTTP_KEEPALIVE_EXPIRY_SECONDS", 5.0, 0.0, 3600.0
                ),
            ),
            search=SearchSettings(
                cache_ttl_seconds=_float(values, "SEARCH_CACHE_TTL_SECONDS", 21600.0),
                cache_max_entries=_int(values, "SEARCH_CACHE_MAX_ENTRIES", 500, 1),
                cache_max_bytes=_int(values, "SEARCH_CACHE_MAX_BYTES", 4194304, 1024),
                provider_service_cache_entries=_int(
                    values, "SEARCH_PROVIDER_SERVICE_CACHE_ENTRIES", 6, 1
                ),
                default_provider_keys=_search_providers(values),
                expansion_enabled=_bool(values, "SEARCH_EXPANSION_ENABLED", True),
                collection_page_size=_int(values, "SEARCH_COLLECTION_PAGE_SIZE", 20, 1),
                collection_max_pages=_int(values, "SEARCH_COLLECTION_MAX_PAGES", 4, 1),
                collection_max_items=_int(
                    values, "SEARCH_COLLECTION_MAX_ITEMS", 120, 1
                ),
                relevance_threshold=_int(values, "SEARCH_RELEVANCE_THRESHOLD", 55, 0),
                irrelevant_tail_pages=_int(
                    values, "SEARCH_IRRELEVANT_TAIL_PAGES", 1, 1
                ),
                expansion_max_seeds=_int(values, "SEARCH_EXPANSION_MAX_SEEDS", 1, 0),
                expansion_max_queries=_int(
                    values, "SEARCH_EXPANSION_MAX_QUERIES", 1, 0
                ),
                expansion_max_pages=_int(values, "SEARCH_EXPANSION_MAX_PAGES", 2, 1),
            ),
            mappings=MappingSettings(
                db_path=_path(values, "MAPPINGS_DB_PATH", "data/mappings/mappings.db"),
                manifest_path=_path(
                    values,
                    "MAPPINGS_MANIFEST_PATH",
                    "data/mappings/manifest.json",
                ),
                release_repository=_raw(
                    values,
                    "MAPPINGS_RELEASE_REPOSITORY",
                    "anime-db-project/anime-mapping-db",
                ).strip("/"),
                auto_update=_bool(values, "MAPPINGS_AUTO_UPDATE", True),
                check_interval_seconds=_float(
                    values,
                    "MAPPINGS_CHECK_INTERVAL_SECONDS",
                    3600.0,
                    300.0,
                ),
                request_timeout_seconds=_float(
                    values,
                    "MAPPINGS_REQUEST_TIMEOUT_SECONDS",
                    300.0,
                    30.0,
                ),
                keep_previous=_bool(values, "MAPPINGS_KEEP_PREVIOUS", True),
            ),
            sources=SourceSettings(
                mal_client_id=_raw(values, "MAL_CLIENT_ID"),
                mal_api_url=_raw(
                    values,
                    "MAL_API_URL",
                    "https://api.myanimelist.net/v2",
                ).rstrip("/"),
                jikan_api_url=_raw(
                    values,
                    "JIKAN_API_URL",
                    "http://127.0.0.1:8091",
                ).rstrip("/"),
                jikan_port=_int(values, "JIKAN_PORT", 8091, 1),
                jikan_request_timeout_seconds=_float(
                    values,
                    "JIKAN_REQUEST_TIMEOUT_SECONDS",
                    45.0,
                    5.0,
                ),
                jikan_supplement_cache_ttl_seconds=_float(
                    values,
                    "JIKAN_SUPPLEMENT_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                jikan_people_cache_ttl_seconds=_float(
                    values,
                    "JIKAN_PEOPLE_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                jikan_update_idle_wait_seconds=_int(
                    values,
                    "JIKAN_UPDATE_IDLE_WAIT_SECONDS",
                    300,
                    30,
                ),
                anidb_client=_raw(values, "ANIDB_CLIENT"),
                anidb_client_version=_int(values, "ANIDB_CLIENT_VERSION", 1, 1),
                anidb_http_api_url=_raw(
                    values,
                    "ANIDB_HTTP_API_URL",
                    "http://api.anidb.net:9001/httpapi",
                ),
                anidb_request_timeout_seconds=_float(
                    values,
                    "ANIDB_REQUEST_TIMEOUT_SECONDS",
                    45.0,
                    10.0,
                ),
                anidb_cache_dir=_path(values, "ANIDB_CACHE_DIR", "data/anidb"),
                anidb_cache_ttl_seconds=_float(
                    values,
                    "ANIDB_CACHE_TTL_SECONDS",
                    86400.0,
                ),
                anisearch_api_url=_raw(
                    values,
                    "ANISEARCH_API_URL",
                    "https://api.anisearch.com",
                ).rstrip("/"),
                anisearch_app_id=_raw(values, "ANISEARCH_APP_ID"),
                anisearch_app_secret=_raw(values, "ANISEARCH_APP_SECRET"),
                anisearch_request_timeout_seconds=_float(
                    values,
                    "ANISEARCH_REQUEST_TIMEOUT_SECONDS",
                    30.0,
                    3.0,
                ),
                anisearch_cache_ttl_seconds=_float(
                    values,
                    "ANISEARCH_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                anisearch_cache_max_bytes=_int(
                    values, "ANISEARCH_CACHE_MAX_BYTES", 16777216, 1024
                ),
                anisearch_cache_max_entries=_strict_int(
                    values, "ANISEARCH_CACHE_MAX_ENTRIES", 750, 1, 100000
                ),
                anisearch_interactive_wait_seconds=_strict_float(
                    values, "ANISEARCH_INTERACTIVE_WAIT_SECONDS", 0.75, 0.0, 10.0
                ),
                jikan_cache_max_entries=_strict_int(
                    values, "JIKAN_CACHE_MAX_ENTRIES", 1000, 1, 100000
                ),
                people_manifest_concurrency=_strict_int(
                    values, "PEOPLE_MANIFEST_CONCURRENCY", 12, 1, 128
                ),
                people_portrait_concurrency=_strict_int(
                    values, "PEOPLE_PORTRAIT_CONCURRENCY", 8, 1, 128
                ),
                skipdb_api_url=_raw(
                    values,
                    "SKIPDB_API_URL",
                    "https://skipdb.tv",
                ).rstrip("/"),
                skipdb_site_url=_raw(
                    values,
                    "SKIPDB_SITE_URL",
                    "https://skipdb.tv",
                ).rstrip("/"),
                skipdb_request_timeout_seconds=_float(
                    values,
                    "SKIPDB_REQUEST_TIMEOUT_SECONDS",
                    20.0,
                    3.0,
                ),
                skipdb_cache_ttl_seconds=_float(
                    values,
                    "SKIPDB_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                anime_skip_api_url=_raw(
                    values,
                    "ANIME_SKIP_API_URL",
                    "https://anime-skip.com/api",
                ).rstrip("/"),
                anime_skip_client_id=_raw(values, "ANIME_SKIP_CLIENT_ID"),
                anime_skip_request_timeout_seconds=_float(
                    values,
                    "ANIME_SKIP_REQUEST_TIMEOUT_SECONDS",
                    20.0,
                    3.0,
                ),
                anime_skip_cache_ttl_seconds=_float(
                    values,
                    "ANIME_SKIP_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                animethemes_api_url=_raw(
                    values,
                    "ANIMETHEMES_API_URL",
                    "https://api.animethemes.moe",
                ).rstrip("/"),
                animethemes_request_timeout_seconds=_float(
                    values,
                    "ANIMETHEMES_REQUEST_TIMEOUT_SECONDS",
                    15.0,
                    3.0,
                ),
                animethemes_cache_ttl_seconds=_float(
                    values,
                    "ANIMETHEMES_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                bahamut_api_url=_raw(
                    values,
                    "BAHAMUT_API_URL",
                    "https://api.gamer.com.tw/mobile_app/anime",
                ).rstrip("/"),
                bahamut_request_timeout_seconds=_float(
                    values,
                    "BAHAMUT_REQUEST_TIMEOUT_SECONDS",
                    20.0,
                    3.0,
                ),
                baha_anime_skip_data_url=_raw(
                    values,
                    "BAHA_ANIME_SKIP_DATA_URL",
                    "https://raw.githubusercontent.com/JacobLinCool/baha-anime-skip/main/packages/baha-anime-skip-db/data.json",
                ),
                baha_anime_skip_db_path=_path(
                    values,
                    "BAHA_ANIME_SKIP_DB_PATH",
                    "data/baha_anime_skip.sqlite3",
                ),
                shikimori_api_url=_raw(
                    values,
                    "SHIKIMORI_API_URL",
                    "https://shikimori.io/api/graphql",
                ),
                shikimori_site_url=_raw(
                    values,
                    "SHIKIMORI_SITE_URL",
                    "https://shikimori.io",
                ).rstrip("/"),
                shikimori_people_cache_ttl_seconds=_float(
                    values,
                    "SHIKIMORI_PEOPLE_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                shikimori_people_character_batch_size=_int(
                    values,
                    "SHIKIMORI_PEOPLE_CHARACTER_BATCH_SIZE",
                    8,
                    1,
                ),
                shikimori_rest_min_interval_seconds=_float(
                    values,
                    "SHIKIMORI_REST_MIN_INTERVAL_SECONDS",
                    0.24,
                ),
                people_portrait_batch_size=_int(
                    values,
                    "PEOPLE_PORTRAIT_BATCH_SIZE",
                    50,
                    1,
                ),
                anilist_api_url=_raw(
                    values,
                    "ANILIST_API_URL",
                    "https://graphql.anilist.co",
                ),
                anilist_access_token=_raw(values, "ANILIST_ACCESS_TOKEN"),
                anilist_people_max_pages=_int(
                    values,
                    "ANILIST_PEOPLE_MAX_PAGES",
                    4,
                    1,
                ),
                anilist_people_cache_ttl_seconds=_float(
                    values,
                    "ANILIST_PEOPLE_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                kitsu_api_url=_raw(
                    values,
                    "KITSU_API_URL",
                    "https://kitsu.io/api/edge",
                ).rstrip("/"),
                kitsu_request_timeout_seconds=_float(
                    values,
                    "KITSU_REQUEST_TIMEOUT_SECONDS",
                    20.0,
                    3.0,
                ),
                kitsu_cache_ttl_seconds=_float(
                    values,
                    "KITSU_CACHE_TTL_SECONDS",
                    21600.0,
                ),
                trace_moe_api_url=_raw(
                    values,
                    "TRACE_MOE_API_URL",
                    "https://api.trace.moe",
                ).rstrip("/"),
                trace_moe_api_key=_raw(values, "TRACE_MOE_API_KEY"),
                trace_moe_request_timeout_seconds=_float(
                    values, "TRACE_MOE_REQUEST_TIMEOUT_SECONDS", 30.0, 5.0
                ),
                trace_moe_quota_cache_ttl_seconds=_float(
                    values, "TRACE_MOE_QUOTA_CACHE_TTL_SECONDS", 30.0, 1.0
                ),
                trace_moe_cut_borders=_bool(values, "TRACE_MOE_CUT_BORDERS", True),
                whatanimeisthis_api_url=_raw(
                    values,
                    "WHATANIMEISTHIS_API_URL",
                    "https://whatanimeisthis.com",
                ).rstrip("/"),
                whatanimeisthis_api_key=_raw(values, "WHATANIMEISTHIS_API_KEY"),
                whatanimeisthis_request_timeout_seconds=_float(
                    values, "WHATANIMEISTHIS_REQUEST_TIMEOUT_SECONDS", 30.0, 5.0
                ),
                whatanimeisthis_guest_daily_limit=_int(
                    values, "WHATANIMEISTHIS_GUEST_DAILY_LIMIT", 5, 1
                ),
                whatanimeisthis_api_daily_limit=_int(
                    values, "WHATANIMEISTHIS_API_DAILY_LIMIT", 10, 0
                ),
                image_search_max_bytes=_int(
                    values, "IMAGE_SEARCH_MAX_BYTES", 20 * 1024 * 1024, 1024
                ),
                imdb_ratings_backend=_raw(
                    values, "IMDB_RATINGS_BACKEND", "auto"
                ).casefold(),
                imdb_ratings_db_path=_path(
                    values, "IMDB_RATINGS_DB_PATH", "data/imdb_ratings.sqlite3"
                ),
                imdb_api_endpoint=_raw(
                    values,
                    "IMDB_API_ENDPOINT",
                    "https://api-fulfill.dataexchange.us-east-1.amazonaws.com/v1",
                ),
                imdb_api_key=_raw(values, "IMDB_API_KEY"),
                imdb_api_data_set_id=_raw(values, "IMDB_API_DATA_SET_ID"),
                imdb_api_revision_id=_raw(values, "IMDB_API_REVISION_ID"),
                imdb_api_asset_id=_raw(values, "IMDB_API_ASSET_ID"),
                imdb_aws_access_key_id=(
                    _raw(values, "IMDB_AWS_ACCESS_KEY_ID")
                    or _raw(values, "AWS_ACCESS_KEY_ID")
                ),
                imdb_aws_secret_access_key=(
                    _raw(values, "IMDB_AWS_SECRET_ACCESS_KEY")
                    or _raw(values, "AWS_SECRET_ACCESS_KEY")
                ),
                imdb_aws_session_token=(
                    _raw(values, "IMDB_AWS_SESSION_TOKEN")
                    or _raw(values, "AWS_SESSION_TOKEN")
                ),
                imdb_aws_region=_raw(values, "IMDB_AWS_REGION", "us-east-1"),
                imdb_request_timeout_seconds=_float(
                    values, "IMDB_REQUEST_TIMEOUT_SECONDS", 15.0, 3.0
                ),
            ),
            source_health=SourceHealthSettings(
                failure_threshold=_int(
                    values,
                    "SOURCE_HEALTH_FAILURE_THRESHOLD",
                    3,
                    1,
                ),
                cooldown_seconds=_float(
                    values,
                    "SOURCE_HEALTH_COOLDOWN_SECONDS",
                    30.0,
                    1.0,
                ),
            ),
            rate_limits=RateLimitSettings(
                user_per_minute=_int(values, "USER_RATE_LIMIT_PER_MINUTE", 50, 1),
                user_per_hour=_int(values, "USER_RATE_LIMIT_PER_HOUR", 1000, 1),
                upstream_default_per_minute=upstream_default,
                upstream_per_source=upstream_per_source,
                upstream_per_operation=upstream_per_operation,
            ),
        )

    def require_bot_token(self) -> str:
        if not self.telegram.bot_token:
            raise RuntimeError(
                "BOT_TOKEN is empty. Add it to .env.d/telegram.env (or legacy .env) before starting the bot."
            )
        return self.telegram.bot_token


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings.from_env()


def reload_settings() -> AppSettings:
    get_settings.cache_clear()
    return get_settings()
