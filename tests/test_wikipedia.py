from __future__ import annotations

import html
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch
import unittest

from aiogram.types import InlineKeyboardMarkup

from bots.telegram.card.features.wikipedia import (
    _chapter_page,
    _japanese_label,
    _search_term,
    _tr,
)
from bots.telegram.card.ui.keyboards import (
    fallback_main_keyboard,
    navigation_keyboard,
)
from core.models import AggregatedAnime
from services.users.settings import SQLiteUserSettingsStore
from sources.providers.wikipedia import (
    MAX_HTML_BYTES,
    WikipediaArticle,
    WikipediaClient,
    WikipediaSection,
    WikipediaHit,
    japanese_title_in_article,
    _safe_link,
    paginate_section,
    parse_wikipedia_html,
    wikipedia_api_url,
    wikipedia_page_url,
)


EXAMPLE_WIKI_HTML = """
<div class="mw-parser-output">
  <div class="hatnote">For other uses, see Naruto (disambiguation).</div>
  <p>Anime <a href="/wiki/Naruto">Naruto</a> is a work about
     <b>ninjas</b> and <a href="https://example.org/info">other details</a>.</p>
  <div class="mw-heading mw-heading2"><h2 id="Plot">Plot
  <span class="mw-editsection">[edit]</span></h2></div>
  <p>The plot is <a href="/wiki/Konoha">Konoha</a>-centred.</p>
  <div class="mw-heading mw-heading3"><h3 id="Arc">First arc</h3></div>
  <p>A <i>ninja</i> appears in the first arc.</p>
  <div class="mw-heading mw-heading2"><h2 id="Production">Production</h2></div>
  <table class="infobox"><tr><th>Unrelated infobox text</th></tr></table>
  <p>Produced in <a href="/wiki/Japan">Japan</a>.</p>
  <div class="mw-heading mw-heading2"><h2 id="References">References</h2></div>
  <p>Service references and citation list.</p>
  <div class="mw-heading mw-heading2"><h2 id="Music">Music</h2></div>
  <p>Music <script>alert('unsafe')</script> OP details.
     <a href="javascript:alert(1)">Bad</a> <a href="/wiki/Opening">Opening</a>.</p>
  <div class="navbox">Link list and navigation noise</div>
</div>
"""


class WikipediaSourceTests(unittest.TestCase):
    def test_language_endpoint_is_allowlisted(self) -> None:
        self.assertEqual(wikipedia_api_url("ru"), "https://ru.wikipedia.org/w/api.php")
        with self.assertRaises(ValueError):
            wikipedia_api_url("evil.example")
        self.assertIn("Naruto", wikipedia_page_url("en", "Naruto"))

    def test_link_filter_blocks_unsafe_urls_and_preserves_citations(self) -> None:
        base = wikipedia_page_url("en", "Naruto")
        self.assertIsNone(_safe_link("javascript:alert(1)", base=base))
        self.assertIsNone(_safe_link("data:text/html;base64,xxx", base=base))
        self.assertIsNone(_safe_link("https://user:pass@example.org", base=base))
        self.assertEqual(_safe_link("/wiki/Japan", base=base), "https://en.wikipedia.org/wiki/Japan")
        self.assertEqual(_safe_link("#Reception", base=base), base + "#Reception")

    def test_clean_mode_strips_service_sections_but_keeps_parent_child_and_links(self) -> None:
        sections = parse_wikipedia_html(
            EXAMPLE_WIKI_HTML,
            url=wikipedia_page_url("en", "Naruto"),
            mode="clean",
        )
        titles = [item.title for item in sections]
        self.assertEqual(titles, ["Introduction", "Plot", "Production", "Music"])
        plot = "".join(p for row in sections[1].paragraphs for p in row)
        self.assertIn("<b>First arc</b>", plot)
        self.assertIn("first arc", plot)
        self.assertIn('href="https://en.wikipedia.org/wiki/Konoha"', plot)
        intro = "".join(p for row in sections[0].paragraphs for p in row)
        self.assertNotIn("[edit]", intro)
        self.assertIn("<b>ninjas</b>", intro)
        self.assertIn("<a", intro)
        production = "".join(p for row in sections[2].paragraphs for p in row)
        self.assertNotIn("Unrelated infobox text", production)
        music = "".join(p for row in sections[3].paragraphs for p in row)
        self.assertIn("Opening</a>", music)
        self.assertNotIn("javascript:", music)
        self.assertNotIn("alert(", music)
        self.assertNotIn("Link list", music)

    def test_raw_mode_keeps_editorial_noise_but_never_executable_html(self) -> None:
        sections = parse_wikipedia_html(
            EXAMPLE_WIKI_HTML,
            url=wikipedia_page_url("en", "Naruto"),
            mode="raw",
        )
        joined = "\n".join(
            fragment for section in sections for paragraph in section.paragraphs
            for fragment in paragraph
        )
        self.assertIn("References", [section.title for section in sections])
        self.assertTrue(any("[edit]" in section.title for section in sections))
        self.assertIn("Unrelated infobox text", joined)
        self.assertIn("navigation noise", joined)
        self.assertNotIn("<script", joined)
        self.assertNotIn("javascript:", joined)
        self.assertNotIn("alert(", joined)

    def test_parent_section_is_paginated_without_breaking_html(self) -> None:
        para = ('<a href="https://en.wikipedia.org/wiki/Test">hello</a>',) * 200
        section = WikipediaSection("Plot", (para,))
        pages = paginate_section(section, limit=500)
        self.assertGreater(len(pages), 1)
        for value in pages:
            self.assertLessEqual(len(value), 500)
            self.assertEqual(value.count("<a "), value.count("</a>"))


    def test_paragraph_flow_matches_wikipedia_not_one_line_per_link(self) -> None:
        sections = parse_wikipedia_html(
            EXAMPLE_WIKI_HTML,
            url=wikipedia_page_url("en", "Naruto"),
            mode="clean",
        )
        intro = paginate_section(sections[0])
        self.assertEqual(len(intro), 1)
        self.assertIn(
            'Anime <a href="https://en.wikipedia.org/wiki/Naruto">Naruto</a>'
            ' is a work about <b>ninjas</b> and ',
            intro[0],
        )
        self.assertNotIn("</a>\n", intro[0])
        plot = paginate_section(sections[1])
        self.assertEqual(len(plot), 1)
        self.assertIn("\n\n<b>First arc</b>\n\nA <i>ninja</i>", plot[0])

    def test_short_chapter_has_only_cross_section_navigation(self) -> None:
        article = WikipediaArticle(
            pageid=123,
            title="Naruto",
            language="en",
            url=wikipedia_page_url("en", "Naruto"),
            sections=(
                WikipediaSection("World", (("Short world text.",),)),
                WikipediaSection("Plot", (("A scene.",),)),
                WikipediaSection("Production", (("Behind the scenes.",),)),
            ),
        )
        body, menu = _chapter_page(
            article, 1, 0, token="abc", locale="en",
        )
        callbacks = [
            button.callback_data
            for row in menu.inline_keyboard for button in row
        ]
        self.assertEqual(
            callbacks,
            [
                "wk:abc:r:0:0",
                "wk:abc:r:2:0",
                "wk:abc:l:0",
                "av:abc:main:0",
            ],
        )
        self.assertEqual(body.splitlines()[0], "<b>Plot</b>")
        self.assertNotIn("1/1", body)
        self.assertEqual(len(paginate_section(article.sections[1])), 1)

    def test_japanese_alias_only_when_present_in_actual_wiki_article(self) -> None:
        with_japanese = WikipediaArticle(
            1, "Naruto", "en", "https://en.wikipedia.org/wiki/Naruto",
            (), japanese_phrases=("Japanese title NARUTO -ナルト-",),
        )
        without_japanese = WikipediaArticle(
            2, "Naruto", "en", "https://en.wikipedia.org/wiki/Naruto",
            (), japanese_phrases=("English text only",),
        )
        self.assertEqual(
            japanese_title_in_article(with_japanese, "ナルト"), "ナルト"
        )
        self.assertEqual(
            japanese_title_in_article(with_japanese, "NARUTO -ナルト-"),
            "NARUTO -ナルト-",
        )
        self.assertIsNone(
            japanese_title_in_article(without_japanese, "ナルト")
        )
        self.assertIsNone(
            japanese_title_in_article(with_japanese, "One Piece")
        )

    def test_excessive_html_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_wikipedia_html(
                "a" * (MAX_HTML_BYTES + 1), url="https://en.wikipedia.org/wiki/Test",
                mode="clean",
            )


class WikipediaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_article_requests_are_read_only_and_cached(self) -> None:
        client = WikipediaClient()
        async def fake_query(lang, params):
            self.assertEqual(lang, "en")
            if params["action"] == "query":
                self.assertEqual(params["list"], "search")
                return {"query": {"search": [
                    {"pageid": 123, "title": "Naruto", "snippet": "Japanese <span>anime</span>"},
                    {"pageid": 125, "title": "Other Naruto", "snippet": ""},
                ]}}
            self.assertEqual(params["action"], "parse")
            self.assertEqual(params["pageid"], 123)
            return {"parse": {
                "pageid": 123, "title": "Naruto", "revid": 77,
                "text": EXAMPLE_WIKI_HTML,
            }}
        with patch.object(client, "_query", side_effect=fake_query) as query:
            hits = await client.search(language="en", title="Naruto")
            self.assertEqual(len(hits), 2)
            self.assertEqual(hits[0].pageid, 123)
            self.assertEqual(hits[0].snippet, "Japanese anime")
            article = await client.article(language="en", pageid=123, mode="clean")
            self.assertEqual(article.title, "Naruto")
            self.assertEqual(article.revision_id, 77)
            self.assertIn("Plot", [x.title for x in article.sections])
            second = await client.article(language="en", pageid=123, mode="clean")
            self.assertIs(second, article)
            self.assertEqual(query.call_count, 2)
            raw = await client.article(language="en", pageid=123, mode="raw")
            self.assertNotEqual(len(raw.sections), len(article.sections))
            self.assertEqual(query.call_count, 3)


class WikipediaUiTests(unittest.TestCase):
    def test_wikipedia_above_related_and_recommendations_in_both_card_modes(self) -> None:
        anime = AggregatedAnime(title="Naruto")
        for markup in (
            navigation_keyboard("token", anime, "main", 0, 1, locale="ru", show_related=True),
            fallback_main_keyboard("token", locale="ru", show_related=True),
        ):
            self.assertIsInstance(markup, InlineKeyboardMarkup)
            actions = [
                b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data
            ]
            self.assertLess(
                actions.index("av:token:wikipedia:0"), actions.index("ci:token:related")
            )
            self.assertLess(
                actions.index("av:token:wikipedia:0"), actions.index("ci:token:recommendations")
            )

    def test_article_has_header_with_page_number_and_attached_links(self) -> None:
        paragraph = ('<a href="https://ru.wikipedia.org/wiki/Test">Тест</a> ',) * 600
        article = WikipediaArticle(
            pageid=10, title="Тест", language="ru",
            url="https://ru.wikipedia.org/wiki/Test",
            sections=(WikipediaSection("Сюжет", (paragraph,)),),
        )
        rendered, markup = _chapter_page(article, 0, 1, token="card", locale="ru")
        self.assertIn("<b>Сюжет</b>\n\n", rendered)
        self.assertIn('<a href="https://ru.wikipedia.org/wiki/Test">Тест</a>', rendered)
        self.assertIn("CC BY-SA 4.0", rendered)
        self.assertIn("wk:card:r:0:0", str(markup.model_dump()))

    def test_search_prefers_locale_title_not_broad_search(self) -> None:
        anime = AggregatedAnime(
            title="Naruto",
            titles={"russian": "Наруто", "english": "Naruto", "japanese": "ナルト"},
        )
        self.assertEqual(_search_term(anime, "ru"), "Наруто")
        self.assertEqual(_search_term(anime, "en"), "Naruto")
        self.assertEqual(_search_term(anime, "ja"), "ナルト")
        self.assertEqual(
            _japanese_label(
                anime, WikipediaHit(10, "Наруто", "Оригинал ナルト", "ru")
            ),
            "Наруто (ナルト)",
        )
        self.assertEqual(
            _japanese_label(
                anime, WikipediaHit(11, "Наруто", "Японский оригинал не указан", "ru")
            ),
            "Наруто",
        )


class WikipediaPreferenceTests(unittest.TestCase):
    def test_persists_with_language_across_sqlite_store_instances(self) -> None:
        with TemporaryDirectory() as path:
            p = Path(path) / "users.db"
            store = SQLiteUserSettingsStore(p)
            self.assertEqual(store.get(7).wikipedia_mode, "clean")
            store.set_locale(7, "ru")
            store.set_wikipedia_mode(7, "raw")
            self.assertEqual(store.get(7).locale, "ru")
            self.assertEqual(store.get(7).wikipedia_mode, "raw")
            store.set_locale(7, "en")
            self.assertEqual(store.get(7).wikipedia_mode, "raw")
            other = SQLiteUserSettingsStore(p)
            self.assertEqual(other.get(7).wikipedia_mode, "raw")
            self.assertEqual(other.get(8).wikipedia_mode, "clean")
            with self.assertRaises(ValueError):
                other.set_wikipedia_mode(7, "unsafe")
            self.assertEqual(other.get(7).wikipedia_mode, "raw")

    def test_migrates_preexisting_user_settings_column(self) -> None:
        import sqlite3
        from contextlib import closing
        with TemporaryDirectory() as path:
            p = Path(path) / "old.db"
            with closing(sqlite3.connect(p)) as conn, conn:
                conn.execute(
                    "CREATE TABLE user_settings ("
                    "platform TEXT NOT NULL, user_id INTEGER NOT NULL, "
                    "locale TEXT NOT NULL DEFAULT 'en', "
                    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "PRIMARY KEY(platform, user_id))"
                )
                conn.execute(
                    "INSERT INTO user_settings(platform,user_id,locale) VALUES('telegram',1,'ru')"
                )
            store = SQLiteUserSettingsStore(p)
            self.assertEqual(store.get(1).wikipedia_mode, "clean")
            store.set_wikipedia_mode(1, "raw")
            self.assertEqual(store.get(1).locale, "ru")
            self.assertEqual(store.get(1).wikipedia_mode, "raw")


if __name__ == "__main__":
    unittest.main()
