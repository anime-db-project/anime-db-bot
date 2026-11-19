# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.lock requirements-backends.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.lock -r requirements-backends.txt

FROM python:3.12-slim-bookworm AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="anime-db-bot" \
      org.opencontainers.image.description="Multi-source anime Telegram bot" \
      org.opencontainers.image.source="https://github.com/anime-db-project/anime-db-bot" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANIME_DB_BOT_HOME=/data \
    ANIME_DB_BOT_VERSION=${APP_VERSION}
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 anime \
    && useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home anime \
    && mkdir -p /app /data /data/logs /data/data \
    && chown -R anime:anime /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY api/ api/
COPY bots/ bots/
COPY core/ core/
COPY localization/ localization/
COPY mappings/ mappings/
COPY search/ search/
COPY services/ services/
COPY sources/ sources/
COPY scripts/ scripts/
COPY .env.example.d/ .env.example.d/
COPY .proxies.example.d/ .proxies.example.d/
COPY .user-agents.example.d/ .user-agents.example.d/
USER anime
EXPOSE 8181
VOLUME ["/data"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "scripts.docker_health"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "scripts.release_entry"]
CMD ["run"]
