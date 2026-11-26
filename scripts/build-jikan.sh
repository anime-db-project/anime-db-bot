#!/usr/bin/env bash
# Explicit local build only. No scheduled updates and no automatic rebuilds.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
name=anime-db-bot-Jikan
image=anime-db-bot-jikan:current
candidate=anime-db-bot-jikan:candidate
rollback=anime-db-bot-jikan:rollback
force=false
if [[ "${1:-}" == --yes ]]; then force=true; shift; fi
if (( $# > 1 )); then echo "Usage: $0 [--yes] [JIKAN_VERSION]" >&2; exit 2; fi
python=python3
if [[ -x .venv/bin/python ]]; then
    python=.venv/bin/python
    export JIKAN_PORT="${JIKAN_PORT:-$("$python" -c 'from core.settings import get_settings; print(get_settings().sources.jikan_port)')}"
fi
export JIKAN_PORT="${JIKAN_PORT:-8091}"
version="${1:-}"
if [[ -z "$version" ]]; then
    version="$(curl -fsSL --connect-timeout 10 --max-time 30 https://api.github.com/repos/jikan-me/jikan/releases/latest | "$python" -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
fi
version="${version#v}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9.-]+)?$ ]] || { echo "Invalid Jikan version" >&2; exit 2; }
old="$(docker inspect --type container --format '{{.Image}}' "$name" 2>/dev/null || true)"
current=""
if [[ -n "$old" ]]; then
    current="$(docker run --rm --network none --entrypoint php "$old" -r 'require "/app/vendor/autoload.php"; echo Composer\InstalledVersions::getPrettyVersion("jikan-me/jikan");' 2>/dev/null || true)"
elif docker image inspect "$image" >/dev/null 2>&1; then
    current="$(docker run --rm --network none --entrypoint php "$image" -r 'require "/app/vendor/autoload.php"; echo Composer\InstalledVersions::getPrettyVersion("jikan-me/jikan");' 2>/dev/null || true)"
fi
current="${current#v}"
if [[ "$current" == "$version" && "$force" != true ]]; then
    if [[ ! -t 0 ]]; then echo "Jikan $version already installed; use --yes to rebuild."; exit 0; fi
    read -r -p "Jikan $version is already installed. Rebuild anyway? [y/N] " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || exit 0
fi
echo "Building Jikan $version without cache; existing container stays running."
docker build --pull --no-cache --build-arg "JIKAN_VERSION=$version" -f jikan_service/Dockerfile -t "$candidate" .
docker run --rm --network none --entrypoint php "$candidate" -l /app/public/router.php
built="$(docker run --rm --network none --entrypoint php "$candidate" -r 'require "/app/vendor/autoload.php"; echo Composer\InstalledVersions::getPrettyVersion("jikan-me/jikan");')"
[[ "${built#v}" == "$version" ]] || { echo "Unexpected built version: $built" >&2; exit 1; }
# Do not destroy the running instance until the new image is ready.
if [[ -n "$old" ]]; then docker tag "$old" "$rollback"; fi
export JIKAN_IMAGE="$image"
docker tag "$candidate" "$image"
restore() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    if [[ -n "$old" ]]; then
        docker tag "$rollback" "$image"
        docker compose up -d --no-build --pull never jikan
    fi
}
if [[ -n "$old" ]]; then
    if ! docker stop "$name"; then exit 1; fi
    if ! docker rm "$name"; then
        docker start "$name" || true
        exit 1
    fi
fi
if ! docker compose up -d --no-build --pull never jikan; then
    restore
    exit 1
fi
healthy=false
for _ in {1..30}; do
    if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || true)" == healthy ]]; then healthy=true; break; fi
    sleep 2
done
if [[ "$healthy" != true ]]; then
    echo "New container failed health check; restoring previous image." >&2
    restore
    exit 1
fi
docker image rm "$candidate" >/dev/null 2>&1 || true
if [[ -n "$old" ]]; then docker image rm "$rollback" >/dev/null 2>&1 || true; fi
echo "Jikan $version installed; previous container removed."
