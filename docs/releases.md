# Binary and Docker releases

English is the canonical project language. All release assets are built on
native GitHub Actions runners; no build or test is performed on the production
server.

## Supported release artifacts

| Archive / image | Architecture | Launch |
|---|---|---|
| `anime-db-bot-vX.Y.Z-windows-x64.zip` | Windows x86-64 | Double-click `anime-db-bot.exe` / `Run.cmd`, or PowerShell / CMD |
| `anime-db-bot-vX.Y.Z-linux-x64.tar.gz` | Linux x86-64 | Executable / `Run.sh`, or terminal |
| `anime-db-bot-vX.Y.Z-linux-arm64.tar.gz` | Linux AArch64 | Executable / `Run.sh`, or terminal |
| `anime-db-bot-vX.Y.Z-macos-x64.tar.gz` | macOS Intel | Double-click `Run.command`, or terminal |
| `anime-db-bot-vX.Y.Z-macos-arm64.tar.gz` | macOS Apple Silicon | Double-click `Run.command`, or terminal |
| `ghcr.io/anime-db-project/anime-db-bot:X.Y.Z` | Docker Linux amd64/arm64 | Docker / Compose |

Starting with **v0.3.1**, each release includes both `SHA256SUMS.txt`
and `manifest.json`. The checksum file uses standard GNU sha256sum format:
one hash and filename per archive. The JSON manifest includes schema version,
repository, release tag, full Git commit SHA, generated time, and each
archive's filename, platform, architecture, byte size and SHA-256 hash;
it also records the checksum file's digest. Docker image tags/platforms are
listed as identifiers, not as a substitute for immutable registry digests.

The release publishing job verifies all five platform archives, generates
both files from their **actual bytes**, and verifies consistency immediately
before publication. A changed archive or mismatched checksum fails
verification rather than publishing an invalid release.

Verify an archive on Linux or macOS with:

```sh
sha256sum --check SHA256SUMS.txt
```

Run the command in a directory containing all five archives, or compare the
selected archive's hash manually. On Windows use `Get-FileHash` in PowerShell
and compare with the manifest. Verify downloaded archives before running. The executable packages use PyInstaller **onedir** for faster startup
and avoid unpacking a Python runtime on each invocation. Keep the included
`_internal` directory beside the executable; it contains libraries and
bundled configuration/localization templates.

The macOS binaries are not Apple Developer ID signed or notarized.
macOS Gatekeeper may block the first launch until the user approves it;
there is no automatic bypass. For Linux, executable and script permissions
are preserved in the TAR archive. Some file managers may prompt before
executing a downloaded file.

## External API

**v0.3.0 and later** add REST `/api/v1/...` and GraphQL `/graphql` to
Windows/Linux/macOS binaries and GHCR. HTTP listeners are off by default;
start explicitly with `anime-db-bot api` or enable `API_ENABLED=true`
alongside Telegram polling. API-only operation does not require `BOT_TOKEN`.
Keys, Basic/local policies, exact CORS origins, public URL and limits are
configured in `.env.d/api.env`.

Docker users can deploy a dedicated API-only service using
`docker compose -f compose.api.yml up -d`; the host port binds to localhost
until intentionally placed behind a TLS reverse proxy. REST operations and
GraphQL both call the same shared application services; admin mutations need
explicit confirmation. Details: [External API](api.md).

## Telegram administrator ENV settings

The private `/settings` menu now includes **Bot configuration (ENV)** for
IDs listed in `ADMIN_TELEGRAM_USER_IDS`. Use `/admin_config` directly to
open the browser, search all documented options, inspect masked values and
safely edit/reset settings with confirmation. Incoming messages containing
secret values are deleted where Telegram permits. Every ENV change still
requires an explicit bot restart; no automated server build/test is needed.
See [configuration reference](configuration.md).

## Operator console

**Version 0.2.0 and later** also include a full local operator console. Use
`anime-db-bot console` (interactive) or `anime-db-bot cli COMMAND`
(one-shot; supports JSON output). Linux/macOS users should prefix the executable
name with `./`. When a foreground binary runs on a terminal, a console
prompt is shown alongside Telegram polling; disable with `run --no-console`.

For a running Docker container, use:

```sh
docker exec -it anime-db-bot python -m scripts.release_entry console
docker exec anime-db-bot python -m scripts.release_entry cli --json status
docker exec anime-db-bot python -m scripts.release_entry cli users
```

The console supports anime search, local image recognition, details and lazy
card sections, user locales and bans, mapping overrides, configuration editing,
indexes, logs and real-time worker metrics. No second Telegram poller is
created. Full reference: [Operator console](operator-console.md).

## Start from a native archive

Extract the archive to a writable directory. Run the executable, or use its
clickable launcher. The first launch creates `.env.d`, `.proxies.d` and
`.user-agents.d` templates. Insert your Telegram token into
`.env.d/telegram.env` (`BOT_TOKEN`) or pass it via an environment variable.

The application uses English for the startup interface and documentation
and supports optional Telegram UI localizations.

### CLI commands

On Linux or macOS, prefix the commands with `./anime-db-bot`.
On Windows use `anime-db-bot.exe`.

```sh
./anime-db-bot --version
./anime-db-bot --help
./anime-db-bot init
./anime-db-bot check
./anime-db-bot doctor
./anime-db-bot run
./anime-db-bot start
./anime-db-bot status
./anime-db-bot stop
./anime-db-bot restart
```

The **default** when no argument is specified is `run`, a foreground console
process, which is suitable for a double-click. The `start` command launches
a persistent detached supervisor and writes `logs/launcher.log`. Use
`stop` to request a graceful shutdown or `restart` to cycle the
background instance. A single writable home is used for all of
`config/data/logs`; two processes started with the same home will not
create duplicate pollers.

Supported overrides:

```sh
./anime-db-bot run --home /srv/anime-db-bot
./anime-db-bot start --env-file /etc/anime-db-bot.env
./anime-db-bot run --log-level DEBUG --max-updates 12
./anime-db-bot run --on-failure restart --restart-limit 5 --restart-delay 4
./anime-db-bot run --no-mappings-update
```

Avoid passing `BOT_TOKEN` on the command line, where arguments may appear in
process listings. Real environment variables take precedence over local
files; explicit documented CLI switches take highest precedence.

Binary running mode uses the existing lifecycle supervisor: fatal
process exits can stop or restart with capped exponential backoff.
Operator `stop` always terminates the supervisor and worker.

### Data and log paths

The default home for a frozen executable is the executable's directory.
Change it using `--home DIRECTORY` or `ANIME_DB_BOT_HOME` if the
installation directory is read-only. Inside the home:

- `.env.d/*.env` — credentials and settings; never commit or publish
- `.proxies.d/` and `.user-agents.d/` — optional network profiles
- `data/` — local SQLite databases, mappings, caches and runtime PID files
- `logs/` — launcher and application logs

## Docker / GHCR

The Docker image includes the same features and optional Redis/PostgreSQL
clients. It uses Python 3.12 and a non-root account. Jikan runs in a separate container. Use `docker compose -f compose.bot.yml up -d`
or `docker compose -f compose.api.yml up -d` for the private network
(`http://jikan:8091`). For native binaries or source installs, use
`docker compose up -d --no-build jikan` (loopback port 8091).
The companion image is `ghcr.io/anime-db-project/anime-db-bot-jikan:latest`;
set `ANIME_DB_BOT_TAG` for a matching release or `JIKAN_IMAGE` to override it.
Native binary users without a source checkout can run:\n\n```sh\ndocker run -d --name anime-db-bot-Jikan --restart unless-stopped \\\n  -p 127.0.0.1:8091:8091 ghcr.io/anime-db-project/anime-db-bot-jikan:latest\n```\n\nManual local build: `./scripts/build-jikan.sh [--yes] [JIKAN_VERSION]`.
The old container remains running during the no-cache build; successful
replacement removes it. No scheduled Jikan builds or updates are installed.
For bot-only Docker deployment, start just the bot service or use the
standalone command below.

```sh
docker pull ghcr.io/anime-db-project/anime-db-bot:latest

docker run -d --name anime-db-bot \
  --restart unless-stopped \
  -e BOT_TOKEN="$BOT_TOKEN" \
  -v anime-db-bot-data:/data \
  ghcr.io/anime-db-project/anime-db-bot:latest

docker logs -f anime-db-bot
docker exec anime-db-bot python -m scripts.release_entry status
docker stop anime-db-bot
```

`/data` contains all mutable configuration, state, mapping catalogs and logs;
the application code in `/app` is immutable. Mount a Docker named volume or
a host directory writable by UID/GID `10001`. Environment variables
(`BOT_TOKEN`, `DATABASE_BACKEND`, `DATABASE_URL`,
`CACHE_BACKEND`, `REDIS_URL`, etc.) can be supplied with `-e` or
Docker's `--env-file`. For an additional dot-env passed to the program,
use `--env-file /data/settings.env` *after* the image.

For Compose, supply `BOT_TOKEN` through the shell or a local untracked
`.env` file, then run:

```sh
docker compose -f compose.bot.yml up -d
docker compose -f compose.bot.yml logs -f
docker compose -f compose.bot.yml down
```

The container defaults to `run`, because Docker handles detaching with
`-d`. An additional child-process supervisor remains responsible for
fatal-error policies. Avoid combining unconditional container restarts and
unbounded internal restarts.

Docker control arguments:

```sh
docker run --rm ghcr.io/anime-db-project/anime-db-bot:latest --help
docker run --rm ghcr.io/anime-db-project/anime-db-bot:latest doctor
docker run --rm -v anime-db-bot-data:/data ghcr.io/anime-db-project/anime-db-bot:latest check
```

## Publishing

`.github/workflows/release.yml` builds five native archives, publishes a
multiarch GHCR image, validates basic execution on each native platform,
and publishes a GitHub Release only after all jobs succeed. Source-only
releases are never generated by this workflow.

The first bootstrap release is `v0.1.0`, created automatically only
if that release does not already exist. Subsequent releases are produced
by pushing a `vMAJOR.MINOR.PATCH` tag, or by manually running the workflow
with an explicit version. CI and release builds run in GitHub Actions,
not on the server. The workflow performs no production deployment.
