> **AI-assisted project:** Developed with the assistance of AI.

<p align="center">
  <strong>English</strong> · <a href="docs/README.ru.md">Русский</a> · <a href="docs/README.ja.md">日本語</a> · <a href="docs/README.de.md">Deutsch</a> · <a href="docs/README.es.md">Español</a> · <a href="docs/README.fr.md">Français</a> · <a href="docs/README.it.md">Italiano</a>
</p>

# anime-db-bot

[![Python](https://img.shields.io/badge/Python-3.11--3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![CI](https://github.com/anime-db-project/anime-db-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/anime-db-project/anime-db-bot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-D22128.svg)](LICENSE)

A Telegram bot for searching anime, combining metadata from independent databases, and exploring titles, episodes, characters, staff, and related works in one interface.

## Project ecosystem in numbers

The bot belongs to a family of independently usable anime databases. These numbers illustrate the reach of the underlying projects, **not** a guarantee that all titles have complete metadata or skip timestamps.

| Metric | Value |
| --- | ---: |
| Stable anime title identities in Anime Mapping DB | **34,851** (≈35k) |
| External identifier namespaces | **55** |
| Accepted cross-ID mapping edges | **1,225,741** |
| Normalized episodes in Anime Timestamp DB | **57,623** |
| Normalized skip intervals in Anime Timestamp DB | **214,241** |

Verified snapshots from **5 November 2026**: [Mapping DB release](https://github.com/anime-db-project/anime-mapping-db/releases/tag/v2026.11.05.24) and [Timestamp DB release](https://github.com/anime-db-project/anime-timestamp-db/releases/tag/v2026.11.05.8). The rounded **≈35,000 anime titles** corresponds to 34,851 stable AMDB identities in the reference snapshot. Check each project's latest `manifest.json` for updated values.

You can use the two SQLite databases outside Telegram: a website or library tool can translate an anime's MAL/AniList ID through **Anime Mapping DB**, then optionally look up episode intervals through **Anime Timestamp DB**. Python resolvers, read-only SQL and portable export examples are documented in each database repository. **anime-db-bot currently consumes Mapping DB, but does not depend on or automatically integrate Timestamp DB.**

**[Anime Mapping DB](https://github.com/anime-db-project/anime-mapping-db)** · **[Anime Timestamp DB](https://github.com/anime-db-project/anime-timestamp-db)**

## Features

- Search anime by title, external ID, or image, with interactive result pagination.
- Combine ratings, descriptions, release information, cast, staff, relations and recommendations from multiple providers.
- Browse episodes, OP/ED information and available skip timestamps; expensive sections load only when opened.
- Keep cross-database identity conservative: only verified ID relationships may merge titles.
- Use persistent user preferences, moderation tools, per-source rate limits, proxies, and User-Agent profiles.
- Choose among seven interface languages; source text is displayed in the languages upstream provides.

## Search and navigation

The `/anime` workflow accepts a title or an explicit provider ID. Search candidates are paginated before a detailed card is opened. Results use names and aliases from different writing systems, but exact external-ID lookups never rely on a title-only match.

Image recognition accepts a Telegram image or public image URL. Available providers show similarity, episode and timestamp evidence before the user opens an anime card; guest quotas and API-key quotas are handled per provider.

The main card presents normalized title information. More expensive sections—characters, staff, voice cast, screenshots, detailed ratings, descriptions, trailers, related titles, recommendations, and episodes—are loaded on demand. Episode views can include seasons/cours/specials when the underlying source provides that structure.

## Data sources

The bot uses MyAnimeList (with Jikan as an optional supplement), AniList, Shikimori, Kitsu, AniDB (optional client registration), and AniSearch for anime metadata. Additional services serve narrowly scoped features rather than acting as interchangeable metadata databases.

| Features | Data sources |
| --- | --- |
| Anime metadata and search | [MyAnimeList](https://myanimelist.net/) / [Jikan](https://jikan.moe/), [AniList](https://anilist.co/), [Shikimori](https://shikimori.one/), [Kitsu](https://kitsu.app/), [AniDB](https://anidb.net/), [AniSearch](https://www.anisearch.com/) |
| Additional ratings | [IMDb](https://www.imdb.com/) |
| Image recognition | [trace.moe](https://trace.moe/), [What Anime Is This](https://whatanimeisthis.com/) |
| Episodes, themes and skip intervals | [AnimeThemes](https://animethemes.moe/), [AniSkip](https://aniskip.com/), [Anime Skip](https://anime-skip.com/), [Baha Anime Skip](https://github.com/JacobLinCool/baha-anime-skip), [SkipDB](https://github.com/SkipDB-TV/skipdb) |

[Anime Mapping DB](https://github.com/anime-db-project/anime-mapping-db) provides cross-database ID mappings. SkipDB's timestamp provider is present but currently dormant; a source listed here does not imply that all titles or episodes have data.

## Provider capabilities

| Source | Title search | Metadata | People | Characters | Episodes | Relations | Recommendations |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Shikimori | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ |
| MAL / Jikan | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| AniList | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ |
| Kitsu | ✓ | ✓ | — | — | ✓ | ✓ | — |
| AniDB | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| AniSearch | — | ✓ | ✓ | ✓ | ✓ | ✓ | — |

AniSearch enriches confirmed titles with localized metadata and voice credits but does not participate in ordinary title search. AnimeThemes enriches episodes with OP/ED metadata rather than creating anime identities. SkipDB remains an inactive runtime timestamp provider, not a promise of skip coverage.

## Identity and data aggregation

The project does not assign authority to a single anime catalog. MAL, AniList, Shikimori, Kitsu, AniDB and AniSearch identities remain distinct until confirmed mappings associate them. A matching number, similar title or franchise membership is not sufficient proof.

Runtime mapping is direct-only and conflict-aware. Aggregation retains attribution to each responding service; provider-specific views remain available where implemented. IMDb ratings require a direct, unambiguous anime-native mapping; TVDB/TMDB bridge matches alone cannot identify the correct IMDb title.

```text
Telegram /anime → Search → Confirmed IDs → Aggregated metadata
                                  ↑
                       mappings.db (local SQLite)
                                  ↓
               Anime card → lazy source-specific sections
```

## Quick start

Python 3.11–3.14 on Linux, macOS or Windows. Clone the project and use its cross-platform launcher:

```bash
git clone https://github.com/anime-db-project/anime-db-bot.git
cd anime-db-bot
python3 scripts/project.py setup
# Add BOT_TOKEN to .env.d/telegram.env
python3 scripts/project.py check
python3 scripts/project.py run
```

Windows: use `py -3 scripts\project.py` instead of `python3 scripts/project.py`. Background operation uses `python3 scripts/project.py start` (`status`, `restart`, `stop` are also supported); Linux deployments may use `./start.sh`.

## Ready-to-run releases and GHCR

GitHub Releases include standalone console executables for Windows x64,
Linux x64/ARM64, macOS Intel/Apple Silicon and a multiarch GHCR Docker image.
Native releases bundle Python, dependencies and configuration templates;
they do not require Python to be installed. Double-click the Windows binary,
use the included `Run.command` on macOS, or run the Linux binary/`Run.sh`.

Use `run` (foreground), `start` (background), `stop`, `restart`,
`status`, `init`, `check`, or `doctor`. Docker runs in the foreground
by default and is detached with `docker run -d`; persistent data lives in
`/data`. See the [release setup guide](docs/releases.md) for environment
variables, CLI flags, startup output and deployment examples.

**[Download a release](https://github.com/anime-db-project/anime-db-bot/releases)** ·
**[GHCR image](https://github.com/anime-db-project/anime-db-bot/pkgs/container/anime-db-bot)**

## Local operator console (native and Docker)

The new operator console exposes the bot's anime search, full card sections,
image recognition, episodes, timestamps, OP/ED themes, cast, recommendations,
moderation, configuration and mapping maintenance using the same services
as Telegram. Enter `console` for an interactive REPL or `cli COMMAND` for
scripting; **neither command starts a second Telegram polling instance**.

```sh
./anime-db-bot console
./anime-db-bot cli search Frieren
./anime-db-bot cli anime mal:5114 --section related
./anime-db-bot cli --json metrics

# Against a running Docker container:
docker exec -it anime-db-bot python -m scripts.release_entry console
docker exec anime-db-bot python -m scripts.release_entry cli users
docker exec anime-db-bot python -m scripts.release_entry cli source-health
```

A terminal-attached foreground `run` also opens the interactive operator
prompt; `run --no-console` disables it. Commands such as `/anime`, `/users`,
`/ban`, `/mapping_set` and `/mapping_confirm` work with familiar slash
syntax. A private rolling status snapshot provides current worker metrics to
separate console processes; secrets are not exported.

See **[Operator console documentation](docs/operator-console.md)** for every
command, flags, confirmation rules, JSON output and Docker examples.

## REST v1 and GraphQL API

The optional authenticated HTTP API exposes the same anime discovery,
source-scoped identity, lazy card sections, people, episode timestamps,
relations, image recognition, user moderation, mappings, configuration,
diagnostics and maintenance actions as the operator console.

```bash
# API-only native binary: no Telegram token or polling required
./anime-db-bot api

# External API requests (keys configured in .env.d/api.env)
curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/search?query=Frieren"

# Docker-only API service
docker compose -f compose.api.yml up -d
```

Endpoints: `/api/v1/...`, `/api/v1/openapi.json` (OpenAPI 3.1), and
`/graphql` (typed queries/mutations). By default the API is **off** and
binds only to loopback. A bearer/API key or explicitly configured local
authentication is required; admin mutations require `confirm=true`.
Long-running index/mapping operations return background job IDs.

See [HTTP API documentation](docs/api.md) for all operations, authentication,
CORS, GraphQL examples, resource limits, and Docker instructions.

## Deployment and operation

The cross-platform launcher manages virtual environments, checks, foreground/background execution and process status. For Linux, `start.sh` provides the existing tmux-based deployment path. Windows has `start.ps1`/`start.cmd`; macOS also includes `start.command`.

Docker Compose and local Jikan are optional supplements. The official MAL integration can operate without Jikan. Use the published companion image with Docker Compose; local builds are manual via `./scripts/build-jikan.sh`. See [Docker deployment](docs/releases.md#docker--ghcr).

```bash
python3 scripts/project.py doctor
python3 scripts/project.py setup
python3 scripts/project.py check
python3 scripts/project.py start
python3 scripts/project.py status
python3 scripts/project.py restart
python3 scripts/project.py stop
```

## Lifecycle and recovery policy

Bot process failures can be handled by an optional supervisor. The default
policy remains **stop**. To restart automatically after fatal errors, add these
settings to `.env.d/lifecycle.env` (create it from
`.env.example.d/lifecycle.env` if needed):

```dotenv
BOT_ON_FAILURE=restart
BOT_ON_CLEAN_EXIT=stop
BOT_RESTART_MAX_ATTEMPTS=5
BOT_RESTART_DELAY_SECONDS=5
BOT_RESTART_MAX_DELAY_SECONDS=60
BOT_RESTART_RESET_AFTER_SECONDS=300
BOT_SHUTDOWN_TIMEOUT_SECONDS=10
```

The bot subprocess is supervised by `./start.sh` and
`scripts/project.py run/start`. Expected manual stops always stop the
supervisor and child, even if the clean-exit policy requests restarts. Handled
provider/API errors do not restart the whole bot. A restart budget and capped
exponential backoff prevent hot crash loops. See
[configuration reference](docs/configuration.md#process-lifecycle-and-automatic-recovery)
for policy semantics.

## Usage

Use `/anime` to choose a search source or enter a title. An image attached to `/anime` or a public image URL activates image search when a backend is available. `/settings` changes preferences; `/cancel` ends pending input.

```text
/anime Frieren
/anime mal:5114
/anime anilist:<id>
/anime shikimori:<id>
/settings
/ping
/cancel
```

## Telegram commands

The interactive `/anime` command is the entry point for ordinary searches. Administrative commands require configured Telegram administrator IDs and enforce access control. Mapping changes have a confirmation step and are recorded in an audit history.

| Command | Purpose |
| --- | --- |
| `/start`, `/ping` | Start/help and basic health check |
| `/anime`, `/anime <title>`, `/anime mal:<id>` | Search by title, numeric ID, provider ID or image |
| `/settings` | User language and preferences |
| `/cancel` | Cancel pending interactive input |
| `/users`, `/status`, `/source_health` | List known users; runtime statistics and provider health |
| `/ban <user_id> [seconds] [reason]`, `/unban <user_id>` | Ban or unban a Telegram user |
| `/mapping_overrides`, `/mapping_history [limit]` | Inspect overrides and mapping audit history |
| `/mapping_set`, `/mapping_delete`, `/mapping_confirm`, `/mapping_cancel` | Stage mapping decisions, then confirm or cancel |

## Wikipedia article reader

An anime card now includes a separate **Wikipedia** inline button directly
above Related and Recommendations. It is independent of anime aggregation:
you explicitly select a search result, choose a major article section, and
read its text in pages with **bold section titles, page numbers and inline
links**. Lower-level subsections stay inside their parent chapter. No AI
summary or automatic wiki-title-to-anime association is performed.

Every user can choose **/settings → Wikipedia · display mode**:
Clean (omit service/navigation noise) or Full RAW (retain all available
visible service text while filtering executable HTML). This preference is
persisted in SQLite or PostgreSQL and does not require restarting the bot.
In the Recommendations screen, the card-opening action is now **Details**
(«Подробнее»). Wikidata and anime-mapping-db are unchanged.

[Wikipedia integration and safety details](docs/wikipedia.md).

## Administrator configuration in Telegram

Authorized Telegram administrators can open **/settings → Bot configuration
(ENV)** in a **private chat**, or send `/admin_config` (alias:
`/bot_config`). The UI enumerates every documented option from
`.env.example.d/*.env` automatically and offers category browsing,
paginated keys, name search, descriptions, masked current values, editing and
resetting. New keys are picked up automatically from the templates.

Every change requires a separate inline confirmation, validated against
the same typed configuration rules as the local CLI. Tokens, passwords, URLs
and other sensitive values are never displayed; messages containing secrets
are deleted before confirmation. No change takes effect until a normal bot
restart, and process environment variables still override local ENV files.
Regular users cannot enter the configuration UI, including via forwarded
callback buttons. See [configuration reference](docs/configuration.md).

Release archives are published together with **SHA256SUMS.txt** and
**manifest.json** containing artifact SHA-256 hashes, sizes, source commit and
platform identifiers. The workflow validates both before publication.

## Configuration

`BOT_TOKEN` is required. Add provider credentials only for the services you intend to enable; `MAL_CLIENT_ID` improves MyAnimeList coverage and `ANIDB_CLIENT` enables AniDB. Optional reverse-image API keys are not required for guest operation. Proxy and User-Agent profiles are configured under `.proxies.d/` and `.user-agents.d/`.

```text
.env.example.d/      -> .env.d/
.proxies.example.d/  -> .proxies.d/
.user-agents.example.d/ -> .user-agents.d/
```

The bot downloads and verifies release artifacts from **Anime Mapping DB**. It consumes `mappings.db` directly and does not build mapping datasets or automatically incorporate **Anime Timestamp DB**.

## Configuration reference

Configuration templates are copied into `.env.d/` without overwriting existing settings. Only the predefined env filenames are loaded. Process environment values take precedence; duplicate project-file keys cause startup failure. Keep credentials and private profiles out of Git.

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token (required) |
| `MAL_CLIENT_ID` | Official MyAnimeList API client ID |
| `ANIDB_CLIENT` | Optional AniDB HTTP API client name |
| `ANILIST_ACCESS_TOKEN` | Optional authenticated AniList access |
| `TRACE_MOE_API_KEY` | Optional trace.moe key; guest mode exists |
| `WHATANIMEISTHIS_API_KEY` | Optional What Anime Is This key; guest mode exists |
| `ADMIN_TELEGRAM_USER_IDS` | Telegram IDs permitted to run admin commands |
| `TELEGRAM_BOT_API_URL` | Custom Telegram Bot API endpoint |
| `MAPPINGS_DB_PATH` | Runtime cross-ID mapping database path |
| `IMDB_RATINGS_BACKEND` | IMDb local/API/off selection |
| `BAHA_ANIME_SKIP_DB_PATH` | Local Baha Anime Skip index path |

Each source can have an independent TOML proxy profile in `.proxies.d/`. Supported protocols include HTTP, SOCKS4 and SOCKS5. Higher priorities win, equal-priority entries rotate and lower priorities are fallbacks; an active proxy profile does not silently bypass failed proxies using direct access.

Custom User-Agent profiles live in `.user-agents.d/` and support prioritization. Both types of profile are loaded when the process starts, so restart after editing. Self-hosted Telegram Bot API endpoints are optional and must be secured.

```toml
enabled = true
[[proxy]]
name = "primary"
enabled = true
priority = 100
url = "socks5://127.0.0.1:1080"
```

The reference templates under `.env.example.d/`, `.proxies.example.d/`, and `.user-agents.example.d/` define available settings and profile filenames.

## Local data and maintenance

The mapping updater downloads a versioned `mappings.db.zst`, verifies checksums and replaces the local database atomically while preserving audited overrides. The bot reads the compact runtime database; it never downloads the mapping evidence DB for normal operation.

```bash
python3 scripts/project.py mappings --status
python3 scripts/project.py mappings --force
python3 scripts/project.py mappings --override-list
python3 scripts/project.py mappings --override-history
python3 scripts/project.py imdb-index
python3 scripts/project.py baha-index
```

The optional local IMDb index keeps only ratings tied to accepted anime-native identifiers, rather than storing the full IMDb ratings dump. Baha Anime Skip timestamps are indexed locally and queried without live Bahamut requests. Both indexes can be refreshed independently after the mapping database is available.

User preferences, known-user data, moderation state and certain local quotas are persisted in SQLite. Daily logs are grouped under `logs/YYYY.MM.DD/` as `bot.log`, `users.log`, `api.log` and `errors.log`. The background launcher also writes `logs/launcher.log`.

## Episode data, OP/ED and skip intervals

Episode listings and skip timestamps are separate capabilities. The selected episode-list provider does not determine which timestamp service can be used: each provider keeps its own source IDs and scope. When no trusted match exists, the bot should display the absence of data rather than borrow an unrelated episode.

AnimeThemes adds opening/ending names, songs, artists and available video links to the relevant episode view. Timestamp services can identify intervals such as OP, ED and recap; their reported ranges may differ because source numbering and video editions differ. A label describing one source does not mean all sources confirm that interval.

## Operations and diagnostics

Before troubleshooting a missing title, confirm which source responded and whether the cross-ID mapping exists. An API timeout or missing optional key should affect its own provider, not the entire search flow. Use `/status` for rolling runtime statistics and `/source_health` for circuit-breaker state.

If the process does not start, run `doctor` and `check`, inspect `logs/YYYY.MM.DD/errors.log` and ensure `BOT_TOKEN` is set. Changes in `.env.d/`, proxy or User-Agent profiles require a restart. Check the mapping manifest and updater status before rebuilding optional IMDb/Baha indexes.

```bash
python3 scripts/project.py doctor
python3 scripts/project.py check
python3 scripts/project.py mappings --status
```

## Architecture and reliability

Search, identity resolution, source clients, aggregation, localization, persistence and Telegram presentation are separated. Source-level timeouts, retry policies, rate limits and circuit breakers reduce the impact of individual provider failures.

```text
bots/telegram/    Telegram UI, card and search handlers
core/             contracts, settings, HTTP and logging
search/           search source adapters
services/         identity, aggregation, runtime and media
sources/          API providers and source health
mappings/         local cross-ID resolution and updater
localization/     seven UI locale catalogs
jikan_service/    optional local MAL supplement
tests/            automated regression tests
```

## Development and security

Contributions should preserve conservative identity matching and avoid adding network calls to the initial card without justification. Use the supported Python versions and run the project checks before opening a pull request. Report vulnerabilities privately through the security guidance; never post tokens in issues.

```bash
python3 scripts/project.py setup --dev
python3 scripts/project.py check
```

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Code of Conduct](CODE_OF_CONDUCT.md)

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [Application architecture](docs/app-context.md) · [Localization](localization/README.md)
- [Anime Mapping DB](https://github.com/anime-db-project/anime-mapping-db)

## License and attribution

Source code is available under Apache-2.0. Third-party services and datasets retain their own licenses, attribution requirements, and API terms. Keep credentials outside version control. [Apache-2.0](LICENSE).

[OpenAI Codex](https://github.com/apps/openai-codex) · [Issues](https://github.com/anime-db-project/anime-db-bot/issues)

**Optional storage backends:** Redis search caching and PostgreSQL for
mutable user records are supported without changing the SQLite default.
Configuration, safe migration and optional local Docker services:
[docs/backends.md](docs/backends.md).

