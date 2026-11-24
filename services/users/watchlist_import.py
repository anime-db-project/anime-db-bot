from __future__ import annotations

import json
import gzip
import io
import re
import xml.etree.ElementTree as ET

import httpx
from core.models import SearchCandidate

STATUS = {
    "current": "watching",
    "watching": "watching",
    "completed": "completed",
    "dropped": "dropped",
    "planning": "planned",
    "planned": "planned",
    "plan to watch": "planned",
    "on_hold": "paused",
    "on-hold": "paused",
    "on hold": "paused",
    "hold": "paused",
    "paused": "paused",
    "repeating": "rewatching",
    "rewatching": "rewatching",
}


def imported(source, external_id, title, status, progress=0, score=None, ids=None):
    key = STATUS.get(str(status).strip().lower())
    value = str(external_id or "")
    if not key or not value.isascii() or not value.isdigit() or int(value) <= 0:
        return None
    try:
        progress = max(0, min(100000, int(progress or 0)))
        score = float(score) if score is not None and 0 < float(score) <= 100 else None
    except (TypeError, ValueError):
        progress, score = 0, None
    return (
        key,
        SearchCandidate(
            title=str(title or (source + " #" + value))[:512],
            external_ids={source: value, **(ids or {})},
        ),
        progress,
        score,
    )


def parse_mal_xml(data: bytes, limit: int):
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Oversized export")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
    if (
        len(data) > 2 * 1024 * 1024
        or b"\x00" in data
        or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I)
    ):
        raise ValueError("Unsafe or oversized XML")
    root = ET.fromstring(data)
    if root.tag != "myanimelist":
        raise ValueError("Not a MAL export")
    result = []
    for node in root.findall("anime"):
        item = imported(
            "mal",
            node.findtext("series_animedb_id"),
            node.findtext("series_title"),
            node.findtext("my_status"),
            node.findtext("my_watched_episodes"),
            node.findtext("my_score"),
        )
        if item:
            result.append(item)
        if len(result) > limit:
            raise ValueError("Import limit exceeded")
    return result


async def _json(client, method, url, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("Response too large")
        return json.loads(content)


async def fetch_public(source, username, limit):
    username = username.strip()
    if not username or len(username) > 80:
        raise ValueError("Invalid account")
    result = []
    async with httpx.AsyncClient(
        timeout=10,
        follow_redirects=False,
        headers={"User-Agent": "anime-db-bot/1.0", "Accept": "application/json"},
    ) as client:
        if source == "anilist":
            query = """query($user:String!,$chunk:Int!){MediaListCollection(userName:$user,type:ANIME,chunk:$chunk,perChunk:500){hasNextChunk lists{entries{mediaId status progress score(format:POINT_10) media{idMal title{romaji english}}}}}}"""
            for page in range(1, min(100, limit // 500 + 3)):
                data = await _json(
                    client,
                    "POST",
                    "https://graphql.anilist.co",
                    json={
                        "query": query,
                        "variables": {"user": username, "chunk": page},
                    },
                )
                if data.get("errors"):
                    raise ValueError("AniList account unavailable or private")
                collection = data["data"]["MediaListCollection"]
                for group in collection["lists"]:
                    for e in group["entries"]:
                        media = e.get("media") or {}
                        titles = media.get("title") or {}
                        item = imported(
                            "anilist",
                            e["mediaId"],
                            titles.get("romaji") or titles.get("english"),
                            e.get("status"),
                            e.get("progress"),
                            e.get("score"),
                            {"mal": str(media["idMal"])}
                            if media.get("idMal")
                            else None,
                        )
                        if item:
                            result.append(item)
                if len({x[1].external_ids["anilist"] for x in result}) > limit:
                    raise ValueError("Import limit exceeded")
                if not collection.get("hasNextChunk"):
                    break
            else:
                raise ValueError("Import pagination limit exceeded")
        elif source == "shikimori":
            if not username.isascii() or not username.isdigit():
                raise ValueError("Numeric Shikimori user ID required")
            size = min(200, limit + 1)
            seen = set()
            for page in range(1, limit // size + 3):
                data = await _json(
                    client,
                    "GET",
                    f"https://shikimori.one/api/users/{username}/anime_rates",
                    params={"limit": size, "page": page},
                )
                if not isinstance(data, list):
                    raise ValueError("Unexpected Shikimori response")
                new = 0
                for e in data:
                    anime = e.get("anime") or {}
                    item = imported(
                        "shikimori",
                        anime.get("id"),
                        anime.get("russian") or anime.get("name"),
                        e.get("status"),
                        e.get("episodes"),
                        e.get("score"),
                    )
                    if item and item[1].identity.fingerprint() not in seen:
                        seen.add(item[1].identity.fingerprint())
                        result.append(item)
                        new += 1
                if len(result) > limit:
                    raise ValueError("Import limit exceeded")
                if len(data) < size:
                    break
                if not new:
                    raise ValueError("Repeated import page")
            else:
                raise ValueError("Import pagination limit exceeded")
        elif source == "kitsu":
            if not username.isascii() or not username.isdigit():
                raise ValueError("Numeric Kitsu user ID required")
            size = 100
            for offset in range(0, limit + size, size):
                data = await _json(
                    client,
                    "GET",
                    "https://kitsu.io/api/edge/library-entries",
                    params={
                        "filter[user_id]": username,
                        "filter[kind]": "anime",
                        "include": "anime",
                        "page[limit]": size,
                        "page[offset]": offset,
                    },
                )
                titles = {
                    str(x["id"]): x.get("attributes", {}).get("canonicalTitle")
                    for x in data.get("included", [])
                    if x.get("type") == "anime"
                }
                for e in data["data"]:
                    attrs = e.get("attributes", {})
                    media = (
                        e.get("relationships", {}).get("anime", {}).get("data") or {}
                    )
                    mid = str(media.get("id") or "")
                    item = imported(
                        "kitsu",
                        mid,
                        titles.get(mid),
                        attrs.get("status"),
                        attrs.get("progress"),
                        float(attrs["ratingTwenty"]) / 2
                        if attrs.get("ratingTwenty")
                        else None,
                    )
                    if item:
                        result.append(item)
                if len(result) > limit:
                    raise ValueError("Import limit exceeded")
                if not data.get("links", {}).get("next"):
                    break
            else:
                raise ValueError("Import pagination limit exceeded")
        else:
            raise ValueError("Unsupported import source")
    unique = {}
    for entry in result:
        unique.setdefault(entry[1].identity.fingerprint(), entry)
    if len(unique) > limit:
        raise ValueError("Import limit exceeded")
    return list(unique.values())
