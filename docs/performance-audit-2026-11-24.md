# Telegram anime request latency audit — 2026-11-24

Scope: read-only investigation of production, existing logs and code paths.
No performance optimization or production ENV changes were made.

## Evidence and limits

- Repository anime-db-project/anime-db-bot; audit baseline cf84947.
- Log source: daily logs/YYYY.MM.DD/users.log and bot.log.
- User-update timings span the entire handler; they are not per-stage timings
  and the action field says command/callback, not the exact command.
- The small, mixed historical samples do NOT constitute a controlled benchmark.

| Day | Commands | Median | Maximum |
| --- | ---: | ---: | ---: |
| 2026-11-06 | 5 | 1.24 s | 4.37 s |
| 2026-11-18 | 9 | 1.30 s | 19.76 s |
| 2026-11-22 | 13 | 1.61 s | 24.21 s |
| 2026-11-24 | 3 | 0.14 s | 5.02 s |

November 22 callbacks (n=159): median ~0.13 s, p90 ~1.01 s, max
~8.47 s. November 24 callbacks (n=26): median ~0.10 s, max ~1.59 s.

## Audited production configuration

- Search: Shikimori, MAL/Jikan, AniList, Kitsu; per-provider page cap 4,
  page size 20; optional cross-language expansion enabled.
- Rich media fallback order: direct, url, ram, disk; RAM/disk enabled.
  Local download timeout 3 s; total fallback budget 7 s.
- Telegram uses local tg-bot-api, with source proxy profiles disabled at
  the observed startup. Search cache is in-memory.
- IMDb ratings are served from local SQLite. IMDb remote API is not
  configured in this deployment.

## Critical path before first search preview

1. SearchService.collect() waits for concurrent providers; each provider
   may request four sequential pages inside a 16 s provider budget.
2. When enabled, expand_text_search() can add up to 8 s waiting for
   additional searches or identity enrichment.
3. Only then does _send_preview() send the first plain Telegram message.
   A rich-image upgrade is a separate Telegram API request.
4. This initial search-preview path does not use the RAM/disk rich-media
   fallback manager.
5. One search-expansion-budget warning was logged on November 22.
   The observed maximum ~24 s whole-command time is consistent with
   16+8 s budgets but cannot be conclusively attributed to them.

## Critical path before first full anime card

1. aggregate_and_send() awaits identity resolution, concurrent metadata
   requests and optional second passes before sending anything.
2. Identity enrichment has an 8 s budget per pass, concurrent source
   get_details() calls have a 12 s budget per pass; conditional second
   passes can add more latency. Sources can use retry/backoff internally.
3. IMDb rating enrichment is awaited before the card is sent, although
   it is local SQLite on this deployment.
4. Full rich cards can contain multiple remote poster URLs. Media
   delivery attempts direct and URL-backed methods before local RAM/disk
   uploads. A successful remote Telegram send can itself be slow.
   No per-stage timings are currently logged. The fallback-loop deadline
   is not separately enforced around every remote await send(...) call.
5. Related-title availability is checked asynchronously after the card
   has been sent and is not part of first-card latency.

## Evidence about media fallback

- Rich-media fallback warnings occurred on Nov 18 and Nov 19 (four
  events each), establishing that alternatives were actually attempted.
- No such warning was found in the examined Nov 22–24 bot logs.
  This does not rule out slow successful remote media sends or entirely
  prove the absence of local uploads.
- Media fallback is plausible for some full-card delays, but is not
  shown to be the primary reason for the initial text-search delay.

## Diagnostics to implement only after approval

1. Record per-stage timings: initial provider collection, expansion,
   first plain preview, rich poster edit, identity pass, metadata passes,
   local IMDb, rich send and each media strategy. Log no query strings,
   user identifiers, image URLs or secrets.
2. Compare controlled cold and warm runs of identical anime titles;
   distinguish time-to-first-message from time-to-rich-image.
3. Evaluate expansion and provider page budgets in an isolated environment
   without modifying production settings.
4. Only after validated phase timings choose whether to shorten initial
   collection, stream results, defer optional enrichment or rework media
   delivery.

Conclusion: there are several concrete synchronous wait points; the
primary bottleneck cannot yet be ranked from existing aggregate logs.
No performance changes applied.
