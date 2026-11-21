# Configuration reference

**No code editing is required to tune the bot.** The shipped
[.env.example.d](../.env.example.d) directory is the complete list of supported
project settings with their defaults and comments. Existing installations may
keep using the single \`.env\` file.

## How settings are loaded

1. Existing \`.env\` plus supported files in \`.env.d/\` are read on startup.
2. Real process environment variables have priority over file values.
3. A key **must not be declared in two project files**. Duplicate declarations
   are errors rather than silently overriding another value.
4. Any missing variable uses the original built-in default.
5. Settings are **read once on startup**, not hot-reloaded. Restart the bot to
   apply changes. Never commit active env files containing credentials.

Inspect supported key names, or validate the current configuration (no secrets
are printed):

\`\`\`sh
python -m scripts.config_report --list
python -m scripts.config_report --check
python -m scripts.config_report --check --strict
\`\`\`

The strict command also flags key names not present in the sample files.
Unexpected names are warnings in normal mode, not automatically rejected.

## Telegram pagination: shared and per-section settings

The global setting is `TELEGRAM_PAGINATION_PAGE_SIZE=10` (valid 1–30).
Each Telegram block has an **optional** override in `.env.d/telegram.env`:

```dotenv
TELEGRAM_PAGINATION_PAGE_SIZE=10
TELEGRAM_PAGINATION_EPISODES_PAGE_SIZE=5
TELEGRAM_PAGINATION_WIKIPEDIA_PAGE_SIZE=
TELEGRAM_PAGINATION_SETTINGS_PAGE_SIZE=20
```

A blank per-block value inherits the global size; one section's custom size
does not affect any other section. Missing entries also inherit the global
size. All supplied values must be integers from 1 to 30.

| Block | Override variable |
|---|---|
| wikipedia | `TELEGRAM_PAGINATION_WIKIPEDIA_PAGE_SIZE` |
| settings | `TELEGRAM_PAGINATION_SETTINGS_PAGE_SIZE` |
| search | `TELEGRAM_PAGINATION_SEARCH_PAGE_SIZE` |
| episodes | `TELEGRAM_PAGINATION_EPISODES_PAGE_SIZE` |
| characters | `TELEGRAM_PAGINATION_CHARACTERS_PAGE_SIZE` |
| staff | `TELEGRAM_PAGINATION_STAFF_PAGE_SIZE` |
| voices | `TELEGRAM_PAGINATION_VOICES_PAGE_SIZE` |
| related | `TELEGRAM_PAGINATION_RELATED_PAGE_SIZE` |
| recommendations | `TELEGRAM_PAGINATION_RECOMMENDATIONS_PAGE_SIZE` |
| trailers | `TELEGRAM_PAGINATION_TRAILERS_PAGE_SIZE` |
| screenshots | `TELEGRAM_PAGINATION_SCREENSHOTS_PAGE_SIZE` |
| titles | `TELEGRAM_PAGINATION_TITLES_PAGE_SIZE` |
| popularity | `TELEGRAM_PAGINATION_POPULARITY_PAGE_SIZE` |
| descriptions | `TELEGRAM_PAGINATION_DESCRIPTIONS_PAGE_SIZE` |
| source ids | `TELEGRAM_PAGINATION_SOURCE_IDS_PAGE_SIZE` |

The regular anime-search picker keeps its **compact numbered grid** (not the
named-card list); the search-specific setting controls grid window length.
Wikipedia, source IDs, episode/people galleries, related anime and the
administrator ENV browser use the named section selectors. This setting controls
the **number of choices**, not Telegram's rich/plain text message budgets.
Details still use their normal previous/next controls. Changes to ENV settings
require restarting the bot.

## Per-user Wikipedia reading preference

The **/settings → Wikipedia · display mode** menu lets every Telegram user
switch between Clean and Full RAW Wikipedia article output. This is a
persistent **user preference**, not an environment variable and not
restricted to administrators. It is stored in the existing user-settings
database with automatic SQLite/PostgreSQL schema migration, without
overwriting the user's language preference. Changes apply on the next
Wikipedia page view (no bot restart). See [Wikipedia reader](wikipedia.md).

## Where to change what

| File | Settings |
|---|---|
| \`telegram.env\` | Bot API transport, users/admins, allowlist, callback/session caps, named selection page size, images, rich fallback methods and timeouts |
| \`sources.env\` | API endpoints and credentials, upstream timeout/caching limits, parallel loading of staff and portraits |
| \`runtime.env\` | Search defaults, search expansion and query budgets, provider health, HTTP connection/concurrency pool, Redis cache |
| \`rate-limits.env\` | Per-user request ceilings and source-/operation-specific API rate limits |
| \`storage.env\` | SQLite/PostgreSQL selection, database locations, user locale defaults and local user caches |
| \`mappings.env\` | Mapping catalog location and background release checks |
| \`timestamps.env\` | Skip/intro timestamp data and provider-specific settings |
| \`logging.env\` | Structured logs, redaction, log size, archive and retention |
| `lifecycle.env` | Fatal process-exit actions, restart limits, backoff and graceful shutdown |
| `api.env` | Optional authenticated REST v1 and GraphQL HTTP server |
| \`.env.example\` | Legacy single-file template (for compatibility) |

The sample directory is authoritative: copy **one** sample key at a time into
the matching active file. Do not copy the same key into both \`.env\` and
\`.env.d\`.

### Process lifecycle and automatic recovery

The supervised launcher reads settings from `.env.d/lifecycle.env` (or the
legacy `.env`) and applies them to **fatal bot process exits**, not ordinary
API errors, rejected Telegram updates, or handled provider failures.

```dotenv
BOT_ON_FAILURE=restart
BOT_ON_CLEAN_EXIT=stop
BOT_RESTART_MAX_ATTEMPTS=5
BOT_RESTART_DELAY_SECONDS=5
BOT_RESTART_MAX_DELAY_SECONDS=60
BOT_RESTART_RESET_AFTER_SECONDS=300
BOT_SHUTDOWN_TIMEOUT_SECONDS=10
```

- Actions are `stop` or `restart`; both actions default to `stop` for
  backward compatibility. A non-zero exit uses `BOT_ON_FAILURE`, while
  a successful exit (code 0) uses `BOT_ON_CLEAN_EXIT`.
- If an action is `restart`, the supervisor waits before starting a new
  child. Delays increase exponentially from the initial delay up to the
  configured cap. A maximum-attempt limit (0–100) prevents a crash loop.
  After the bot runs for `BOT_RESTART_RESET_AFTER_SECONDS`, consecutive
  restart attempts reset.
- An explicit operator stop (`scripts/project.py stop`, Ctrl+C, or
  SIGINT/SIGTERM/SIGHUP) **always stops** the child and supervisor even when
  `BOT_ON_CLEAN_EXIT=restart`. The child receives a graceful terminate
  request and is killed only if it exceeds `BOT_SHUTDOWN_TIMEOUT_SECONDS`.
- The policies apply to `./start.sh` tmux sessions and to the
  cross-platform `scripts/project.py run/start/restart` commands.
  Running `python -m bots.telegram.bot` directly bypasses the supervisor.
- Do not combine this policy with another unbounded `systemd` or Docker
  restart loop without understanding both layers. The provided
  `compose.yml` manages **only Jikan**, not the bot process.
- Legacy installations with an existing `.env.d` do not have their active
  config overwritten. Add `lifecycle.env` yourself when customizing;
  missing keys safely retain their built-in defaults.

### External REST and GraphQL API

The HTTP server is disabled by default. It can run independently of Telegram
(`anime-db-bot api`) or beside polling (`API_ENABLED=true`). Settings live
in `.env.d/api.env`:

```dotenv
API_ENABLED=false
API_HOST=127.0.0.1
API_PORT=8181
API_AUTH_MODE=apikey
API_READ_KEY=
API_ADMIN_KEY=
API_CORS_ORIGINS=
API_PUBLIC_URL=
API_TRUST_PROXY=false
API_REQUESTS_PER_MINUTE=90
API_MAX_CONCURRENCY=12
API_MAX_BODY_BYTES=4194304
API_TIMEOUT_SECONDS=30
API_GRAPHQL_MAX_DEPTH=8
API_GRAPHQL_MAX_FIELDS=60
API_GRAPHQL_INTROSPECTION=true
```

`API_AUTH_MODE` supports API keys, HTTP Basic, or an opt-in loopback-only
trusted mode. All mutating operations require admin scope and confirmation.
Missing/short credentials are rejected at server startup, not silently
treated as anonymous access. Use `API_BASIC_USERNAME` and
`API_BASIC_PASSWORD` for Basic mode. Cross-origin web requests require
an exact `API_CORS_ORIGINS` allowlist. See
[REST v1 and GraphQL API](api.md) for schema, example requests, rate limits
and Docker Compose instructions.

### Search providers

\`SEARCH_DEFAULT_PROVIDERS=shikimori,mal,anilist,kitsu\` specifies which
providers serve ordinary \`/anime text\` searches. Supported identifiers are
\`shikimori\`, \`mal\`, \`anilist\` and \`kitsu\`; at least one is required,
duplicates are rejected, and ordering is preserved.

\`SEARCH_EXPANSION_ENABLED=false\` disables additional cross-language searches,
reducing API requests and latency. Users can still manually select a provider
via the \`/anime\` source picker.

These two settings affect **default search**, not the registered metadata,
episodes, trailer or image-search providers. They are not global provider
disable switches.

### Cache and memory tuning

\`SEARCH_CACHE_MAX_BYTES\` limits **each** local page/result cache, and
\`ANISEARCH_CACHE_MAX_BYTES\` limits compressed AniSearch metadata.
\`RICH_MEDIA_RAM_CACHE_BYTES=0\` disables retaining downloaded images.
Values are budgets, not exact process RSS limits. Lower settings may cause
more upstream requests. Use \`CACHE_BACKEND=redis\` only after installing the
optional driver and making Redis available.

### Networks

\`HTTP_MAX_CONCURRENT_REQUESTS\` caps simultaneous upstream requests across
the shared bot HTTP pool. \`HTTP_MAX_CONNECTIONS\` and
\`HTTP_MAX_KEEPALIVE_CONNECTIONS\` tune each pooled client. The keepalive
value must not exceed the total connection limit. Other HTTP requests made
outside this managed pool can have their own resource limits.

### Rich media

\`RICH_MEDIA_ORDER\` accepts \`direct,url,ram,disk\`, in any order.
\`RICH_MEDIA_RAM_ENABLED\` and \`RICH_MEDIA_DISK_ENABLED\` can independently
disable local image upload strategies. Even with both disabled, rich tables and
buttons survive image errors. Reducing timeout values can shorten waits, but
may lead to more missing pictures. Disk cache stays inside the repository root
and is bounded.

### Access control

\`TELEGRAM_ALLOWED_USER_IDS=\` means all users may interact with the bot.
A nonempty list is restrictive, and administrators are **not automatically
exempt**. Notification text and the per-user/global limits for denied requests
can be configured separately. Avoid making the denied-request global limit very
large because it may turn spam into outgoing Telegram API traffic.

### Database and migration

Default mutable data storage remains SQLite. PostgreSQL is optional;
switching does not automatically migrate the existing users, bans, or quotas.
See [backends.md](backends.md) before changing the backend in production.

## Telegram administrator configuration UI

Open `/settings` in a private chat with the bot using an ID in
`ADMIN_TELEGRAM_USER_IDS`, then press **Bot configuration (ENV)**.
The equivalent direct commands are `/admin_config` and `/bot_config`.

The administrator UI is deliberately separate from regular user preferences.
It enumerates all keys from all shipped `.env.example.d/*.env` files rather
than maintaining a fragile hard-coded list. Available interactions:

- Browse groups and pages; select any key and see its documented group,
  active file or process environment source, safe display value, and brief
  reference comments.
- Search keys by partial name, e.g. `MAPPINGS`, `REDIS` or `API_`.
- Set a new single-line value, or reset a local override to defaults.
  A separate inline confirmation is required for every operation.
- Sensitive keys (including passwords, bot tokens, API keys and URLs) are
  always masked. When editing, the bot attempts to delete the administrator's
  message immediately; if a sensitive message cannot be deleted the edit
  is rejected so the operator can delete it manually.
- All edits use the same strict scalar and typed-settings validation as the
  local CLI and API. No arbitrary new ENV key or raw shell command is allowed.
- Settings are saved to the existing `.env.d/*.env` (or legacy `.env`)
  and **require a manual bot restart**; changing settings does not start a
  second bot or immediately restart the service. Process environment values
  override local files. Restrict access to the private Telegram chat.
- Non-administrators and group chats are denied at every menu, search,
  edit, reset and confirmation step. Confirmation state is short-lived and
  bound to the initiating administrator.

The editor covers documented BOT_TOKEN, databases, sources, search,
rate limits, logging, mapping updates, caching, API server, OIDC-like
future keys when documented, lifecycle, and all other shipped ENV options.
There are no separate Telegram UI restrictions for documented valid values,
but some settings can lock the administrator out after the next restart:
changing `ADMIN_TELEGRAM_USER_IDS`, `TELEGRAM_ALLOWED_USER_IDS`, API
credentials, or the bot token warrants particular caution.

## Safeguards

The newer concurrency/memory/timeout controls are range-validated and fail
startup on malformed values rather than silently accepting impossible limits.
Legacy settings retain their existing parsing semantics for compatibility.
Protocol-derived constants, external-service hard limits, Telegram callback
formats and data schema identifiers intentionally remain in source code rather
than user-configurable knobs.

Additional proxy and User-Agent profile settings use their existing profile
config files (rather than a separate environment variable for each entry).

The inspector also checks numeric/boolean values and suggests a likely spelling for unknown keys without displaying credentials. To locate one option, use `python -m scripts.config_report --explain TELEGRAM_MAX_CONCURRENT_UPDATES`.
