from __future__ import annotations

import asyncio
import html
import logging
import time
from urllib.parse import quote

import httpx
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputRichMessage
from localization import text

ORG = "anime-db-project"
REPOSITORIES = ("anime-db-bot", "anime-mapping-db", "anime-timestamp-db", "anime-db")
BASE = "https://github.com/" + ORG
_cache: tuple[float, list[dict]] | None = None
_lock = asyncio.Lock()


async def project_repositories() -> list[dict]:
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    async with _lock:
        now = time.monotonic()
        if _cache is not None and _cache[0] > now:
            return _cache[1]
        try:
            async with httpx.AsyncClient(timeout=3.0, follow_redirects=False, headers={"Accept": "application/vnd.github+json", "User-Agent": "anime-db-bot"}) as client:
                response = await client.get("https://api.github.com/orgs/" + ORG + "/repos", params={"per_page": 100})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("Unexpected repository response")
            result = []
            for repo in payload:
                name = str(repo.get("name", ""))
                if not name or repo.get("private"):
                    continue
                license_info = repo.get("license") or {}
                result.append({"name": name, "stars": repo.get("stargazers_count"), "license": license_info.get("spdx_id"), "description": repo.get("description") or ""})
            known = {item["name"] for item in result}
            result.extend({"name": name} for name in REPOSITORIES if name not in known)
            _cache = (now + 900, result)
            return result
        except Exception:
            logging.debug("Public project metadata not available", exc_info=True)
            fallback = _cache[1] if _cache else [{"name": name} for name in REPOSITORIES]
            _cache = (now + 60, fallback)
            return fallback


async def about_content(locale: str) -> tuple[str, InputRichMessage]:
    repos = await project_repositories()
    title = text("project", "title", locale=locale)
    description = text("project", "description", locale=locale)
    unknown = text("common", "no_data", locale=locale)
    stars_label = text("project", "stars", locale=locale)
    license_label = text("project", "license", locale=locale)
    repository_label = text("project", "repository", locale=locale)
    rows = []
    lines = [title, "", description, "", BASE, ""]
    total = sum(r["stars"] for r in repos if isinstance(r.get("stars"), int))
    for repo in sorted(repos, key=lambda item: (REPOSITORIES.index(item["name"]) if item["name"] in REPOSITORIES else len(REPOSITORIES), item["name"])):
        name = str(repo["name"])
        url = BASE + "/" + quote(name, safe="")
        stars = str(repo["stars"]) if isinstance(repo.get("stars"), int) else unknown
        license_id = str(repo.get("license") or unknown)
        lines.append(f"{name}: {stars_label} {stars}; {license_label}: {license_id}\n{url}")
        rows.append(f'<tr><td align="left"><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a></td><td align="center"><a href="{html.escape(url + "/stargazers", quote=True)}">{html.escape("⭐ " + stars)}</a></td><td align="left">{html.escape(license_id)}</td></tr>')
    total_label = str(total) if all(isinstance(r.get("stars"), int) for r in repos) else unknown
    body = f"<h2>{html.escape(title)}</h2><p>{html.escape(description)}</p><p><a href=\"{BASE}\">GitHub · {ORG}</a></p><p>{html.escape(stars_label)}: {html.escape(total_label)}</p><p><br></p><table bordered striped compact><tr><th>{html.escape(repository_label)}</th><th>{html.escape(stars_label)}</th><th>{html.escape(license_label)}</th></tr>" + "".join(rows) + "</table>"
    return "\n".join(lines), InputRichMessage(html=body)


def about_keyboard(locale: str, *, back: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="GitHub", url=BASE)], [InlineKeyboardButton(text=text("settings", "back", locale=locale), callback_data="settings:back")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_about(message, locale: str, *, edit: bool = False) -> None:
    plain, rich = await about_content(locale)
    send = message.edit_text if edit else message.answer
    keyboard = about_keyboard(locale, back=edit)
    try:
        if edit:
            await message.edit_text(rich_message=rich, reply_markup=keyboard)
        else:
            await message.answer_rich(rich, reply_markup=keyboard)
    except Exception:
        await send(text=plain, reply_markup=keyboard)
