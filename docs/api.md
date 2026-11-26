# External REST v1 and GraphQL API

The API is an **optional, authenticated HTTP interface** to anime-db-bot's
existing application services. It runs in a standalone API-only process, or
alongside Telegram polling with the **same AppContext**. Neither mode uses
Telegram inline keyboard or callback handlers to serve HTTP requests.

The API follows the project's architectural roadmap: IDs from MAL, AniList,
Shikimori, Kitsu, AniDB and other sources remain **peer identities**. A matching
numeric value from two different databases does not imply the same anime.
Related works, characters, episodes, portraits and media are loaded only if a
client explicitly requests the corresponding capability. English is the
default; localized output is optional.

## Endpoints and versioning

- `GET /api/versions` — list supported major API versions
- `GET /api/v1/health` — unauthenticated minimal liveness
- `GET /api/v1/` — API discovery
- `GET /api/v1/openapi.json` — OpenAPI 3.1 contract (regular JSON document)
- `GET /api/v1/operations` — list operations allowed by the caller's key
- `POST /api/v1/actions/{operation}` — any registered console capability
- `POST /graphql` — GraphQL v1 queries and mutations
- `GET /api/v1/jobs` and `GET /api/v1/jobs/{id}` — admin maintenance job status

REST v1 provides convenient direct routes for searches, source-scoped
identities, lazy anime sections, recognition, source metrics, users,
moderation, mappings, configuration, logs and index builds. The
`/api/v1/actions/{operation}` route covers every operation in the
[operator console](operator-console.md) that makes sense as a stateless API.
`open 1`, `view` on an already selected anime, and `/cancel` are
session-local console actions; the equivalent API query is
`anime(query, section, episode, page, limit)`. Future incompatible
contracts will use `/api/v2` and will not silently change v1.

**GraphQL** has typed root queries/mutations, a schema discoverable via
standard introspection when enabled, and a `JSON` scalar for heterogeneous
provider data. This initial schema intentionally leaves room for richer
typed nested models without inventing unverified metadata.

## Direct source API: independent data by capability

Use the **source-native API** when you want records from MAL, AniList,
Shikimori, Kitsu, AniDB or AniSearch **without aggregation**.

By default, the response is the complete source adapter's **unmerged domain
record** for the requested capability. These records can normalize enums,
dates and names, but they are **never combined, deduplicated or averaged**
across sources. For original provider JSON/XML (with its native fields),
request the separate `wire` capability. Neither mode fetches the aggregate
anime card when you only ask for voices, staff, IDs or images.

The existing `/api/v1/anime`, `/api/v1/search` and ordinary GraphQL
`anime`/`search` queries remain available as **aggregated** alternatives.

### One endpoint per capability

| Data | REST | GraphQL |
| --- | --- | --- |
| Capabilities/source list | `GET /api/v1/raw/capabilities` | `sourceCatalog` |
| Search (individual results) | `GET /api/v1/raw/search` | `rawSearch` |
| Source-scoped ID only | `GET /api/v1/raw/id/{source}/{id}` | `rawId` |
| Metadata (per-provider domain model) | `GET /api/v1/raw/metadata` | `rawMetadata` |
| Original source JSON/XML | `GET /api/v1/raw/wire` | `rawWire` |
| Seiyuu / voice actors | `GET /api/v1/raw/seiyuu` | `rawSeiyuu`, `rawVoices` |
| Staff / production personnel | `GET /api/v1/raw/staff` | `rawStaff` |
| Characters | `GET /api/v1/raw/characters` | `rawCharacters` |
| Episodes / individual episode | `GET /api/v1/raw/episodes`, `/episode` | `rawEpisodes`, `rawEpisode` |
| Skip timestamps | `GET /api/v1/raw/timestamps` | `rawTimestamps` |
| OP / ED themes | `GET /api/v1/raw/themes` | `rawThemes` |
| Trailers / screenshots | `GET /api/v1/raw/trailers`, `/screenshots` | `rawTrailers`, `rawScreenshots` |
| Statistics / descriptions | `GET /api/v1/raw/statistics`, `/description` | `rawStatistics`, `rawDescription` |
| Related works / recommendations | `GET /api/v1/raw/relations`, `/recommendations` | `rawRelations`, `rawRecommendations` |
| Recognition by photo | `POST /api/v1/raw/image` | `rawImage` |
| Chosen sections / all sections | `POST /api/v1/raw/query` | `rawBundle`, `rawAll` |

**One source only:**

```sh
curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/mal/5114/voices"

curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/mal/5114/metadata"

# Original upstream JSON selected by the provider's metadata query:
curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/mal/5114/wire"

# Only a source ID; no full metadata request:
curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/id/mal/5114"
```

**All relevant sources, independently:**

```sh
curl -G -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/voices" \
  --data-urlencode "source=all" \
  --data-urlencode "ids=mal:5114,anilist:9253"

curl -G -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/search" \
  --data-urlencode "source=all" \
  --data-urlencode "query=Frieren"
```

In the second example, **MAL and AniList are independently searched**;
results are not re-ranked together and each provider's pagination is retained.

Multiple IDs are accepted as `ids=mal:5114,anilist:9253` in GET URLs or
as an object in a JSON body. The `source=all` selector contacts every
**enabled provider for that capability**, and each has its own result.
**Never send `id=5114&source=all`:** the API refuses to interpret one bare
number as an ID in every database. To discover additional ID edges, set
`resolve=true` explicitly; expansion uses confirmed **one-hop mapping**
relationships, never title/number guessing. A missing ID is reported as
`missing_id`, and an unsupported provider capability as `unsupported`.

Example simplified result:

```json
{
  "capability": "voices",
  "aggregated": false,
  "representation": "source_adapter_record",
  "identity": {"mal": "5114", "anilist": "9253"},
  "sources": {
    "mal": {"status": "ok", "data": {"level": "manifest", "people": []}},
    "anilist": {"status": "ok", "data": {"level": "manifest", "people": []}},
    "kitsu": {"status": "missing_id", "data": null}
  }
}
```

The actual REST response encloses this object under `data` with a
`meta.request_id`. Individual source failures have `status: "error"`
and a sanitized `error_type`, and do not suppress successful peers.

### Lazy/large capabilities

A normal metadata or ID query never implicitly fetches cast, voice actors,
episodes, images, skip segments, relations or reviews. The explicit
`wire` request returns the original provider response selected by its
existing query: MAL, AniList, Shikimori and Kitsu return JSON, AniDB returns
XML, and AniSearch returns its cached full record. Upstream query field
selection remains provider-defined; wire mode cannot request arbitrary
additional upstream fields or bypass upstream quotas. For `staff` and
`voices`, `detail=manifest` (default) returns lightweight source-native
person IDs/names/roles without requesting all portraits. Explicitly use
`detail=full` to fetch richer people details where supported.

Use `page` for searches/episode pages, and `episode` to request one
episode, skip timeline or theme set. Backends such as Jikan are listed under
their backend key (e.g. `jikan`) while their **anime identity** remains
`mal`. For example:

```sh
curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/raw/jikan/5114/episodes?page=1"
```

**One combined request only when explicitly chosen:**

```sh
curl -X POST "http://127.0.0.1:8181/api/v1/raw/query" \
  -H "X-API-Key: $API_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "capability": "bundle",
    "source": "all",
    "ids": {"mal": "5114", "anilist": "9253"},
    "capabilities": ["metadata", "voices", "episodes", "relations"]
  }'
```

Set `capability: "all"` to request all supported anime-detail capabilities
explicitly; doing so can be expensive and is still subject to API timeouts and
provider quotas. Each capability in the response retains its own
source-separated results, and no aggregated anime model is constructed.
The convenience query does not include image recognition (requires image
bytes) or search (requires title text).

### Separate GraphQL queries

```graphql
query NativeSources {
  sourceCatalog
  rawSearch(query: "Frieren", source: "all")
  rawId(source: "mal", id: "5114")
  rawMetadata(ids: {mal: "5114", anilist: "9253"})
  rawWire(source: "mal", id: "5114")
  rawSeiyuu(source: "mal", id: "5114")
  rawStaff(source: "mal", id: "5114")
  rawCharacters(source: "mal", id: "5114")
}

query EpisodesAndTimings {
  rawEpisodes(source: "jikan", id: "5114", page: 1)
  rawTimestamps(ids: {mal: "5114"}, episode: 1)
}
```

These are standalone root fields with no dependency on an earlier
`anime` query or a session-level `open` action. For a custom capability
set use `rawBundle(capabilities: ["metadata","voices"], ids: {...})`.
GraphQL query depth/field/root budgets and existing upstream rate limits
still apply.

## Launching the API

By default, **no HTTP listener exists**.

### Native binary / source checkout (API only)

```sh
# From a Windows/macOS/Linux native release:
./anime-db-bot api --api-host 127.0.0.1 --api-port 8181

# Windows:
anime-db-bot.exe api --api-host 127.0.0.1 --api-port 8181

# Python checkout:
python -m scripts.release_entry api
```

Configure `API_ADMIN_KEY` and/or `API_READ_KEY` in `.env.d/api.env`
or the process environment before starting. An API-only service **does not
require BOT_TOKEN** and does not run Telegram polling.

### API within the Telegram bot

```dotenv
BOT_TOKEN=your_telegram_token
API_ENABLED=true
API_HOST=127.0.0.1
API_PORT=8181
API_AUTH_MODE=apikey
API_READ_KEY=generate_an_independent_random_read_secret_here
API_ADMIN_KEY=generate_a_distinct_random_admin_secret_here
```

Then run `./anime-db-bot run` as usual. HTTP and Telegram share AppContext,
source clients and cache/rate limiters. They shut down together.

### API-only Docker / GHCR

```sh
# API_AUTH_MODE=apikey and API_ADMIN_KEY are required.
# Publish the container port on the Docker host loopback only.
docker run -d --name anime-db-api --restart unless-stopped \
  -e ANIME_DB_BOT_RUN_MODE=api \
  -e API_ENABLED=true -e API_HOST=0.0.0.0 \
  -e API_ADMIN_KEY="$API_ADMIN_KEY" \
  -p 127.0.0.1:8181:8181 \
  -v anime-db-bot-data:/data \
  ghcr.io/anime-db-project/anime-db-bot:latest api

docker logs -f anime-db-api
```

Alternatively, place the secret in a local untracked `.env` and run:

```sh
docker compose -f compose.api.yml up -d
docker compose -f compose.api.yml logs -f
```

The API-only Compose service and the Telegram bot Compose service can mount
the same persistent data volume, but use separate processes. PostgreSQL is
recommended for heavier concurrent user-state writes; mutable mapping files
retain their existing SQLite synchronization model. Docker and reverse-proxy
ports are not opened automatically by an ordinary Telegram-bot installation.

## Authentication

`API_AUTH_MODE` accepts:

- `apikey` (default): `X-API-Key: <secret>` or `Authorization: Bearer <secret>`.
  Distinct `API_READ_KEY` and `API_ADMIN_KEY` grant different scopes.
  Configure at least one secret, each 24 or more characters.
- `basic`: HTTPS-protected HTTP Basic credentials configured by
  `API_BASIC_USERNAME` and `API_BASIC_PASSWORD`. Basic credentials grant
  administrator scope; use HTTPS outside localhost.
- `local`: explicit opt-in trusted-local mode. Only permitted when
  `API_HOST` is `127.0.0.1`, `::1` or `localhost`, and the immediate peer
  is also loopback. **Do not use local mode behind a reverse proxy**; a
  reverse proxy connecting from localhost would be treated as the caller.

Anonymous access is restricted to `/api/versions` and `/api/v1/health`.
Read keys can query anime, search, status, sources and metrics.
Users, bans, logs, mapping overrides and all configuration mutations require
the administrator scope. Any operation that changes durable state also
requires explicit `confirm: true`; without it, the API returns HTTP 409.

Even administrators should avoid sending credentials as URL parameters.
Never store API secrets in GitHub workflows, client-side JavaScript, or public
Mini App bundles. A future Telegram Mini App must validate signed init data
server-side before binding any Telegram identity to an HTTP principal;
**sending a user ID alone is not authorization**.

## Example REST requests

Use your locally configured key and endpoint:

```sh
curl -H "Authorization: Bearer $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/search?query=Frieren&limit=5"

curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/anime/mal/5114/related"

curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/anime?query=mal:5114&section=timestamps&episode=1"

curl -H "X-API-Key: $API_READ_KEY" \
  "http://127.0.0.1:8181/api/v1/source-health"

curl -H "X-API-Key: $API_ADMIN_KEY" \
  "http://127.0.0.1:8181/api/v1/users/123456"
```

The response format is:

```json
{
  "data": {
    "title": "..."
  },
  "meta": {
    "version": "v1",
    "request_id": "opaque-request-id"
  }
}
```

Use the registered command gateway to access the remaining console functions:

```sh
curl -X POST "http://127.0.0.1:8181/api/v1/actions/mapping.set" \
  -H "X-API-Key: $API_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action":"accept","source":"mal","source_id":"1","target":"anilist","target_id":"2","reason":"manually verified","confirm":true}'
```

The base64 image recognition endpoint accepts bytes and an explicit image
MIME type, rather than an arbitrary server-side filesystem path or untrusted
URL. Example JSON payload:

```json
{
  "content_base64": "<base64-encoded JPEG, PNG or WebP>",
  "content_type": "image/jpeg",
  "provider": "trace_moe,whatanimeisthis",
  "limit": 10
}
```

Post it to `/api/v1/images/recognize`. Images are checked against both
byte-size and pixel limits.

## Example GraphQL requests

GraphQL requests use **POST** with a JSON body:

```graphql
query AnimeInformation {
  search(query: "Frieren", limit: 5) {
    count
    items { index title year externalIds }
  }
  anime(query: "mal:5114", section: "related") {
    query
    section
    data
  }
}
```

```graphql
mutation UpdateUser {
  setLanguage(userId: 123456, locale: "ru", confirm: true)
}
```

Example HTTP call:

```sh
curl -X POST "http://127.0.0.1:8181/graphql" \
  -H "Authorization: Bearer $API_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ apiVersion sources metrics }"}'
```

Available fields include `search`, `lookup`, `anime`,
`recognizeImage`, `related` and other card sections through `anime`,
`user`, `users`, `language`, `status`, `sourceHealth`,
`mappingStatus`, `mappingOverrides`, `mappingHistory`,
`configGet`, `configList`, `logs`, `jobs` and `job`.
Mutations include `banUser`, `unbanUser`, `setLanguage`,
`setMapping`, `deleteMapping`, `updateMappings`, `setConfig`,
`resetConfig`, `buildImdbIndex`, and `buildBahaIndex`.

The generic GraphQL `operation(name:..., input:...)` field mirrors the
REST operation registry. It rejects mutations in a `query` or reading in a
`mutation`, enforcing GraphQL operation semantics.

## Background maintenance jobs

Replacing a mapping catalog or building a local IMDb/Baha index may take
minutes. These operations return **HTTP 202** and a job descriptor, rather
than leaving a request connected or restarting the bot:

```sh
curl -X POST "http://127.0.0.1:8181/api/v1/mappings/update" \
  -H "X-API-Key: $API_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"confirm":true}'

curl -H "X-API-Key: $API_ADMIN_KEY" \
  "http://127.0.0.1:8181/api/v1/jobs/<job-id>"
```

Each job moves through queued/running/succeeded/failed states. At most one
heavy maintenance operation runs concurrently by default. The server retains
a bounded in-memory status history (100 jobs); jobs/history do **not** survive
a process restart. The underlying index builders use the same SQLite and
filesystem safety rules as the console.

## Network safety and observability

- Disabled by default and bound to loopback by default.
- `API_PUBLIC_URL` sets the externally advertised base URL in OpenAPI.
- `API_TRUST_PROXY=true` may read forwarded host/scheme solely for OpenAPI
  presentation **when the direct peer is loopback**. Forwarded headers never
  change authentication, client identity or rate-limit buckets.
- `API_CORS_ORIGINS` is a comma-separated exact allowlist, never an implicit
  wildcard. Empty list disables browser cross-origin access.
- `API_REQUESTS_PER_MINUTE` limits both authenticated and invalid requests
  using bounded per-peer counters.
- `API_MAX_CONCURRENCY`, `API_TIMEOUT_SECONDS`, and `API_MAX_BODY_BYTES`
  bound resource use.
- `API_GRAPHQL_MAX_DEPTH`, `API_GRAPHQL_MAX_FIELDS`, and
  `API_GRAPHQL_INTROSPECTION` control GraphQL query budgets. Field and depth
  validation happens before running any data resolver.
- Logs record a generated correlation ID, route template, HTTP status and
  duration. They exclude raw query strings, authentication headers, API keys,
  user IDs, anime IDs and raw upstream exception messages.
- TLS termination should be handled by a trusted reverse proxy. Do not
  expose an unencrypted Basic or Bearer-authenticated port to the Internet.
- `/api/v1/health` is intentionally minimal, unauthenticated and suitable
  for local Docker health checks; it contains no user or credential state.
- OAuth/JWT, persistent HTTP sessions, Telegram Mini App authentication,
  subscriptions, playback gateway and additional API versions remain
  future expansions instead of being misleadingly represented as completed.

All names, comments, documentation and API error strings use English as
the project's canonical default.
