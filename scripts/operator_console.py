"""Unified operator and anime-exploration console for native binaries and Docker.

Commands call the same service layer as Telegram, never Telegram handlers.
An interactive REPL retains the search and selected anime context. One-shot
commands produce readable text or machine-readable JSON without starting polling.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
import json
import mimetypes
from pathlib import Path
import shlex
import sys
from typing import Any

from core.image_search import ImageSearchInput
from core.models import SearchCandidate


class ConsoleError(Exception):
    """Recoverable operator input error, without exposing credentials."""


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return number


def _serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serializable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, (str, float, int, bool)):
        return value
    return str(value)


def format_output(data: Any, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(_serializable(data), ensure_ascii=False, indent=2)
    if isinstance(data, str):
        return data
    if data is None:
        return "No data."
    if isinstance(data, list):
        if not data:
            return "No results."
        return "\n".join(
            f"{index}. {format_output(item)}"
            for index, item in enumerate(data, 1)
        )
    if isinstance(data, dict):
        if not data:
            return "No data."
        lines = []
        for key, val in data.items():
            if isinstance(val, (dict, list, tuple)):
                lines.append(f"{key}:")
                for subline in format_output(val).splitlines():
                    lines.append(f"  {subline}")
            else:
                lines.append(f"{key}: {val if val is not None else '—'}")
        return "\n".join(lines)
    if is_dataclass(data):
        return format_output(_serializable(data))
    return str(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anime-db-bot cli",
        description="Run anime discovery, moderation, configuration and diagnostics.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON.")
    parser.add_argument("--locale", default="en", choices=("en", "ru", "ja", "de", "es", "fr", "it"))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("help", help="Show all available commands.")
    commands.add_parser("ping", help="Show current console backend availability.")
    commands.add_parser("status", help="Show running supervisor status for this home.")
    commands.add_parser("sources", help="List available runtime source backends.")
    commands.add_parser(
        "source-health", help="Show console-local source health (not bot process counters)."
    )
    commands.add_parser("metrics", help="Show console-local request statistics.")
    commands.add_parser("users", help="List Telegram users in persistent storage.")
    commands.add_parser("image-providers", help="Show image search providers and quotas.")

    search = commands.add_parser("search", help="Search anime titles across providers.")
    search.add_argument("query", nargs="+")
    search.add_argument("--source", help="Comma-separated: mal,shikimori,anilist,kitsu.")
    search.add_argument("--limit", type=_positive, default=20)

    direct = commands.add_parser("lookup", help="Find by a confirmed source ID.")
    direct.add_argument("identity", help="Example: mal:5114, anilist:5114, or 5114.")

    anime = commands.add_parser("anime", help="Open an anime by ID or title.")
    anime.add_argument("query", nargs="+")
    anime.add_argument("--section", default="main", help="main, related, episodes, etc.")
    anime.add_argument("--source", help="Optional episode backend or source name.")
    anime.add_argument("--episode", type=_positive, default=1)
    anime.add_argument("--page", type=_positive, default=1)
    anime.add_argument("--limit", type=_positive, default=20)

    open_item = commands.add_parser("open", help="Select a numbered search result.")
    open_item.add_argument("index", type=_positive)

    view = commands.add_parser("view", help="View a section of the selected anime.")
    view.add_argument(
        "section", choices=(
            "main", "titles", "ratings", "description", "links", "staff",
            "voices", "characters", "popularity", "screenshots", "trailers",
            "episodes", "episode", "timestamps", "themes", "related",
            "recommendations",
        )
    )
    view.add_argument("--source", help="Optional provider/source filter.")
    view.add_argument("--episode", type=_positive, default=1)
    view.add_argument("--page", type=_positive, default=1)
    view.add_argument("--limit", type=_positive, default=20)

    picture = commands.add_parser("image", help="Search by a local image or HTTPS image URL.")
    picture.add_argument("input", help="Path or HTTPS URL.")
    picture.add_argument(
        "--provider", default="trace_moe,whatanimeisthis",
        help="Comma-separated image-recognition backend keys.",
    )
    picture.add_argument("--limit", type=_positive, default=10)

    user = commands.add_parser("user", help="Inspect user locale and active ban.")
    user.add_argument("user_id", type=_positive)
    language = commands.add_parser("language", help="Read/change a user's Telegram locale.")
    language.add_argument("user_id", type=_positive)
    language.add_argument("target_locale", nargs="?", choices=("en", "ru", "ja", "de", "es", "fr", "it"))

    ban = commands.add_parser("ban", help="Ban one Telegram user, optionally temporarily.")
    ban.add_argument("user_id", type=_positive)
    ban.add_argument("--seconds", type=_positive, help="Ban duration; omit for permanent.")
    ban.add_argument("--reason", default="")
    ban.add_argument("--yes", action="store_true", help="Confirm non-interactively.")

    unban = commands.add_parser("unban", help="Unban one Telegram user.")
    unban.add_argument("user_id", type=_positive)
    unban.add_argument("--yes", action="store_true")

    mapping = commands.add_parser("mapping", help="Manage the independent Mapping DB.")
    mp = mapping.add_subparsers(dest="mapping_action", required=True)
    mp.add_parser("status")
    mp.add_parser("list")
    hist = mp.add_parser("history")
    hist.add_argument("--limit", type=_positive, default=20)
    update = mp.add_parser("update")
    update.add_argument("--force", action="store_true")
    update.add_argument("--yes", action="store_true")
    setmap = mp.add_parser("set")
    setmap.add_argument("action", choices=("accept", "reject"))
    setmap.add_argument("source")
    setmap.add_argument("source_id")
    setmap.add_argument("target")
    setmap.add_argument("target_id")
    setmap.add_argument("--reason", required=True)
    setmap.add_argument("--yes", action="store_true")
    delmap = mp.add_parser("delete")
    delmap.add_argument("source")
    delmap.add_argument("source_id")
    delmap.add_argument("target")
    delmap.add_argument("target_id")
    delmap.add_argument("--reason", required=True)
    delmap.add_argument("--yes", action="store_true")

    config = commands.add_parser("config", help="List/check settings (no secret values).")
    cp = config.add_subparsers(dest="config_action", required=True)
    cp.add_parser("list")
    cp.add_parser("check")
    explain = cp.add_parser("explain")
    explain.add_argument("key")

    logs = commands.add_parser("logs", help="Tail launcher or application logs.")
    logs.add_argument("--lines", type=_positive, default=60)
    logs.add_argument("--file", choices=("launcher", "bot", "api", "users", "errors"),
                      default="launcher")
    return parser


def _confirmed(prompt: str, *, yes: bool, interactive: bool) -> None:
    if yes:
        return
    if not interactive or not sys.stdin.isatty():
        raise ConsoleError("Confirmation required: repeat with --yes.")
    answer = input(f"{prompt} Type 'yes' to continue: ").strip()
    if answer != "yes":
        raise ConsoleError("Operation cancelled; no changes made.")


def _parse_source_id(value: str) -> tuple[str | None, str] | None:
    from bots.telegram.search.handlers import _parse_direct_query

    # Use the same parser/alias normalization as Telegram.
    supported = ("mal", "shikimori", "anilist", "kitsu", "anidb", "anisearch")
    return _parse_direct_query(value, supported)


class OperatorConsole:
    """One session with shared application services, settings and selected anime."""

    def __init__(self, context: Any, *, interactive: bool = False) -> None:
        self.context = context
        self.interactive = interactive
        self.candidates: list[SearchCandidate] = []
        self.selected: Any = None
        self.locale = "en"
        self.as_json = False
        self.pending_mapping: list[str] | None = None

    async def run_tokens(
        self, tokens: list[str], parser: argparse.ArgumentParser
    ) -> Any:
        """Support regular CLI commands and familiar Telegram slash syntax."""
        if not tokens:
            return await self.execute(parser.parse_args(["help"]))
        command, *rest = tokens
        command = command.casefold()
        if command in {"cancel", "/cancel"}:
            self.candidates.clear()
            self.selected = None
            self.pending_mapping = None
            return "Pending selections and mapping changes cancelled."
        if command in {"settings", "/settings"}:
            return (
                "Telegram user language: language USER_ID [en|ru|ja|de|es|fr|it].\n"
                "Project settings: config list | config check | config explain KEY."
            )
        if command == "/mapping_cancel":
            self.pending_mapping = None
            return "Pending mapping mutation cancelled."
        if command == "/mapping_confirm":
            if self.pending_mapping is None:
                raise ConsoleError("No pending mapping mutation.")
            staged = list(self.pending_mapping)
            self.pending_mapping = None
            return await self.execute(parser.parse_args([*staged, "--yes"]))
        if command in {"/mapping_set", "/mapping_delete"}:
            if not self.interactive:
                raise ConsoleError(
                    "Use the interactive console for staged slash mapping changes. "
                    "For scripts use mapping set/delete with --reason and --yes."
                )
            if command == "/mapping_set":
                if len(rest) < 6:
                    raise ConsoleError(
                        "Usage: /mapping_set accept|reject SOURCE ID TARGET ID REASON"
                    )
                action, source, source_id, target, target_id, *reason_parts = rest
                staged = [
                    "mapping", "set", action, source, source_id, target, target_id,
                    "--reason", " ".join(reason_parts),
                ]
            else:
                if len(rest) < 5:
                    raise ConsoleError(
                        "Usage: /mapping_delete SOURCE ID TARGET ID REASON"
                    )
                source, source_id, target, target_id, *reason_parts = rest
                staged = [
                    "mapping", "delete", source, source_id, target, target_id,
                    "--reason", " ".join(reason_parts),
                ]
            parser.parse_args(staged)
            if not " ".join(reason_parts).strip():
                raise ConsoleError("An audit reason is required.")
            self.pending_mapping = staged
            return (
                "Staged: " + " ".join(staged[:7])
                + "\nConfirm: /mapping_confirm | Cancel: /mapping_cancel"
            )

        aliases = {
            "/start": "help", "/ping": "ping", "/users": "users",
            "/status": "status", "/source_health": "source-health",
            "/unban": "unban",
        }
        if command == "/anime":
            if not rest:
                return "Usage: /anime TITLE or /anime mal:ID. Search, then open INDEX."
            direct_id = _parse_source_id(" ".join(rest))
            tokens = ["lookup" if direct_id is not None else "search", *rest]
        elif command == "/mapping_overrides":
            tokens = ["mapping", "list"]
        elif command == "/mapping_history":
            tokens = ["mapping", "history", *(["--limit", rest[0]] if rest else [])]
        elif command == "/ban":
            if not rest:
                raise ConsoleError("Usage: /ban USER_ID [SECONDS] [REASON]")
            user, *remaining = rest
            tokens = ["ban", user]
            if remaining and remaining[0].isdecimal():
                tokens.extend(["--seconds", remaining.pop(0)])
            if remaining:
                tokens.extend(["--reason", " ".join(remaining)])
        elif command in aliases:
            tokens = [aliases[command], *rest]
        return await self.execute(parser.parse_args(tokens))

    async def _find(self, query: str) -> SearchCandidate:
        value = query.strip()
        if not value:
            raise ConsoleError("Anime title or source ID is required.")
        if value.isdecimal() or (":" in value and value.split(":", 1)[1].isdecimal()):
            identity = _parse_source_id(value)
            if identity is None:
                raise ConsoleError("Unknown source ID. Use mal:ID, anilist:ID, etc.")
            source, identifier = identity
            result = await self.context.direct_lookup.lookup(
                identifier, source_keys=(source,) if source else None
            )
        else:
            result = await self.context.search_service.collect(value)
        if not result.items:
            raise ConsoleError("No anime found. Check the source ID or try another title.")
        self.candidates = list(result.items)
        return self.candidates[0]

    async def _open(self, candidate: SearchCandidate) -> Any:
        anime = await self.context.aggregator.aggregate(candidate)
        try:
            await self.context.imdb_rating_service.enrich(anime)
        except Exception:
            pass  # IMDb is a nonessential rating-only supplement.
        self.selected = anime
        return anime

    async def _section(self, anime: Any, section: str, args: Any) -> Any:
        from bots.telegram.card.views.sections import anime_sections
        identity = anime.identity
        page = max(1, getattr(args, "page", 1))
        limit = min(100, max(1, getattr(args, "limit", 20)))
        source = (getattr(args, "source", None) or "").strip()
        episode_number = max(1, getattr(args, "episode", 1))

        if section == "main":
            return anime_sections(anime, locale=self.locale)["main"]
        if section == "titles":
            return {"titles": anime.titles, "synonyms": anime.synonyms,
                    "by_source": anime.titles_by_source,
                    "external_ids": anime.external_ids, "amdb_id": anime.amdb_id}
        if section == "links":
            return anime.urls
        if section == "ratings":
            return {"scores": anime.scores, "average": anime.score_average,
                    "votes": anime.score_votes, "ranking": anime.rank,
                    "popularity_rank": anime.popularity_rank}
        if section in {"related", "recommendations"}:
            loader = (self.context.related_anime_service.load_relations
                      if section == "related"
                      else self.context.related_anime_service.load_recommendations)
            result = await loader(identity)
            items = result.items
            begin = (page - 1) * limit
            return {
                "title": anime.title, "section": section, "total": len(items),
                "page": page, "items": items[begin:begin + limit],
                "by_source": result.source_names, "errors": result.errors,
            }
        if section in {"description", "screenshots", "popularity", "characters", "trailers"}:
            services = {
                "description": self.context.description_service,
                "screenshots": self.context.screenshot_service,
                "popularity": self.context.popularity_service,
                "characters": self.context.character_service,
                "trailers": self.context.trailer_service,
            }
            result = await services[section].load(identity)
            data = dict(result.by_source)
            if source:
                data = {key: value for key, value in data.items()
                        if key.casefold() == source.casefold()
                        or result.key_by_source.get(key, "").casefold() == source.casefold()}
            return {"title": anime.title, "section": section, "by_source": data,
                    "errors": result.errors}
        if section in {"staff", "voices"}:
            progress = self.context.people_progress_service.build_progress(anime, section)
            await self.context.people_progress_service.ensure(
                anime, section, progress,
                source=source or None,
                requested_page=page - 1,
            )
            by_source = getattr(anime, f"{section}_by_source")
            if source:
                by_source = {
                    key: value for key, value in by_source.items()
                    if key.casefold() == source.casefold()
                }
            return {
                "title": anime.title, "section": section,
                "by_source": {
                    key: people[(page - 1) * limit:page * limit]
                    for key, people in by_source.items()
                },
            }
        if section in {"episodes", "episode"}:
            available = self.context.episode_service.available_backends(identity)
            if not available:
                return {"title": anime.title, "section": section, "items": [],
                        "note": "No episode source is available."}
            backend = source or available[0]
            if backend not in available:
                raise ConsoleError(
                    "Episode source not available. Choose: " + ", ".join(available)
                )
            if section == "episode":
                record = await self.context.episode_service.load_episode(
                    identity, episode_number, backend=backend
                )
                return {"title": anime.title, "backend": backend,
                        "episode": record}
            results = await self.context.episode_service.load_page(
                identity, page=page, backend=backend
            )
            return {"title": anime.title, "backend": backend, "page": page,
                    "items": results.items[:limit], "has_next": results.has_next}
        if section == "timestamps":
            from services.media.episode_segments import aggregate_segments
            by_source, errors = await self.context.episode_segment_service.load_all(
                identity, episode_number
            )
            if source:
                by_source = {k: v for k, v in by_source.items() if k == source}
            return {"title": anime.title, "episode": episode_number,
                    "by_source": by_source,
                    "consensus": aggregate_segments(by_source), "errors": errors}
        if section == "themes":
            data = await self.context.anime_theme_service.load_episode_themes(
                identity, episode_number
            )
            return {"title": anime.title, "episode": episode_number, "themes": data}
        raise ConsoleError(f"Unsupported card section: {section}")

    async def _search(self, args: Any) -> Any:
        query = " ".join(args.query)
        if args.source:
            from bots.telegram.search.handlers import _parse_scope, _service_for

            keys = _parse_scope(args.source)
            if not keys:
                raise ConsoleError(
                    "Invalid source. Use mal,anilist,shikimori,kitsu or all."
                )
            service = _service_for(
                keys, self.context.search_service,
                {str(key): val for key, val in self.context.search_provider_map.items()},
                {},
            )
        else:
            service = self.context.search_service
        results = await service.collect(query)
        self.candidates = list(results.items)
        limit = min(100, args.limit)
        return {
            "query": query, "count": len(results.items),
            "complete": results.complete, "errors": results.errors,
            "items": [
                {"index": i, "title": item.title,
                 "year": item.year, "external_ids": item.external_ids,
                 "scores": item.scores}
                for i, item in enumerate(results.items[:limit], 1)
            ],
            "hint": "Run 'open 1' to open a result in this console session.",
        }

    async def _image(self, args: Any) -> Any:
        from urllib.parse import urlparse

        value = args.input.strip()
        if value.startswith("https://"):
            parsed = urlparse(value)
            if not parsed.hostname:
                raise ConsoleError("Image URL must be a valid HTTPS URL.")
            image = ImageSearchInput(url=value)
        else:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise ConsoleError("Image file does not exist.")
            max_bytes = self.context.settings.sources.image_search_max_bytes
            if path.stat().st_size > max_bytes:
                raise ConsoleError(f"Image exceeds {max_bytes} bytes.")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if not mime.startswith("image/"):
                raise ConsoleError("Provide a supported image file.")
            image = ImageSearchInput(
                content=path.read_bytes(), filename=path.name, content_type=mime
            )
        keys = tuple(k.strip() for k in args.provider.split(",") if k.strip())
        results = await self.context.image_search_service.search(image, keys)
        self.candidates = [item.candidate for item in results.items]
        return {
            "results": [
                {"index": i, "candidate": item.candidate, "matches": item.matches}
                for i, item in enumerate(results.items[:min(100, args.limit)], 1)
            ],
            "errors": results.errors,
        }

    async def _mapping(self, args: Any) -> Any:
        from mappings.resolver import (
            delete_mapping_override, list_mapping_override_history,
            list_mapping_overrides, set_mapping_override,
        )
        from mappings.updater import mapping_status, update_mapping_database

        if args.mapping_action == "status":
            return await asyncio.to_thread(mapping_status)
        if args.mapping_action == "list":
            return await asyncio.to_thread(list_mapping_overrides)
        if args.mapping_action == "history":
            return await asyncio.to_thread(
                list_mapping_override_history,
                limit=args.limit,
                newest_first=True,
            )
        if args.mapping_action == "update":
            _confirmed("Download/replace the local mapping catalog?", yes=args.yes,
                       interactive=self.interactive)
            result = await asyncio.to_thread(update_mapping_database, force=args.force)
            return {"updated": result}
        if args.mapping_action == "set":
            if not args.reason.strip():
                raise ConsoleError("A nonempty audit reason is required.")
            _confirmed("Write directional mapping override?", yes=args.yes,
                       interactive=self.interactive)
            result = await asyncio.to_thread(
                set_mapping_override, args.source, args.source_id,
                args.target, args.target_id,
                action=args.action, reason=args.reason, author="operator-console"
            )
            return {"override": result}
        if args.mapping_action == "delete":
            if not args.reason.strip():
                raise ConsoleError("A nonempty audit reason is required.")
            _confirmed("Delete directional mapping override?", yes=args.yes,
                       interactive=self.interactive)
            removed = await asyncio.to_thread(
                delete_mapping_override, args.source, args.source_id,
                args.target, args.target_id,
                reason=args.reason, author="operator-console"
            )
            return {"deleted": removed}
        raise ConsoleError("Unsupported mapping operation.")

    async def execute(self, args: argparse.Namespace) -> Any:
        from services.users.bans import ban_user, get_active_ban, unban_user
        from services.users.settings import (
            get_user_locale, list_platform_user_ids, set_user_locale,
        )

        name = args.command
        self.locale = args.locale
        self.as_json = args.json
        if name == "help":
            return (
                "Commands: ping, status, sources, source-health, metrics, search, "
                "lookup, anime, open, view, image, image-providers, users, user, "
                "language, ban, unban, mapping, config, logs, cancel.\n"
                "Telegram aliases: /start, /ping, /anime, /users, /status, "
                "/source_health, /ban, /unban, /settings, /cancel, "
                "/mapping_overrides, /mapping_history, /mapping_set, "
                "/mapping_delete, /mapping_confirm, /mapping_cancel.\n"
                "Use '<command> --help' for arguments; 'exit' leaves the console."
            )
        if name == "ping":
            return {"ok": True, "transport": "local-console",
                    "telegram_polling_started": False}
        if name == "status":
            from scripts.release_entry import current_pid, runtime_home
            from services.runtime.status_snapshot import read_live_snapshot

            home = runtime_home()
            pid = current_pid(home)
            live = read_live_snapshot(home)
            return {
                "running": pid is not None or live is not None,
                "supervisor_pid": pid,
                "worker_pid": live["worker_pid"] if live else None,
                "home": home,
                "live_telegram": live is not None,
                "uptime_seconds": live.get("uptime_seconds") if live else None,
                "summary": live.get("summary") if live else None,
            }
        if name in {"sources", "source-health", "metrics"}:
            from scripts.release_entry import runtime_home
            from services.runtime.status_snapshot import read_live_snapshot

            live = read_live_snapshot(runtime_home())
            if live:
                if name == "sources":
                    return live["sources"]
                if name == "source-health":
                    return {"live_telegram": True, "sources": live["sources"]}
                return {
                    "live_telegram": True,
                    "uptime_seconds": live["uptime_seconds"],
                    "windows": live["windows"],
                    "caches": live["caches"],
                }
            if name == "sources":
                return [asdict(item) for item in self.context.runtime_source_descriptors()]
            if name == "source-health":
                return {
                    "live_telegram": False,
                    "notice": "No fresh Telegram worker snapshot. Showing only "
                              "this console's source-health state.",
                    "sources": {
                        item.key: asdict(self.context.source_health.snapshot(item.key))
                        for item in self.context.runtime_source_descriptors()
                    },
                }
            return {
                "live_telegram": False,
                "notice": "Only requests from this console session are counted.",
                "stats": self.context.runtime_metrics.window(3600),
            }
        if name == "users":
            ids = await asyncio.to_thread(list_platform_user_ids, "telegram")
            return {"count": len(ids), "user_ids": ids}
        if name == "user":
            user_id = args.user_id
            return {
                "user_id": user_id, "locale": get_user_locale(user_id),
                "ban": get_active_ban("telegram", user_id),
            }
        if name == "language":
            if args.target_locale:
                return {
                    "user_id": args.user_id,
                    "locale": set_user_locale(args.user_id, args.target_locale),
                }
            return {"user_id": args.user_id, "locale": get_user_locale(args.user_id)}
        if name == "ban":
            _confirmed(f"Ban Telegram user {args.user_id}?", yes=args.yes,
                       interactive=self.interactive)
            return {"ban": ban_user(
                "telegram", args.user_id, banned_by=0,
                duration_seconds=args.seconds, reason=args.reason or None
            )}
        if name == "unban":
            _confirmed(f"Unban Telegram user {args.user_id}?", yes=args.yes,
                       interactive=self.interactive)
            return {"unbanned": unban_user("telegram", args.user_id)}
        if name == "search":
            return await self._search(args)
        if name == "lookup":
            value = args.identity.strip()
            direct_id = _parse_source_id(value)
            if direct_id is None:
                raise ConsoleError("Expected a source-prefixed numeric ID.")
            source, identifier = direct_id
            results = await self.context.direct_lookup.lookup(
                identifier, source_keys=(source,) if source else None
            )
            self.candidates = list(results.items)
            return {
                "query": value,
                "count": len(results.items),
                "items": [
                    {"index": index, "title": item.title,
                     "external_ids": item.external_ids}
                    for index, item in enumerate(results.items, 1)
                ],
                "errors": results.errors,
            }
        if name == "anime":
            candidate = await self._find(" ".join(args.query))
            anime = await self._open(candidate)
            return await self._section(anime, args.section, args)
        if name == "open":
            if args.index > len(self.candidates):
                raise ConsoleError("No such result; run search first.")
            anime = await self._open(self.candidates[args.index - 1])
            return await self._section(anime, "main", args)
        if name == "view":
            if self.selected is None:
                raise ConsoleError("Select an anime first: search ...; open 1")
            return await self._section(self.selected, args.section, args)
        if name == "image":
            return await self._image(args)
        if name == "image-providers":
            return await self.context.image_search_service.availability()
        if name == "mapping":
            return await self._mapping(args)
        if name == "config":
            from scripts.config_report import example_groups, validate_config

            if args.config_action == "list":
                return example_groups()
            if args.config_action == "explain":
                matches = [
                    name for name, keys in example_groups().items()
                    if args.key in keys
                ]
                return {"key": args.key, "documented_in": matches}
            return {"valid": True, "unknown_keys": validate_config()}
        if name == "logs":
            from scripts.release_entry import runtime_home

            home = runtime_home()
            if args.file == "launcher":
                file = home / "logs" / "launcher.log"
            else:
                directories = sorted(
                    (home / "logs").glob("????.??.??"), reverse=True
                )
                file = (directories[0] if directories else home / "logs") / (
                    f"{args.file}.log"
                )
            if not file.is_file():
                return {"file": file, "lines": [], "notice": "No log file yet."}
            # Tail without reading unbounded log history into memory.
            from collections import deque

            with file.open(encoding="utf-8", errors="replace") as stream:
                lines = list(deque(stream, maxlen=min(2000, args.lines)))
            return {"file": file, "lines": [line.rstrip() for line in lines]}
        raise ConsoleError(f"Unknown console command {name}")


async def run_console(
    tokens: list[str] | None = None, *, interactive: bool = False
) -> int:
    """Reuse runtime services in one REPL without starting Telegram polling."""
    from core.settings import get_settings
    from services.runtime.app_context import AppContext, activate_app_context

    parser = build_parser()
    settings = get_settings()
    context = AppContext.create(settings)
    session = OperatorConsole(context, interactive=interactive)
    try:
        with activate_app_context(context):
            if not interactive:
                try:
                    words = tokens or ["help"]
                    result = await session.run_tokens(words, parser)
                    print(format_output(result, as_json="--json" in words), flush=True)
                    return 0
                except ConsoleError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 2
            print("anime-db-bot operator console")
            print("Commands: help, search, anime, open, view, users, mapping, etc.")
            print("Type 'exit' or 'quit' to leave. This console does not start polling.")
            while True:
                try:
                    raw = input("anime-db-bot> ")
                except (KeyboardInterrupt, EOFError):
                    print("")
                    return 0
                if not raw.strip():
                    continue
                if raw.strip().casefold() in {"exit", "quit"}:
                    return 0
                try:
                    words = shlex.split(raw)
                    result = await session.run_tokens(words, parser)
                    print(format_output(result, as_json="--json" in words), flush=True)
                except (ConsoleError, ValueError) as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                except SystemExit:
                    continue
                except Exception as exc:
                    # Error types, not credentials or raw upstream error strings.
                    print(
                        f"Command failed: {type(exc).__name__}. "
                        "Check API availability and application logs.",
                        file=sys.stderr,
                    )
    finally:
        await context.aclose()


def main(argv: list[str] | None = None) -> int:
    options = list(sys.argv[1:] if argv is None else argv)
    return asyncio.run(run_console(options, interactive=not options))


if __name__ == "__main__":
    raise SystemExit(main())
