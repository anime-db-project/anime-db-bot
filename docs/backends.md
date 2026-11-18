# Optional PostgreSQL and Redis backends

The bot runs without either service. The defaults are **SQLite for durable
mutable records** and a **bounded in-process cache** for search results.

To enable optional drivers, install:
```sh
python -m pip install -r requirements-backends.txt
```

Configure these in `.env.d/storage.env`:

```dotenv
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql://bot:password@127.0.0.1:5432/anime_db_bot
DATABASE_POOL_SIZE=5
```

In `.env.d/runtime.env`:

```dotenv
CACHE_BACKEND=redis
REDIS_URL=redis://:password@127.0.0.1:6379/0
REDIS_KEY_PREFIX=anime-db-bot
REDIS_TIMEOUT_SECONDS=0.25
```

## PostgreSQL

Only **user preferences, known users, ban records and image-search quota
usage** are stored in PostgreSQL. The mapping catalog (`mappings.db`), source
databases, IMDb files and application-generated SQLite data are not automatically
converted. PostgreSQL tables are initialized automatically. Credentials are
never written to the repo.

Switching to PostgreSQL **does not automatically migrate existing SQLite
records**. Back up the SQLite databases before changing the backend. Migrate
existing records explicitly before switching a populated deployment, or they
will not appear in PostgreSQL. Database write failures fail closed: the bot
does not silently write mutable data to a different local database.

Use a non-superuser PostgreSQL role with privileges only on its application
schema, enable TLS when connecting to a remote host, and secure the DSN as
you would a password.

## Redis

Redis stores only *successful* search pages and consolidated search result
sets using JSON, with the same configured TTL as local search caches.
Every instance uses a namespace including the configured provider identities.
Redis errors do **not** block search: queries proceed using the process-local
cache and upstream providers.

Redis is **not** an authoritative database. Restart/eviction may invalidate
search cache entries without losing user data. Keep Redis private, require
authentication when exposed beyond loopback, and prefer `rediss://` for
remote connections.

Local Telegram callback sessions, poster RAM/disk caches, live source
health and rate-limit buckets remain process-local for now.
This is not full horizontal clustering; use one polling instance per bot token.

## Performance notes

Adding Redis or PostgreSQL is optional, not automatically faster than SQLite.
For a single-process bot with a small number of users, the local defaults
typically have lower latency and fewer failure modes. Use Redis mainly for a
shared cache and PostgreSQL for concurrency or external database management.
