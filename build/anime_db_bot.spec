# -*- mode: python ; coding: utf-8 -*-
"""Fast onedir portable build. No extraction-on-launch and no Python install."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path.cwd()
data = collect_data_files("localization")
# Bundle source metadata needed by the runtime ENV inspector in frozen builds.
data.append((str(root / "core" / "settings.py"), "core"))
data.extend(
    (str(root / path), path)
    for path in (
        ".env.example.d",
        ".proxies.example.d",
        ".user-agents.example.d",
    )
)
hidden = ["bots.telegram.bot", "psycopg", "psycopg_pool", "redis.asyncio"]
hidden.extend(collect_submodules("sources.providers"))
hidden.extend(collect_submodules("search.providers"))

analysis = Analysis(
    [str(root / "scripts" / "binary_main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=data,
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "IPython", "pytest", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="anime-db-bot",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="anime-db-bot",
)
