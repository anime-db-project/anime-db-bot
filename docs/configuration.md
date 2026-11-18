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

## Where to change what

| File | Settings |
|---|---|
| \`telegram.env\` | Bot API transport, users/admins, allowlist, callback/session caps, images, rich fallback methods and timeouts |
| \`sources.env\` | API endpoints and credentials, upstream timeout/caching limits, parallel loading of staff and portraits |
| \`runtime.env\` | Search defaults, search expansion and query budgets, provider health, HTTP connection/concurrency pool, Redis cache |
| \`rate-limits.env\` | Per-user request ceilings and source-/operation-specific API rate limits |
| \`storage.env\` | SQLite/PostgreSQL selection, database locations, user locale defaults and local user caches |
| \`mappings.env\` | Mapping catalog location and background release checks |
| \`timestamps.env\` | Skip/intro timestamp data and provider-specific settings |
| \`logging.env\` | Structured logs, redaction, log size, archive and retention |
| \`.env.example\` | Legacy single-file template (for compatibility) |

The sample directory is authoritative: copy **one** sample key at a time into
the matching active file. Do not copy the same key into both \`.env\` and
\`.env.d\`.

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

## Safeguards

The newer concurrency/memory/timeout controls are range-validated and fail
startup on malformed values rather than silently accepting impossible limits.
Legacy settings retain their existing parsing semantics for compatibility.
Protocol-derived constants, external-service hard limits, Telegram callback
formats and data schema identifiers intentionally remain in source code rather
than user-configurable knobs.

Additional proxy and User-Agent profile settings use their existing profile
config files (rather than a separate environment variable for each entry).
