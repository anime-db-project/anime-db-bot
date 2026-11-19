#!/bin/bash
set -u
cd -- "$(dirname -- "$0")" || exit 1
./anime-db-bot run "$@"
result=$?
if [ "$result" -ne 0 ]; then
  printf '\nThe bot exited with status %s.\n' "$result"
  read -r -p "Press Enter to close this window..." _
fi
exit "$result"
