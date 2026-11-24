from __future__ import annotations

import asyncio
import io
import html
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
    InputRichMessage,
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
    menu_page: int = 0
    query: str = ""
    sort: str = "title"
    mode: str = "home"


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

    async def render(message, value, rows, *, edit=False, rich=None):
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        if rich is not None:
            try:
                content = InputRichMessage(html=rich)
                if edit:
                    await message.edit_text(rich_message=content, reply_markup=keyboard)
                else:
                    await message.answer_rich(content, reply_markup=keyboard)
                return
            except Exception:
                logging.debug("Watchlist rich render failed", exc_info=True)
        send = message.edit_text if edit else message.answer
        await send(value, reply_markup=keyboard, parse_mode=None)

    def table(heading, headers, rows, note=""):
        def cell(value):
            return html.escape(str(value))

        result = "<h2>" + cell(heading) + "</h2>"
        if note:
            result += "<p>" + cell(note) + "</p>"
        if rows:
            result += "<p><br></p><table bordered striped compact>"
            if headers:
                result += (
                    "<tr>"
                    + "".join("<th>" + cell(v) + "</th>" for v in headers)
                    + "</tr>"
                )
            result += (
                "".join(
                    "<tr>" + "".join("<td>" + cell(v) + "</td>" for v in row) + "</tr>"
                    for row in rows
                )
                + "</table>"
            )
        return result

    async def lists(owner):
        store = await asyncio.to_thread(watchlists)
        return await asyncio.to_thread(
            store.lists, owner, settings().watchlist_custom_defaults
        )

    def page_nav(token, page, count, size, prev="prev", next="next"):
        rows = []
        if page:
            rows.append(button(token, prev, "◀️"))
        rows.append(
            button(token, "stay", f"{page + 1}/{max(1, (count + size - 1) // size)}")
        )
        if (page + 1) * size < count:
            rows.append(button(token, next, "▶️"))
        return rows

    async def home(message, token, view, locale, *, edit=False):
        view.mode = "home"
        view.list_id = None
        view.entry = None
        all_lists = await lists(view.owner)
        view.menu_page = max(0, min(view.menu_page, (len(all_lists) - 1) // 12))
        view.rows = all_lists[view.menu_page * 12 : (view.menu_page + 1) * 12]
        data = [(label(locale, k, n), count) for k, n, count in view.rows]
        rows = [
            [button(token, "list", f"{name} ({count})", i)]
            for i, (name, count) in enumerate(data)
        ]
        if len(all_lists) > 12:
            rows.append(
                page_nav(
                    token, view.menu_page, len(all_lists), 12, "menu_prev", "menu_next"
                )
            )
        rows.append(
            [
                button(token, "create", t(locale, "create")),
                button(token, "import", t(locale, "import")),
            ]
        )
        heading = t(locale, "title")
        note = t(locale, "shared_help")
        await render(
            message,
            heading
            + "\n\n"
            + note
            + "\n\n"
            + "\n".join(f"{name}: {count}" for name, count in data),
            rows,
            edit=edit,
            rich=table(
                heading, [t(locale, "list_name"), t(locale, "count")], data, note
            ),
        )

    async def choose_list(message, token, view, locale, *, edit=False):
        view.mode = "choose"
        all_lists = await lists(view.owner)
        store = await asyncio.to_thread(watchlists)
        view.entry, selected = await asyncio.to_thread(
            store.memberships, view.owner, view.entry
        )
        view.menu_page = max(0, min(view.menu_page, (len(all_lists) - 1) // 12))
        view.rows = all_lists[view.menu_page * 12 : (view.menu_page + 1) * 12]
        rows = []
        data = []
        for i, (k, n, _) in enumerate(view.rows):
            name = label(locale, k, n)
            marked = k in selected
            rows.append(
                [
                    button(
                        token,
                        "unselect" if marked else "save",
                        ("✓ " if marked else "＋ ") + name,
                        i,
                    )
                ]
            )
            data.append((name, "✓" if marked else "—"))
        if len(all_lists) > 12:
            rows.append(
                page_nav(
                    token, view.menu_page, len(all_lists), 12, "menu_prev", "menu_next"
                )
            )
        rows.append(
            [
                button(token, "progress", t(locale, "set_progress")),
                button(token, "score", t(locale, "set_score")),
            ]
            if selected
            else [button(token, "create_for", t(locale, "create"))]
        )
        if selected:
            rows.append([button(token, "create_for", t(locale, "create"))])
        rows.append([button(token, "detail", t(locale, "done"))])
        rows.append([button(token, "home", t(locale, "title"))])
        heading = view.entry.candidate.title
        note = t(locale, "membership_help")
        note += "\n" + t(locale, "progress", count=view.entry.progress)
        if view.entry.score is not None:
            note += " · " + t(locale, "score", score=view.entry.score)
        await render(
            message,
            heading
            + "\n\n"
            + note
            + "\n\n"
            + "\n".join(f"{name}: {state}" for name, state in data),
            rows,
            edit=edit,
            rich=table(
                heading, [t(locale, "list_name"), t(locale, "membership")], data, note
            ),
        )

    async def list_page(message, token, view, locale, *, edit=False):
        view.mode = "list"
        meta = next((x for x in await lists(view.owner) if x[0] == view.list_id), None)
        if meta is None:
            await home(message, token, view, locale, edit=edit)
            return
        store = await asyncio.to_thread(watchlists)

        async def load():
            return await asyncio.to_thread(
                store.entries,
                view.owner,
                view.list_id,
                view.page,
                10,
                query=view.query,
                sort=view.sort,
            )

        view.rows, count = await load()
        bounded = max(0, min(view.page, max(0, (count - 1) // 10)))
        if bounded != view.page:
            view.page = bounded
            view.rows, count = await load()
        heading = f"{label(locale, meta[0], meta[1])} ({count})"
        data = []
        rows = []
        for i, e in enumerate(view.rows):
            name = e.candidate.title[:180] + (
                f" ({e.candidate.year})" if e.candidate.year else ""
            )
            number = view.page * 10 + i + 1
            data.append(
                (
                    f"{number}. {name}",
                    str(e.progress),
                    f"{e.score:g}" if e.score is not None else "—",
                )
            )
            rows.append([button(token, "entry", f"{number}. {name}", i)])
        rows.append(page_nav(token, view.page, count, 10))
        rows.append(
            [
                button(token, "add", t(locale, "add")),
                button(token, "filter", t(locale, "filter")),
            ]
        )
        rows.append([button(token, "sort", t(locale, "sort_" + view.sort))])
        if view.query:
            rows.append([button(token, "clear_filter", t(locale, "clear_filter"))])
        if view.list_id.startswith("u_"):
            rows.append(
                [
                    button(token, "rename", t(locale, "rename")),
                    button(token, "delete_confirm", t(locale, "delete_list")),
                ]
            )
        rows.append([button(token, "home", t(locale, "back"))])
        note = (
            t(locale, "filter_value", query=view.query) + " · " if view.query else ""
        ) + t(locale, "sort_" + view.sort)
        if not count:
            note += " · " + t(locale, "empty")
        plain = (
            heading
            + "\n\n"
            + note
            + "\n\n"
            + "\n".join(
                f"{name} · {progress} · {score}" for name, progress, score in data
            )
        )
        await render(
            message,
            plain,
            rows,
            edit=edit,
            rich=table(
                heading,
                [t(locale, "anime"), t(locale, "episodes"), t(locale, "rating")],
                data,
                note,
            ),
        )

    async def detail(message, token, view, locale, *, edit=True):
        view.mode = "detail"
        store = await asyncio.to_thread(watchlists)
        e, selected = await asyncio.to_thread(store.memberships, view.owner, view.entry)
        view.entry = e
        all_lists = await lists(view.owner)
        memberships = (
            ", ".join(label(locale, k, n) for k, n, _ in all_lists if k in selected)
            or "—"
        )
        data = [
            (t(locale, "episodes"), str(e.progress)),
            (t(locale, "rating"), f"{e.score:g}/10" if e.score is not None else "—"),
            (t(locale, "title"), memberships),
        ]
        rows = [
            [button(token, "open", t(locale, "open"))],
            [
                button(token, "minus", "−1"),
                button(token, "progress", t(locale, "set_progress")),
                button(token, "plus", "+1"),
            ],
            [
                button(token, "score", t(locale, "set_score")),
                button(token, "choose", t(locale, "manage")),
            ],
            [button(token, "remove", t(locale, "remove"))]
            if view.list_id and view.list_id in selected
            else [],
            [button(token, "page" if view.list_id else "home", t(locale, "back"))],
        ]
        rows = [row for row in rows if row]
        await render(
            message,
            e.candidate.title
            + "\n\n"
            + "\n".join(f"{key}: {value}" for key, value in data),
            rows,
            edit=edit,
            rich=table(e.candidate.title, [], data),
        )

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
            if action in {
                "list",
                "entry",
                "save",
                "unselect",
                "result",
            } and not 0 <= idx < len(view.rows):
                raise ValueError("Index")
            await callback.answer()
            pending.pop(str(uid), None)
            if action == "home":
                await home(message, token, view, locale, edit=True)
            elif action == "list":
                view.list_id = view.rows[idx][0]
                view.page = 0
                view.query = ""
                view.sort = "title"
                await list_page(message, token, view, locale, edit=True)
            elif action in {"menu_prev", "menu_next"}:
                view.menu_page += -1 if action == "menu_prev" else 1
                if view.mode == "choose":
                    await choose_list(message, token, view, locale, edit=True)
                else:
                    await home(message, token, view, locale, edit=True)
            elif action == "detail":
                await detail(message, token, view, locale)
            elif action == "sort":
                order = ("title", "progress", "score")
                view.sort = order[(order.index(view.sort) + 1) % len(order)]
                view.page = 0
                await list_page(message, token, view, locale, edit=True)
            elif action == "clear_filter":
                view.query = ""
                view.page = 0
                await list_page(message, token, view, locale, edit=True)
            elif action in {"plus", "minus"}:
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.adjust_progress,
                    uid,
                    view.entry.key,
                    1 if action == "plus" else -1,
                )
                await detail(message, token, view, locale)
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
                view.menu_page = 0
                await choose_list(message, token, view, locale, edit=True)
            elif action in {"save", "unselect"}:
                target = view.rows[idx][0]
                store = await asyncio.to_thread(watchlists)
                if action == "save":
                    await asyncio.to_thread(
                        store.add_many,
                        uid,
                        [(target, view.entry)],
                        settings().watchlist_max_entries,
                    )
                else:
                    await asyncio.to_thread(store.remove, uid, target, view.entry.key)
                await choose_list(message, token, view, locale, edit=True)
            elif action == "card":
                await aggregate_and_send(message, view.entry.candidate, uid, card_state)
            elif action == "result":
                entry = await asyncio.to_thread(
                    entry_for, view.rows[idx], get_app_context().identity_resolver
                )
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.add_many,
                    uid,
                    [(view.list_id, entry)],
                    settings().watchlist_max_entries,
                )
                view.entry = entry
                view.menu_page = 0
                await choose_list(message, token, view, locale, edit=True)
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
            elif action in {
                "create",
                "create_for",
                "add",
                "progress",
                "score",
                "rename",
                "filter",
            }:
                pending[str(uid)] = (token, action)
                await render(
                    message,
                    t(
                        locale,
                        {
                            "create": "name_prompt",
                            "create_for": "name_prompt",
                            "score": "score_prompt",
                            "rename": "name_prompt",
                            "filter": "filter_prompt",
                            "add": "search_prompt",
                            "progress": "progress_prompt",
                        }[action],
                    ),
                    [
                        [
                            button(
                                token,
                                "home"
                                if action == "create"
                                else "choose"
                                if view.mode == "choose"
                                else "detail"
                                if view.mode == "detail"
                                else "page",
                                t(locale, "back"),
                            )
                        ]
                    ],
                    edit=True,
                )
        except Exception:
            logging.debug("Watchlist callback failed", exc_info=True)
            await render(
                message,
                t(locale, "failed"),
                [
                    [
                        button(
                            token,
                            "choose"
                            if view.mode == "choose"
                            else "detail"
                            if view.mode == "detail"
                            else "page"
                            if view.list_id
                            else "home",
                            t(locale, "back"),
                        )
                    ]
                ],
            )

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
            elif action == "create_for":
                store = await asyncio.to_thread(watchlists)
                new_id = await asyncio.to_thread(
                    store.create, uid, value, settings().watchlist_max_custom
                )
                await asyncio.to_thread(
                    store.add_many,
                    uid,
                    [(new_id, view.entry)],
                    settings().watchlist_max_entries,
                )
                await choose_list(message, token, view, locale)
            elif action == "rename":
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(store.rename, uid, view.list_id, value)
                await list_page(message, token, view, locale)
            elif action == "filter":
                if len(value) > 140:
                    raise ValueError("Filter")
                view.query = value
                view.page = 0
                await list_page(message, token, view, locale)
            elif action == "score":
                score = None if value in {"-", "—"} else float(value.replace(",", "."))
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(store.set_score, uid, view.entry.key, score)
                if view.mode == "choose":
                    await choose_list(message, token, view, locale)
                else:
                    await detail(message, token, view, locale, edit=False)
            elif action == "progress":
                progress = int(value)
                if not 0 <= progress <= 100000:
                    raise ValueError("Progress")
                store = await asyncio.to_thread(watchlists)
                await asyncio.to_thread(
                    store.set_progress, uid, view.list_id, view.entry.key, progress
                )
                if view.mode == "choose":
                    await choose_list(message, token, view, locale)
                else:
                    await detail(message, token, view, locale, edit=False)
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
                    [
                        button(
                            token,
                            "result",
                            f"{i + 1}. {c.title}" + (f" ({c.year})" if c.year else ""),
                            i,
                        )
                    ]
                    for i, c in enumerate(view.rows)
                ]
                rows.append([button(token, "page", t(locale, "back"))])
                await render(
                    message,
                    (
                        t(locale, "choose_title")
                        + "\n\n"
                        + "\n".join(
                            f"{i + 1}. {c.title[:80]}"
                            + (f" ({c.year})" if c.year else "")
                            for i, c in enumerate(view.rows)
                        )
                    )
                    if view.rows
                    else t(locale, "empty"),
                    rows,
                    rich=table(
                        t(locale, "choose_title"),
                        [t(locale, "anime"), "ID"],
                        [
                            (
                                c.title[:180] + (f" ({c.year})" if c.year else ""),
                                ", ".join(
                                    f"{k.upper()}: {v}"
                                    for k, v in list(c.external_ids.items())[:3]
                                ),
                            )
                            for c in view.rows
                        ],
                    ),
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
            pending[str(uid)] = (token, action)
            await render(
                message,
                t(locale, "failed"),
                [
                    [
                        button(
                            token,
                            "choose"
                            if view.mode == "choose"
                            else "detail"
                            if view.mode == "detail"
                            else "page"
                            if view.list_id
                            else "home",
                            t(locale, "back"),
                        )
                    ]
                ],
            )
        finally:
            busy.discard(uid)
