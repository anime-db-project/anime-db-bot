#!/usr/bin/env sh
set -eu
cd -- "$(dirname -- "$0")"
exec ./anime-db-bot run "$@"
