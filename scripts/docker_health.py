"""Container health probe for the polling or API-only runtime.

No outbound network connection or bot token is needed. The API liveness
endpoint is public, while the readiness state does not reveal credentials.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

from core.settings import get_settings


def main() -> int:
    settings = get_settings()
    if settings.api.enabled or os.getenv("ANIME_DB_BOT_RUN_MODE") == "api":
        url = f"http://127.0.0.1:{settings.api.port}/api/v1/health"
        try:
            with urllib.request.urlopen(url, timeout=3.0) as response:
                return 0 if response.status == 200 else 1
        except (urllib.error.URLError, OSError, TimeoutError):
            return 1
    from scripts.release_entry import current_pid, runtime_home

    return 0 if current_pid(runtime_home()) is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
