"""PyInstaller entry point: starts without an installed Python interpreter."""
from __future__ import annotations

import os
import sys

from scripts.release_entry import main

if __name__ == "__main__":
    code = main()
    # A double-click on an unconfigured Windows console app should not close
    # its error/help output before the user can read it.
    if code and not sys.argv[1:] and os.name == "nt" and sys.stdin.isatty():
        try:
            input("Press Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(code)
