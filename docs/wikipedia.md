# Wikipedia reader for Telegram cards

Wikipedia is intentionally **not** an anime aggregation source. Its prose
sometimes describes an entire franchise, manga, film, fictional setting or
character rather than a specific anime series.

This integration **does not use Wikidata or modify anime-mapping-db**.

## Open a page

1. Open an anime card, then tap **Wikipedia**. It appears in its own inline
   row above **Related** and **Recommendations** in rich and plain card views.
2. The bot searches public Wikipedia by the card's title in **the user's
   global /settings language**. There are no language or rendering controls
   cluttering the Wikipedia card. Search results are **not verified anime
   matches**, and no result is automatically selected.
3. Search results are **title-only inline buttons**, without numbered
snippets/descriptions. Only hits with Japanese characters in the visible
Wikipedia title or snippet are included; the original-title retry uses the
same filter. Choose the correct page explicitly; when the
   requested-language article is missing, change the bot language in /settings.
4. Major headings are navigable sections. Lower-level headings and all their
   text remain inside their **parent section**, not duplicated as buttons.
5. Reading a section shows a bold heading and the original flowing Wikipedia
   prose, its paragraph breaks, lists, links and subheadings.
   **Previous/next section** arrow-only buttons work even for short sections.
   Overflow pages use arrow / page-count / arrow navigation; the count opens
   a numbered selection list of up to the configured page size.
6. Page arrows appear **only when the section exceeds the applicable
   message size limit**: rich Telegram HTML first, standard Telegram HTML
   as a fallback. Page numbering is shown only for longer sections.
7. A Japanese original title appears only when its exact writing from the
   anime metadata is actually present in the selected Wikipedia page.
   Search-result button titles can also include that spelling, but only
   when the actual result snippet contains it.

The original anime card remains unchanged and its records are not enriched
or replaced by Wikipedia text.

## Choose the display mode

Every user can open **/settings → Wikipedia · display mode** and select:

- **Clean** (default): omit editing links, navigation templates, infoboxes,
  citation/navigation boilerplate and selected service sections while
  retaining genuine article headings, text, lists and safe inline links.
- **Full RAW**: preserve all available *visible article text* including
  editorial/service elements and chapters, still grouped by headings.

Full RAW is an unfiltered reading mode, not execution or raw insertion of
unsafe HTML into Telegram: scripts, executable embeds, forms and unsafe
link schemes are excluded in both modes. Unsupported complicated tables
are better viewed in the linked original article.

The choice is saved per user/platform in the existing user settings database.
There is a compatible migration for SQLite and PostgreSQL. Changing language
does not overwrite the Wikipedia display setting; it is available to all
ordinary users, not only Telegram administrators.

## Technical boundary

- Adapter: sources/providers/wikipedia.py, separate from anime aggregator.
- Telegram UI: bots/telegram/card/features/wikipedia.py.
- The bot contacts only allowlisted fixed-language Wikipedia API domains.
  User-supplied URLs are never fetched by the server.
- MediaWiki Action API: read-only query/list=search and parse/text/revid
  operations using the numeric page ID from the user's selected result.
- Article data is only requested after choosing a page. The results have a
  bounded TTL/LRU cache, response-size limit and timeout handling.
- Callbacks are bound to the user who owns the original anime-card session.
  Wikipedia result identities never enter anime-mapping-db.
- Wikipedia prose is preserved as continuous paragraphs, not split into a
  newline for every link or inline markup. Short sections have no page controls.
  Larger sections are paginated at balanced HTML-fragment boundaries only
  when they exceed the rich-message or standard-message limit.
- No AI rewriting, summarization or speculative Japanese aliases are used.
- Each block has a Wikipedia link and CC BY-SA 4.0 attribution. No Wikipedia
  images are copied into the bot without separately checking licenses.

Languages: Russian, English, Japanese, German, Spanish, French and Italian.
If an article exceeds the response-size cap, users can view the original
Wikipedia page via the external URL.
