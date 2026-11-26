# Operator console and command-line interface

The same console works in **Windows, Linux and macOS standalone binaries**, in
source checkouts and **inside the Docker image**. English is the default
language. The console is a local operator interface, not a second Telegram bot
or remote HTTP endpoint.

All search and anime-card operations call the same application services as the
Telegram UI. Administrative changes use the same durable users, bans and
mapping audit databases. **No second Telegram polling instance is started.**

## Launch the console

### Linux/macOS binary

```sh
./anime-db-bot console
./anime-db-bot cli help
./anime-db-bot cli search Frieren
./anime-db-bot cli --json anime mal:5114 --section related
```

### Windows binary

```powershell
.\anime-db-bot.exe console
.\anime-db-bot.exe cli help
.\anime-db-bot.exe cli search Frieren
.\anime-db-bot.exe cli anime mal:5114 --section episodes
```

### Docker: use docker exec while Telegram polling is running

```sh
docker exec -it anime-db-bot python -m scripts.release_entry console
docker exec anime-db-bot python -m scripts.release_entry cli status
docker exec anime-db-bot python -m scripts.release_entry cli users
docker exec anime-db-bot python -m scripts.release_entry cli --json metrics
docker exec anime-db-bot python -m scripts.release_entry cli mapping status
```

For a one-time standalone console process, mount the same writable data volume:

```sh
docker run --rm -it -v anime-db-bot-data:/data \
  ghcr.io/anime-db-project/anime-db-bot:latest console
```

Docker image entrypoint forwards all arguments to the portable CLI.
An interactive `console` requires an attached TTY; `cli` is one-shot and
suitable for scripts/cron. The default Docker command stays `run`.

### Direct click / foreground bot process

When a terminal is attached, `anime-db-bot run` (the default when launching
the binary directly) also opens a **live local operator prompt** alongside the
bot's Telegram polling. The prompt runs separately from the worker; typing
`exit` closes the prompt but does not stop the bot. `Ctrl+C` or a supervisor
stop command ends the bot according to the lifecycle policy. Use
`run --no-console` to disable the prompt. Background `start` and detached
Docker `run` do not try to read from an unattended terminal.

## Discover anime

Start an interactive session:

```text
anime-db-bot> search Frieren
anime-db-bot> open 1
anime-db-bot> view main
anime-db-bot> view titles
anime-db-bot> view ratings
anime-db-bot> view description
anime-db-bot> view related
anime-db-bot> view recommendations
anime-db-bot> view staff
anime-db-bot> view voices
anime-db-bot> view characters
anime-db-bot> view episodes --source jikan --page 1
anime-db-bot> view episode --source jikan --episode 1
anime-db-bot> view timestamps --episode 1
anime-db-bot> view themes --episode 1
anime-db-bot> view screenshots
anime-db-bot> view trailers
anime-db-bot> view statistics
anime-db-bot> view links
```

The selected anime is retained between `view` commands. Search uses source
identity matching and the same relevance controls as Telegram. If a provider
does not support a capability, the console reports the absence of data
instead of guessing. Expensive sections load **only when requested**.

One-shot commands (no interactive session state):

```sh
./anime-db-bot cli search Sword Art Online --limit 10
./anime-db-bot cli search Frieren --source mal,anilist
./anime-db-bot cli lookup anilist:154587
./anime-db-bot cli anime mal:5114 --section main
./anime-db-bot cli anime mal:5114 --section recommendations --page 1
./anime-db-bot cli anime mal:5114 --section episode --episode 1
./anime-db-bot cli --json anime mal:5114 --section timestamps --episode 1
./anime-db-bot cli image /path/to/anime-frame.png
./anime-db-bot cli image https://example.com/anime-frame.jpg
./anime-db-bot cli image-providers
```

For images in Docker, mount the local file under `/data` and pass that
container path. Images are checked against the configured size limit.

The `--json` flag prints machine-readable output without embedding any
Telegram markup or callback IDs. `--locale en|ru|ja|de|es|fr|it` chooses
the console presentation locale, separate from each Telegram user's saved
preference. `--source`, `--limit`, `--page` and `--episode` control
search/result pagination and lazy detail loading.

## Telegram-style command aliases

The interactive console accepts familiar commands directly:

```text
anime-db-bot> /start
anime-db-bot> /ping
anime-db-bot> /anime Frieren
anime-db-bot> /anime mal:5114
anime-db-bot> /users
anime-db-bot> /status
anime-db-bot> /source_health
anime-db-bot> /ban 123456 600 spam
anime-db-bot> /unban 123456
anime-db-bot> /mapping_overrides
anime-db-bot> /mapping_history 20
anime-db-bot> /mapping_set accept mal 1 anilist 2 verified manually
anime-db-bot> /mapping_confirm
anime-db-bot> /mapping_delete mal 1 anilist 2 invalid association
anime-db-bot> /mapping_cancel
anime-db-bot> /settings
anime-db-bot> /cancel
```

`/anime` in the console returns a candidate list; `open 1` selects a
result. The local console cannot display a Telegram inline keyboard, receive
a Telegram photo ID or send Telegram chat messages: those are Telegram
transport capabilities, not standalone anime functions.

Staged `/mapping_set` and `/mapping_delete` operations are **not written**
until `/mapping_confirm`. `/mapping_cancel` drops the pending transaction.
For one-shot automation use explicit `mapping set/delete ... --reason ... --yes`
instead; a pending confirmation cannot survive separate CLI processes.

## Users, moderation and configuration

```text
anime-db-bot> users
anime-db-bot> user 123456789
anime-db-bot> language 123456789
anime-db-bot> language 123456789 ru
anime-db-bot> ban 123456789 --seconds 3600 --reason spam
anime-db-bot> unban 123456789
anime-db-bot> mapping status
anime-db-bot> mapping list
anime-db-bot> mapping history --limit 25
anime-db-bot> mapping update --force
anime-db-bot> mapping set accept mal 1 anilist 2 --reason verified
anime-db-bot> mapping delete mal 1 anilist 2 --reason incorrect
anime-db-bot> config list
anime-db-bot> config get BOT_TOKEN
anime-db-bot> config explain HTTP_MAX_CONNECTIONS
anime-db-bot> config check
anime-db-bot> config set LOG_LEVEL DEBUG
anime-db-bot> config set BOT_TOKEN --prompt
anime-db-bot> config reset LOG_LEVEL
```

Administrative commands executed from the local console require access to the
host user or container, not a Telegram admin user ID. Never expose the host
Docker socket, interactive shell or writable `/data` volume to untrusted users.
Mutating one-shot commands require `--yes`, or a confirmation in an
interactive terminal. Configuration values are range-validated before saving;
secrets are masked in `config get`. Avoid typing credentials directly in
terminal arguments, where they can be saved in history or process listings;
use `--prompt` or pipe through `--stdin` instead.

Settings are stored under the active `ANIME_DB_BOT_HOME` (default `/data`
for Docker), not inside the immutable binary. **A bot restart is required**
for configuration edits to take effect. A real process environment variable
overrides the same key in local `.env.d`.

## Maintenance, diagnostics, and local indexes

```text
anime-db-bot> status
anime-db-bot> sources
anime-db-bot> source-health
anime-db-bot> metrics
anime-db-bot> logs --file launcher --lines 60
anime-db-bot> logs --file errors --lines 100
anime-db-bot> index imdb --source-file /data/title.ratings.tsv.gz
anime-db-bot> index baha --concurrency 8
```

The `index` commands reuse the project's original anime-filtered IMDb and
Baha index builders. They are explicitly requested operations and can involve
significant CPU, disk and network use; **neither index is automatically built
when starting a binary or container**.

While polling runs, the worker periodically exports a private, secret-free
status snapshot to `data/runtime/operator-status.json`. The operator
console reads it, so `status`, `source-health`, `sources` and `metrics`
can report **real worker metrics** across processes. Old snapshots are
rejected. If polling is stopped, a fresh snapshot is unavailable and the
console clearly reports that fallback metrics only describe the console
process. No credentials or upstream exception bodies are exported.

## CLI return values and safety

- Exit code `0`: successful command.
- Exit code `2`: invalid input, missing confirmation, or another
  recoverable user-level error.
- Other errors: upstream data failures, missing files or runtime failures.

Search, reading and diagnostics never start another Telegram polling client.
The REPL is not a remotely accessible API and intentionally does not
pretend to reproduce transport-only UI interactions such as callback buttons.
