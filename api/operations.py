"""One typed, scope-controlled gateway to console/application operations.

REST and GraphQL use these same actions. No request can submit raw shell
arguments or an arbitrary Python/Telegram command. Runtime context is shared,
but a fresh operator session is allocated per HTTP request.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import suppress
from dataclasses import dataclass
import io
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from core.settings import AppSettings
from scripts.operator_console import (
    ConsoleError,
    OperatorConsole,
    _serializable,
    build_parser,
)
from services.runtime.app_context import activate_app_context


@dataclass(frozen=True)
class OperationSpec:
    scope: str
    description: str
    writes: bool = False


OPERATIONS: dict[str, OperationSpec] = {
    "ping": OperationSpec("read", "API connectivity without Telegram polling"),
    "status": OperationSpec("read", "Running worker status"),
    "sources": OperationSpec("read", "Configured source capabilities"),
    "source-health": OperationSpec("read", "Source circuit health"),
    "metrics": OperationSpec("read", "Aggregated low-cardinality metrics"),
    "search": OperationSpec("read", "Federated title search"),
    "lookup": OperationSpec("read", "Confirmed direct source ID lookup"),
    "anime": OperationSpec("read", "Anime metadata and lazy card sections"),
    "image": OperationSpec("read", "Anime image recognition from uploaded bytes"),
    "image-providers": OperationSpec("read", "Image recognition providers"),
    "users": OperationSpec("admin", "List persisted user IDs"),
    "user": OperationSpec("admin", "User preferences and ban state"),
    "language": OperationSpec("admin", "Read a saved user language"),
    "language.set": OperationSpec("admin", "Set a user language", True),
    "ban": OperationSpec("admin", "Ban a user", True),
    "unban": OperationSpec("admin", "Remove a ban", True),
    "mapping.status": OperationSpec("read", "Mapping database status"),
    "mapping.list": OperationSpec("admin", "Manual mapping overrides"),
    "mapping.history": OperationSpec("admin", "Mapping audit log"),
    "mapping.update": OperationSpec("admin", "Update mapping dataset", True),
    "mapping.set": OperationSpec("admin", "Set a directional override", True),
    "mapping.delete": OperationSpec("admin", "Delete a directional override", True),
    "index.imdb": OperationSpec("admin", "Build the IMDb ratings index", True),
    "index.baha": OperationSpec("admin", "Build the Baha skip index", True),
    "config.list": OperationSpec("admin", "List documented settings"),
    "config.check": OperationSpec("admin", "Validate loaded settings"),
    "config.get": OperationSpec("admin", "Masked setting lookup"),
    "config.explain": OperationSpec("admin", "Documented setting metadata"),
    "config.set": OperationSpec("admin", "Update a setting", True),
    "config.reset": OperationSpec("admin", "Delete a local setting override", True),
    "logs": OperationSpec("admin", "Bounded local application log tail"),
}
SECTIONS = frozenset(
    ("main", "titles", "ratings", "description", "links", "staff", "voices",
     "characters", "popularity", "screenshots", "trailers", "episodes",
     "episode", "timestamps", "themes", "related", "recommendations")
)
LOCALES = frozenset(("en", "ru", "ja", "de", "es", "fr", "it"))
_SAFE_SETTING = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_MIME_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class ApiOperationError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _string(
    data: Mapping[str, Any], key: str, *, required: bool = False,
    maximum: int = 256, default: str = "",
) -> str:
    val = data.get(key, default)
    if not isinstance(val, (str, int)) or isinstance(val, bool):
        raise ApiOperationError(400, f"{key} must be text")
    result = str(val).strip()
    if (required and not result) or len(result) > maximum:
        raise ApiOperationError(400, f"{key} must contain 1-{maximum} characters")
    return result


def _number(
    data: Mapping[str, Any], key: str, default: int, low: int, high: int,
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ApiOperationError(400, f"{key} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApiOperationError(400, f"{key} must be an integer") from exc
    if integer < low or integer > high:
        raise ApiOperationError(400, f"{key} must be between {low} and {high}")
    return integer


def _confirmed(data: Mapping[str, Any]) -> None:
    if data.get("confirm") is not True:
        raise ApiOperationError(
            409, "This operation changes state. Send confirm=true explicitly."
        )


def _key(data: Mapping[str, Any]) -> str:
    key = _string(data, "key", required=True, maximum=96)
    if not _SAFE_SETTING.fullmatch(key):
        raise ApiOperationError(400, "Invalid configuration key")
    return key


def _assemble(name: str, data: Mapping[str, Any]) -> list[str]:
    """Convert typed input to the existing console parser, with a fixed verb."""
    if name in {"ping", "status", "sources", "source-health", "metrics",
                "users", "image-providers"}:
        return [name]
    if name == "search":
        tokens = ["search", _string(data, "query", required=True)]
        if data.get("source"):
            tokens.extend(["--source", _string(data, "source")])
        tokens.extend(["--limit", str(_number(data, "limit", 20, 1, 100))])
        return tokens
    if name == "lookup":
        return ["lookup", _string(data, "identity", required=True)]
    if name == "anime":
        section = _string(data, "section", default="main")
        if section not in SECTIONS:
            raise ApiOperationError(400, "Unsupported anime section")
        query = _string(data, "query", required=True)
        tokens = ["anime", query, "--section", section]
        if data.get("source"):
            tokens.extend(["--source", _string(data, "source")])
        tokens.extend(["--episode", str(_number(data, "episode", 1, 1, 100000)),
                       "--page", str(_number(data, "page", 1, 1, 10000)),
                       "--limit", str(_number(data, "limit", 20, 1, 100))])
        return tokens
    if name == "image":
        return ["image", "__SECURE_UPLOAD__",
                "--provider", _string(data, "provider",
                                      default="trace_moe,whatanimeisthis"),
                "--limit", str(_number(data, "limit", 10, 1, 100))]
    if name in {"user", "language", "language.set", "ban", "unban"}:
        user_id = str(_number(data, "user_id", 0, 1, 9223372036854775807))
        if name == "user":
            return ["user", user_id]
        if name == "language":
            return ["language", user_id]
        if name == "language.set":
            locale = _string(data, "locale", required=True)
            if locale not in LOCALES:
                raise ApiOperationError(400, "Unsupported locale")
            return ["language", user_id, locale]
        if name == "unban":
            return ["unban", user_id, "--yes"]
        tokens = ["ban", user_id, "--yes"]
        if data.get("seconds") is not None:
            tokens.extend(["--seconds", str(_number(data, "seconds", 0, 1, 315360000))])
        if data.get("reason"):
            tokens.extend(["--reason", _string(data, "reason", maximum=256)])
        return tokens
    if name.startswith("mapping."):
        action = name.split(".", 1)[1]
        if action in {"status", "list"}:
            return ["mapping", action]
        if action == "history":
            return ["mapping", "history", "--limit",
                    str(_number(data, "limit", 20, 1, 100))]
        if action == "update":
            return ["mapping", "update", "--yes",
                    *(["--force"] if data.get("force") is True else [])]
        source = _string(data, "source", required=True, maximum=40)
        source_id = _string(data, "source_id", required=True, maximum=80)
        target = _string(data, "target", required=True, maximum=40)
        target_id = _string(data, "target_id", required=True, maximum=80)
        reason = _string(data, "reason", required=True, maximum=256)
        tokens = ["mapping", action]
        if action == "set":
            relation = _string(data, "action", required=True)
            if relation not in {"accept", "reject"}:
                raise ApiOperationError(400, "action must be accept or reject")
            tokens.append(relation)
        return [*tokens, source, source_id, target, target_id,
                "--reason", reason, "--yes"]
    if name.startswith("config."):
        action = name.split(".", 1)[1]
        if action in {"list", "check"}:
            return ["config", action]
        if action in {"explain", "get"}:
            return ["config", action, _key(data)]
        if action == "reset":
            return ["config", "reset", _key(data), "--yes"]
        return ["config", "set", _key(data), _string(data, "value", maximum=16384),
                "--yes"]
    if name.startswith("index."):
        kind = name.split(".", 1)[1]
        tokens = ["index", kind, "--yes"]
        for key in ("mappings_db", "output"):
            if data.get(key):
                tokens.extend(["--" + key.replace("_", "-"),
                               _string(data, key, maximum=1024)])
        if kind == "imdb":
            for key in ("source_file", "url"):
                if data.get(key):
                    tokens.extend(["--" + key.replace("_", "-"),
                                   _string(data, key, maximum=2048)])
        else:
            for key in ("data_url", "bahamut_api_url"):
                if data.get(key):
                    tokens.extend(["--" + key.replace("_", "-"),
                                   _string(data, key, maximum=2048)])
            tokens.extend(["--concurrency",
                           str(_number(data, "concurrency", 12, 1, 100))])
        return tokens
    if name == "logs":
        kind = _string(data, "file", default="launcher")
        if kind not in {"launcher", "bot", "api", "users", "errors"}:
            raise ApiOperationError(400, "Unsupported log file")
        return ["logs", "--file", kind, "--lines",
                str(_number(data, "lines", 60, 1, 250))]
    raise ApiOperationError(404, "Unknown API operation")


async def _execute_image(
    console: OperatorConsole, name: str, data: Mapping[str, Any],
    args: list[str], settings: AppSettings,
) -> Any:
    from PIL import Image, UnidentifiedImageError

    kind = _string(data, "content_type", default="image/jpeg")
    suffix = _MIME_SUFFIXES.get(kind)
    if suffix is None:
        raise ApiOperationError(400, "Allowed image MIME types: JPEG, PNG, WebP")
    encoded = _string(data, "content_base64", required=True, maximum=16777216)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiOperationError(400, "Invalid base64 image bytes") from exc
    if len(payload) > settings.sources.image_search_max_bytes:
        raise ApiOperationError(413, "Image exceeds configured image-search byte limit")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ApiOperationError(415, "Unsupported image encoding")
            if image.width * image.height > settings.telegram.image_max_pixels:
                raise ApiOperationError(413, "Image exceeds the pixel limit")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ApiOperationError(400, "Invalid image file") from exc
    with tempfile.TemporaryDirectory(prefix="anime-api-image-") as temporary:
        file = Path(temporary) / ("upload" + suffix)
        file.write_bytes(payload)
        args[1] = str(file)
        parsed = build_parser().parse_args(args)
        return await console.execute(parsed)


async def execute_operation(
    context: Any, name: str, payload: Mapping[str, Any] | None,
    *, role: str,
) -> Any:
    """Run exactly one registered API operation under a scoped local context."""
    spec = OPERATIONS.get(name)
    if spec is None:
        raise ApiOperationError(404, "Unknown API operation")
    if spec.scope == "admin" and role != "admin":
        raise ApiOperationError(403, "Administrator scope required")
    data = {} if payload is None else payload
    if not isinstance(data, Mapping):
        raise ApiOperationError(400, "Operation input must be a JSON object")
    if spec.writes:
        _confirmed(data)

    tokens = _assemble(name, data)
    console = OperatorConsole(context, interactive=False)
    # Use the same AppContext-owned caches, SQLite/Postgres repositories and
    # rate limiters as other entrypoints. ContextVars isolate parallel calls.
    try:
        with activate_app_context(context):
            if name == "image":
                result = await _execute_image(
                    console, name, data, tokens, context.settings
                )
            else:
                parsed = build_parser().parse_args(tokens)
                result = await console.execute(parsed)
        return _serializable(result)
    except ApiOperationError:
        raise
    except ConsoleError as exc:
        raise ApiOperationError(400, str(exc)) from exc
    except SystemExit as exc:
        raise ApiOperationError(400, "Invalid operation arguments") from exc
    except (ValueError, FileNotFoundError) as exc:
        # Upstream exceptions may contain authentication data: never return them.
        raise ApiOperationError(422, "Invalid operation input or unavailable data") from exc
    except Exception as exc:
        raise ApiOperationError(
            502, f"Operation failed: {type(exc).__name__}"
        ) from exc
