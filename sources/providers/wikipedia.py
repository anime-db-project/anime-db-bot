"""Standalone Wikipedia article reader; never participates in anime aggregation.

Only fixed-language Wikimedia API endpoints are contacted. Article matching is
an explicit user choice from title-search results, not an assumed identity match.
Parsed HTML keeps useful inline links; clean mode removes navigation/editing
noise, while raw mode retains visible editorial and service blocks.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape, unescape
from html.parser import HTMLParser
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import httpx

LANGUAGES = ("ru", "en", "ja", "de", "es", "fr", "it")
USER_AGENT = (
    "anime-db-bot/0.3 (https://github.com/anime-db-project/anime-db-bot; "
    "Wikipedia read-only integration)"
)
NOISE_CLASSES = (
    "mw-editsection", "navbox", "infobox", "metadata", "reflist",
    "reference", "references", "hatnote", "ambox", "ombox", "tmbox",
    "noprint", "printfooter", "catlinks", "mw-collapsible", "thumb",
    "toc", "portal", "sidebar", "sistersitebox", "mw-empty-elt",
    "authority-control",
)
NOISE_SECTIONS = frozenset({
    "примечания", "сноски", "литература", "внешние ссылки",
    "источники", "references", "notes", "external links",
    "further reading", "bibliography", "出典", "脚注", "外部リンク",
    "参考文献", "注釈",
})
VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "wbr", "source"}
SKIP_TAGS = {"script", "style", "noscript", "iframe", "object", "svg", "form"}
MAX_HTML_BYTES = 1_800_000
MAX_TEXT_CHUNK = 720
PAGE_HTML_LIMIT = 30000
JAPANESE_KANA = re.compile(r"[\u3040-\u30ff\u3400-\u9fff々]")
JAPANESE_NAME_PART = re.compile(r"[\u3040-\u30ff\u3400-\u9fff々ー]{3,}")


@dataclass(frozen=True, slots=True)
class WikipediaHit:
    pageid: int
    title: str
    snippet: str
    language: str


@dataclass(frozen=True, slots=True)
class WikipediaSection:
    title: str
    paragraphs: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class WikipediaArticle:
    pageid: int
    title: str
    language: str
    url: str
    sections: tuple[WikipediaSection, ...]
    revision_id: int | None = None
    japanese_phrases: tuple[str, ...] = ()


def wiki_language(locale: str) -> str:
    return locale if locale in LANGUAGES else "en"


def wikipedia_api_url(language: str) -> str:
    if language not in LANGUAGES:
        raise ValueError("Unsupported Wikipedia language")
    return f"https://{language}.wikipedia.org/w/api.php"


def wikipedia_page_url(language: str, title: str) -> str:
    from urllib.parse import quote

    wikipedia_api_url(language)
    return f"https://{language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"


def _plain_snippet(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", "", value))).strip()[:240]


def _safe_link(href: str, *, base: str) -> str | None:
    value = str(href).strip()
    if not value or len(value) > 600:
        return None
    url = urljoin(base, value)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return url


@dataclass(slots=True)
class _MutableSection:
    title: str
    paragraphs: list[tuple[str, ...]] = field(default_factory=list)


class _WikipediaHTMLParser(HTMLParser):
    """Convert article prose into balanced, safe Telegram HTML fragments.

    Paragraphs stay paragraphs: inline markup does not create extra lines.
    H2 headings divide major chapters; all nested H3-H6 headings belong to
    their parent chapter. RAW retains visible wiki chrome, not executable HTML.
    """

    def __init__(self, *, url: str, raw: bool, intro_title: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.raw = raw
        self.sections = [_MutableSection(intro_title)]
        self._current = self.sections[0]
        self._paragraph: list[str] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._heading_text: list[str] = []
        self._link: str | None = None
        self._link_stack: list[str | None] = []
        self._bold = 0
        self._italic = 0

    def _flush(self) -> None:
        if self._paragraph:
            combined = "".join(self._paragraph)
            if combined.strip():
                self._current.paragraphs.append(tuple(self._paragraph))
            self._paragraph.clear()

    def _append(self, string: str) -> None:
        # Preserve spaces around links and emphasized spans instead of making
        # an artificial line break for each HTML text node.
        normalized = re.sub(r"\s+", " ", string)
        if not normalized:
            return
        if normalized.isspace() and not self._paragraph:
            return
        # Each fragment is independently balanced, so splitting at the
        # Telegram page budget never leaves an open <a>/<b>/<i> tag.
        while normalized:
            if len(normalized) <= MAX_TEXT_CHUNK:
                piece, normalized = normalized, ""
            else:
                split = normalized.rfind(" ", 1, MAX_TEXT_CHUNK)
                split = split if split > MAX_TEXT_CHUNK // 2 else MAX_TEXT_CHUNK
                piece, normalized = normalized[:split], normalized[split:]
            fragment = escape(piece)
            if self._link:
                fragment = (
                    f'<a href="{escape(self._link, quote=True)}">{fragment}</a>'
                )
            if self._italic:
                fragment = f"<i>{fragment}</i>"
            if self._bold:
                fragment = f"<b>{fragment}</b>"
            self._paragraph.append(fragment)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attr = dict(attrs)
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        cls = (attr.get("class") or "").casefold()
        node_id = (attr.get("id") or "").casefold()
        skipped = tag in SKIP_TAGS or (
            not self.raw
            and (
                any(name in cls.split() for name in NOISE_CLASSES)
                or node_id in {"toc", "catlinks", "mw-normal-catlinks"}
                or tag == "sup"
            )
        )
        if skipped:
            if tag not in VOID_TAGS:
                self._skip_depth = 1
            return
        if tag in {"h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading_level = int(tag[1])
            self._heading_text = []
            return
        if tag in {"p", "li", "tr", "blockquote"}:
            self._flush()
            if tag == "li":
                self._paragraph.append("• ")
        elif tag == "br":
            self._paragraph.append("\n")
        elif tag in {"td", "th"} and self._paragraph:
            self._paragraph.append(" | ")
        if tag == "a":
            self._link_stack.append(self._link)
            self._link = _safe_link(attr.get("href") or "", base=self.url)
        elif tag in {"b", "strong"}:
            self._bold += 1
        elif tag in {"i", "em"}:
            self._italic += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if self._heading_level and tag == f"h{self._heading_level}":
            title = " ".join("".join(self._heading_text).split())
            level = self._heading_level
            self._heading_level = 0
            self._heading_text = []
            if title:
                if level == 2:
                    self._current = _MutableSection(title)
                    self.sections.append(self._current)
                else:
                    # A child heading is part of the chapter, not a chapter
                    # on its own. Bold subheadings preserve the reading flow.
                    markup = "b" if level < 5 else "i"
                    self._current.paragraphs.append(
                        (f"<{markup}>{escape(title)}</{markup}>",)
                    )
            return
        if tag in {"p", "li", "tr", "blockquote"}:
            self._flush()
        if tag == "a" and self._link_stack:
            self._link = self._link_stack.pop()
        elif tag in {"b", "strong"}:
            self._bold = max(0, self._bold - 1)
        elif tag in {"i", "em"}:
            self._italic = max(0, self._italic - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading_level:
            self._heading_text.append(data)
        elif data:
            self._append(data)

    def finish(self) -> tuple[WikipediaSection, ...]:
        self._flush()
        sections: list[WikipediaSection] = []
        for item in self.sections:
            if not item.paragraphs:
                continue
            if not self.raw and item.title.casefold().strip() in NOISE_SECTIONS:
                continue
            sections.append(WikipediaSection(item.title[:200], tuple(item.paragraphs)))
        return tuple(sections)


class _JapaneseTextCollector(HTMLParser):
    """Collect Japanese-bearing *page text*, never wiki HTML attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.phrases: list[str] = []

    def handle_data(self, data: str) -> None:
        if JAPANESE_KANA.search(data) and len(self.phrases) < 80:
            self.phrases.append(" ".join(data.split())[:240])


def japanese_title_in_text(
    phrases: tuple[str, ...] | list[str], original_title: str | None
) -> str | None:
    """Only use a Japanese alias if the page text contains its actual writing.

    A mixed romanized/Japanese name (e.g. "NARUTO -ナルト-") may be written
    in Wikipedia using only its kana portion. Matching a distinctive, 3+
    character Japanese run is permitted; arbitrary unrelated CJK snippets
    are never promoted to an anime's Japanese title.
    """
    value = str(original_title or "").strip()
    if not value or not JAPANESE_KANA.search(value):
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    runs = JAPANESE_NAME_PART.findall(normalized)
    for phrase in phrases:
        cleaned = unicodedata.normalize("NFKC", phrase).casefold()
        if normalized in cleaned or any(run in cleaned for run in runs):
            return value
    return None


def japanese_title_in_article(
    article: WikipediaArticle, original_title: str | None
) -> str | None:
    return japanese_title_in_text(article.japanese_phrases, original_title)


def parse_wikipedia_html(
    value: str, *, url: str, mode: str, intro_title: str = "Introduction"
) -> tuple[WikipediaSection, ...]:
    if mode not in {"raw", "clean"}:
        raise ValueError("Invalid Wikipedia display mode")
    if len(value.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("Wikipedia article exceeds the configured response limit")
    parser = _WikipediaHTMLParser(url=url, raw=mode == "raw", intro_title=intro_title)
    parser.feed(value)
    parser.close()
    return parser.finish()


def paginate_section(
    section: WikipediaSection, *, limit: int = PAGE_HTML_LIMIT
) -> tuple[str, ...]:
    """Use one page until the actual Telegram message budget is exceeded.

    Preserve blank lines between Wikipedia paragraphs. Split oversized
    paragraphs only at balanced, self-contained HTML fragment boundaries.
    """
    if limit < 200:
        raise ValueError("Invalid page budget")
    pages: list[str] = []
    current = ""
    for paragraph in section.paragraphs:
        fragments = tuple(paragraph)
        full_paragraph = "".join(fragments).strip()
        if not full_paragraph:
            continue
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(full_paragraph) <= limit:
            current += separator + full_paragraph
            continue
        if current:
            pages.append(current)
            current = ""
        if len(full_paragraph) <= limit:
            current = full_paragraph
            continue
        for fragment in fragments:
            if len(fragment) > limit:
                raise ValueError("Wikipedia fragment exceeds page budget")
            if len(current) + len(fragment) > limit and current:
                pages.append(current.strip())
                current = ""
            current += fragment
    if current.strip():
        pages.append(current.strip())
    return tuple(pages or ("",))


class WikipediaClient:
    """Read-only MediaWiki adapter with bounded response size and TTL/LRU cache."""

    def __init__(self, *, cache_size: int = 8, cache_ttl: float = 1800.0) -> None:
        self._cache_size = max(1, cache_size)
        self._cache_ttl = max(30.0, cache_ttl)
        self._cache: OrderedDict[tuple[str, int, str], tuple[float, WikipediaArticle]] = OrderedDict()

    async def _query(self, language: str, params: dict[str, str | int]) -> dict:
        url = wikipedia_api_url(language)
        timeout = httpx.Timeout(connect=7.0, read=20.0, write=7.0, pool=7.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(url, params={**params, "format": "json", "formatversion": 2})
            response.raise_for_status()
            if len(response.content) > MAX_HTML_BYTES:
                raise ValueError("Wikipedia response too large")
            data = response.json()
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("Wikipedia API returned an invalid response")
        return data

    async def search(self, *, language: str, title: str) -> tuple[WikipediaHit, ...]:
        title = " ".join(str(title).split())[:140]
        if len(title) < 2:
            return ()
        data = await self._query(language, {
            "action": "query",
            "list": "search",
            "srsearch": title,
            "srnamespace": 0,
            "srlimit": 7,
            "srprop": "snippet",
        })
        results = data.get("query", {}).get("search", [])
        return tuple(
            WikipediaHit(
                pageid=int(item["pageid"]),
                title=str(item["title"])[:170],
                snippet=_plain_snippet(str(item.get("snippet", ""))),
                language=language,
            )
            for item in results
            if int(item.get("pageid", 0)) > 0 and item.get("title")
        )

    async def article(
        self, *, language: str, pageid: int, mode: str
    ) -> WikipediaArticle:
        if not 0 < int(pageid) < 2**40:
            raise ValueError("Invalid Wikipedia page ID")
        if mode not in ("raw", "clean"):
            raise ValueError("Invalid Wikipedia mode")
        key = (language, int(pageid), mode)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        data = await self._query(language, {
            "action": "parse",
            "pageid": int(pageid),
            "prop": "text|revid",
            "redirects": 1,
            "disabletoc": 1,
        })
        payload = data.get("parse")
        if not isinstance(payload, dict):
            raise ValueError("Wikipedia page was not found")
        title = str(payload.get("title") or "").strip()
        actual_id = int(payload.get("pageid") or pageid)
        html = payload.get("text")
        if not title or not isinstance(html, str):
            raise ValueError("Wikipedia article text is unavailable")
        url = wikipedia_page_url(language, title)
        japanese = _JapaneseTextCollector()
        japanese.feed(html)
        japanese.close()
        parsed = parse_wikipedia_html(
            html,
            url=url,
            mode=mode,
            intro_title="Введение" if language == "ru" else "Introduction",
        )
        article = WikipediaArticle(
            pageid=actual_id,
            title=title,
            language=language,
            url=url,
            revision_id=int(payload["revid"]) if payload.get("revid") else None,
            sections=parsed,
            japanese_phrases=tuple(japanese.phrases),
        )
        self._cache[key] = (time.monotonic() + self._cache_ttl, article)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return article


@lru_cache(maxsize=1)
def wikipedia_client() -> WikipediaClient:
    return WikipediaClient()
