"""Version information injected by release CI; source checkouts default to dev."""
from __future__ import annotations

import os

VERSION = os.environ.get("ANIME_DB_BOT_VERSION", "dev")
