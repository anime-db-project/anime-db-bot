from __future__ import annotations

import asyncio
import io
import logging
import secrets
from dataclasses import dataclass, field

from aiogram import F
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bots.session_store import ExpiringSessionStore
from bots.telegram.card.runtime.service import aggregate_and_send
from core.settings import get_settings
from localization import text
from services.runtime.app_context import get_app_context
from services.users.settings import get_user_locale
from services.users.watchlists import BUILTIN_LISTS, WatchEntry, entry_for, watchlists
from services.users.watchlist_import import fetch_public, parse_mal_xml


@dataclass(slots=True)
class ListView:
    owner: int
    list_id: str | None = None
    page: int = 0
    entry: WatchEntry | None = None
    rows: list = field(default_factory=list)
    revision: int = 0


def t(locale, key, **kwargs):
    return text("watchlists", key, locale=locale, **kwargs)


def label(locale, key, name):
    return t(locale, key) if key in BUILTIN_LISTS else name


def register_watchlist_handlers(dp, card_state):
    views = ExpiringSessionStore(max_entries=512, ttl_seconds=1800, name="watchlists")
    pending = ExpiringSessionStore(
        max_entries=256, ttl_seconds=300, name="watchlist_input"
    )
    busy = set()

    def settings():
        return get_settings().telegram

    def new(owner, list_id=None, entry=None):
        token = secrets.token_urlsafe(8)
        view = ListView(owner, list_id, entry=entry)
        views[token] = view
        return token, view

    def button(token, action, name, index=None):
        suffix = "" if index is None else ":" + str(index)
        return InlineKeyboardButton(
            text=str(name)[:64],
            callback_data=f"wl:{token}:{views[token].revision}:{action}{suffix}",
        )

    async def render(message, value, rows, *, edit=False):
        send = message.edit_text if edit else message.answer
        await send(
            value,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode=None,
        )

    async def lists(owner):
        store = await asyncio.to_thread(watchlists)
        return await asyncio.to_thread(
            store.lists, owner, settings().watchlist_custom_defaults
        )

    async def home(message, token, view, locale, *, edit=False):
        view.list_id = None
        view.entry = None
        view.rows = await lists(view.owner)
        rows = [
            [button(token, "list", f"{label(locale, k, n)} ({count})", i)]
            for i, (k, n, count) in enumerate(view.rows)
        ]
        rows += [
            [
                button(token, "create", t(locale, "create")),
                button(token, "import", t(locale, "import")),
            ]
        ]
        await render(message, t(locale, "title"), rows, edit=edit)

    async def choose_list(message, token, view, locale, *, edit=False):
        view.rows = await lists(view.owner)
        rows = [
            [button(token, "save", label(locale, k, n), i)]
            for i, (k, n, _) in enumerate(view.rows)
        ]
        rows.append([button(token, "card", t(locale, "back_card"))])
        await render(
            message,
            view.entry.candidate.title + "\n\n" + t(locale, "choose"),
            rows,
            edit=edit,
        )

    async def list_page(message, token, view, locale, *, edit=False):
        meta = next((x for x in await lists(view.owner) if x[0] == view.list_id), None)
        if meta is None:
            await home(message, token, view, locale, edit=edit)
            return
        view.page = max(0, min(view.page, max(0, (meta[2] - 1) // 10)))
        store = await asyncio.to_thread(watchlists)
        view.rows, count = await asyncio.to_thread(
            store.entries, view.owner, view.list_id, view.page
        )
        heading = f"{label(locale, meta[0], meta[1])} ({count})"
        rows = [
            [button(token, "entry", e.candidate.title, i)]
            for i, e in enumerate(view.rows)
        ]
        nav = []
        if view.page:
            nav.append(button(token, "prev", "◀️"))
        nav.append(
            button(token, "stay", f"{view.page + 1}/{max(1, (count + 9) // 10)}")
        )
        if (view.page + 1) * 10 < count:
            nav.append(button(token, "next", "▶️"))
        rows.append(nav)
        rows.append([button(token, "add", t(locale, "add"))])
        if view.list_id.startswith("u_"):
            rows.append([button(token, "delete_confirm", t(locale, "delete_list"))])
        rows.append([button(token, "home", t(locale, "back"))])
        await render(
            message,
            heading + ("" if count else "\n\n" + t(locale, "empty")),
            rows,
            edit=edit,
        )

    async def detail(message, token, view, locale):
        e = view.entry
        rows = [
            [
                button(token, "open", t(locale, "open")),
                button(token, "remove", t(locale, "remove")),
            ],
            [
                button(token, "progress", t(locale, "set_progress")),
                button(token, "choose", t(locale, "add_to_list")),
            ],
            [button(token, "page", t(locale, "back"))],
        ]
        info = f"{e.candidate.title}\n\n" + t(locale, "progress", count=e.progress)
        if e.score is not None:
            info += "\n" + t(locale, "score", score=e.score)
        await render(message, info, rows, edit=True)

    async def apply_import(message, owner, locale, items):
        resolver = get_app_context().identity_resolver
        limit = settings().watchlist_max_entries

        def prepare():
            return [
                (k, entry_for(c, resolver, progress=p, score=s)) for k, c, p, s in items
            ]

        values = await asyncio.to_thread(prepare)
        store = await asyncio.to_thread(watchlists)
        changed = await asyncio.to_thread(
            store.add_many, owner, values, limit, importing=True
        )
        await message.answer(
            t(locale, "import_done", count=changed, skipped=len(values) - changed)
        )
        token, view = new(owner)
        await home(message, token, view, locale)

    from aiogram import BaseMiddleware

    class ClearPendingOnCommand(BaseMiddleware):
        async def __call__(self, handler, event, data):
            if (
                isinstance(event, Message)
                and event.from_user
                and event.chat.type == "private"
                and event.text
                and event.text.startswith("/")
            ):
                command = event.text.split()[0].split("@")[0].lower()
                if command not in {"/cancel", "/lists"}:
                    pending.pop(str(event.from_user.id), None)
            return await handler(event, data)

    dp.message.outer_middleware(ClearPendingOnCommand())

    @dp.message(
        Command("cancel"),
        lambda m: bool(
            m.from_user
            and m.chat.type == "private"
            and pending.get(str(m.from_user.id))
        ),
    )
    async def cancel_list_input(message: Message):
        pending.pop(str(message.from_user.id), None)
        token, view = new(message.from_user.id)
        await home(message, token, view, get_user_locale(message.from_user.id))

    @dp.message(Command("lists"))
    async def open_lists(message: Message):
        if not message.from_user:
            return
        owner = message.from_user.id
        locale = get_user_locale(owner)
        if message.chat.type != "private":
            await message.answer(t(locale, "private"))
            return
        pending.pop(str(owner), None)
        token, view = new(owner)
        await home(message, token, view, locale)

    @dp.callback_query(F.data == "wlh")
    async def list_home_callback(callback: CallbackQuery):
        uid = callback.from_user.id
        locale = get_user_locale(uid)
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer(t(locale, "private"), show_alert=True)
            return
        await callback.answer()
        token, view = new(uid)
        await home(callback.message, token, view, locale)

    @dp.callback_query(F.data.startswith("wlcard:"))
    async def card_add(callback: CallbackQuery):
        locale = get_user_locale(callback.from_user.id)
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer(t(locale, "private"), show_alert=True)
            return
        card_token = callback.data.split(":", 1)[1]
        session = card_state.anime_sessions.get(card_token)
        if not session or session.owner_user_id != callback.from_user.id:
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        await callback.answer()
        token, view = new(
            callback.from_user.id,
            entry=await asyncio.to_thread(
                entry_for, session.anime, get_app_context().identity_resolver
            ),
        )
        # Keep card unchanged and expose private list names only in a new private message.
        await choose_list(callback.message, token, view, locale)

    @dp.callback_query(F.data.startswith("wl:"))
    async def list_action(callback: CallbackQuery):
        uid = callback.from_user.id
        locale = get_user_locale(uid)
        parts = callback.data.split(":")
        view = views.get(parts[1]) if len(parts) >= 4 else None
        if (
            view is None
            or view.owner != uid
            or not callback.message
            or callback.message.chat.type != "private"
        ):
            await callback.answer(
                text("card", "expired" if view is None else "not_owner", locale=locale),
                show_alert=True,
            )
            return
        token, revision, action = parts[1:4]
        if revision != str(view.revision):
            await callback.answer(
                text("card", "expired", locale=locale), show_alert=True
            )
            return
        if action == "stay":
            await callback.answer()
            return
        if action not in {"open", "card"}:
            view.revision += 1
        message = callback.message
        try:
            idx = int(parts[4]) if len(parts) == 5 else -1
            if action in {"list", "entry", "save", "result"} and not 0 <= idx < len(
                view.rows
            ):
                raise ValueError("Index")
            await callback.answer()
            pending.pop(str(uid), None)
            if action == "home":
                await home(message, token, view, locale, edit=True)
            elif action == "list":
                view.list_id = view.rows[idx][0]
                view.page = 0
                await list_page(message, token, view, locale, edit=True)
            elif action in {"prev", "next", "page"}:
                view.page += -1 if action == "prev" else 1 if action == "next" else 0
                await list_page(message, token, view, locale, edit=True)
            elif action == "stay":
                return
            elif action == "entry":
                view.entry = view.rows[idx]
                await detail(message, token, view, locale)
            elif action == "open":
                await aggregate_and_send(message, view.entry.candidate, uid, card_state)
            elif action == "remove":
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(store.remove, uid, view.list_id, view.entry.key)
                await list_page(message, token, view, locale, edit=True)
            elif action == "choose":
                await choose_list(message, token, view, locale, edit=True)
            elif action == "save":
                target = view.rows[idx][0]
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.add_many,
                    uid,
                    [(target, view.entry)],
                    settings().watchlist_max_entries,
                )
                await render(
                    message,
                    t(locale, "saved"),
                    [
                        [
                            button(token, "home", t(locale, "title")),
                            button(token, "card", t(locale, "back_card")),
                        ]
                    ],
                    edit=True,
                )
            elif action == "card":
                await aggregate_and_send(message, view.entry.candidate, uid, card_state)
            elif action == "result":
                entry = entry_for(view.rows[idx], get_app_context().identity_resolver)
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.add_many,
                    uid,
                    [(view.list_id, entry)],
                    settings().watchlist_max_entries,
                )
                view.page = 0
                await list_page(message, token, view, locale, edit=True)
            elif action == "delete_confirm":
                await render(
                    message,
                    t(locale, "confirm_delete"),
                    [
                        [
                            button(token, "delete", t(locale, "delete_list")),
                            button(token, "page", t(locale, "back")),
                        ]
                    ],
                    edit=True,
                )
            elif action == "delete":
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(store.delete, uid, view.list_id)
                await home(message, token, view, locale, edit=True)
            elif action == "import":
                await render(
                    message,
                    t(locale, "import_help"),
                    [
                        [button(token, "import_" + source, source)]
                        for source in ("AniList", "Shikimori", "Kitsu", "MAL XML")
                    ]
                    + [[button(token, "home", t(locale, "back"))]],
                    edit=True,
                )
            elif action.startswith("import_"):
                source = action.removeprefix("import_")
                if source not in ("AniList", "Shikimori", "Kitsu", "MAL XML"):
                    return
                pending[str(uid)] = (token, "import_" + source)
                await render(
                    message,
                    t(
                        locale,
                        "xml_prompt"
                        if source == "MAL XML"
                        else "username_prompt"
                        if source == "AniList"
                        else "id_prompt",
                    ),
                    [[button(token, "home", t(locale, "back"))]],
                    edit=True,
                )
            elif action in {"create", "add", "progress"}:
                pending[str(uid)] = (token, action)
                await render(
                    message,
                    t(
                        locale,
                        {
                            "create": "name_prompt",
                            "add": "search_prompt",
                            "progress": "progress_prompt",
                        }[action],
                    ),
                    [
                        [
                            button(
                                token,
                                "home" if action == "create" else "page",
                                t(locale, "back"),
                            )
                        ]
                    ],
                    edit=True,
                )
        except Exception:
            logging.debug("Watchlist callback failed", exc_info=True)
            await message.answer(t(locale, "failed"))

    @dp.message(
        lambda m: bool(
            m.from_user
            and m.chat.type == "private"
            and pending.get(str(m.from_user.id))
            and (m.document or (m.text and not m.text.startswith("/")))
        )
    )
    async def list_input(message: Message):
        uid = message.from_user.id
        locale = get_user_locale(uid)
        request = pending.pop(str(uid), None)
        if request is None:
            return
        token, action = request
        view = views.get(token)
        if view is not None:
            view.revision += 1
        if view is None or view.owner != uid:
            await message.answer(text("card", "expired", locale=locale))
            return
        if uid in busy:
            await message.answer(t(locale, "busy"))
            return
        busy.add(uid)
        try:
            value = (message.text or "").strip()
            if action == "create":
                store = await asyncio.to_thread(watchlists)
                view.list_id = await asyncio.to_thread(
                    store.create, uid, value, settings().watchlist_max_custom
                )
                view.page = 0
                await list_page(message, token, view, locale)
            elif action == "progress":
                progress = int(value)
                if not 0 <= progress <= 100000:
                    raise ValueError("Progress")
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.set_progress, uid, view.list_id, view.entry.key, progress
                )
                await list_page(message, token, view, locale)
            elif action == "add":
                if not value or len(value) > 140:
                    raise ValueError("Query")
                context = get_app_context()
                from bots.telegram.search.handlers import _parse_direct_query

                direct = _parse_direct_query(value, context.direct_lookup.source_keys)
                if direct:
                    source, external_id = direct
                    result = await context.direct_lookup.lookup(
                        external_id, source_keys=(source,) if source else None
                    )
                else:
                    result = await context.search_service.collect(
                        value, page_size=10, max_pages=1
                    )
                view.rows = result.items[:30]
                rows = [
                    [button(token, "result", c.title, i)]
                    for i, c in enumerate(view.rows)
                ]
                rows.append([button(token, "page", t(locale, "back"))])
                await render(
                    message,
                    t(locale, "choose_title") if view.rows else t(locale, "empty"),
                    rows,
                )
            elif action.startswith("import_"):
                source = action.removeprefix("import_")
                await message.answer(t(locale, "importing"))
                limit = settings().watchlist_import_limit
                if source == "MAL XML":
                    doc = message.document
                    if (
                        not doc
                        or not doc.file_name
                        or not doc.file_name.lower().endswith((".xml", ".xml.gz"))
                        or not doc.file_size
                        or doc.file_size > 2 * 1024 * 1024
                    ):
                        raise ValueError("Expected bounded XML")
                    buffer = io.BytesIO()
                    await message.bot.download(doc, destination=buffer, timeout=20)
                    items = parse_mal_xml(buffer.getvalue(), limit)
                else:
                    items = await asyncio.wait_for(
                        fetch_public(source.lower(), value, limit), 90
                    )
                await apply_import(message, uid, locale, items)
        except Exception:
            logging.debug("Watchlist input failed", exc_info=True)
            await message.answer(t(locale, "failed"))
        finally:
            busy.discard(uid)
