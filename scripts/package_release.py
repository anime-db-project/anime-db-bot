"""Package a PyInstaller onedir bundle into a portable release archive.

Usage: python scripts/package_release.py --platform linux --arch x64 --version 0.1.0
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package(platform_name: str, arch: str, version: str) -> Path:
    if platform_name not in {"windows", "linux", "macos"}:
        raise ValueError("Unsupported release platform")
    if arch not in {"x64", "arm64"}:
        raise ValueError("Unsupported CPU architecture")
    source = ROOT / "dist" / "anime-db-bot"
    binary = source / ("anime-db-bot.exe" if platform_name == "windows" else "anime-db-bot")
    if not binary.is_file():
        raise FileNotFoundError(f"Missing PyInstaller executable: {binary}")
    output = ROOT / "release"
    output.mkdir(exist_ok=True)
    stem = f"anime-db-bot-v{version}-{platform_name}-{arch}"
    filename = stem + (".zip" if platform_name == "windows" else ".tar.gz")
    archive = output / filename

    with tempfile.TemporaryDirectory(prefix="anime-db-bot-release-") as temp:
        folder = Path(temp) / "anime-db-bot"
        shutil.copytree(source, folder, symlinks=True)
        helpers = {
            "windows": ("Run.cmd", "Start-Background.cmd", "Stop.cmd"),
            "linux": ("Run.sh",),
            "macos": ("Run.command",),
        }[platform_name]
        for name in helpers:
            target = folder / name
            shutil.copy2(ROOT / "build" / name, target)
            if name.endswith((".command", ".sh")):
                target.chmod(0o755)
        (folder / "README-START.txt").write_text(
            f"anime-db-bot v{version} — {platform_name}/{arch}\n\n"
            "Everything required to run is included. Keep _internal beside the executable.\n"
            "The first launch creates .env.d configuration templates beside the binary.\n"
            "Set BOT_TOKEN in .env.d/telegram.env or through your environment.\n\n"
            "Commands:\n"
            "  anime-db-bot [run]           Foreground (default for double-click)\n"
            "  anime-db-bot start           Background supervisor\n"
            "  anime-db-bot stop            Graceful shutdown\n"
            "  anime-db-bot restart         Restart background supervisor\n"
            "  anime-db-bot status          Show whether the bot is running\n"
            "  anime-db-bot doctor          Platform and path diagnostics\n"
            "  anime-db-bot check           Validate configuration\n"
            "  anime-db-bot init            Create missing configuration templates\n"
            "  anime-db-bot --help          Show all runtime flags\n\n"
            "Use ./anime-db-bot on Linux/macOS and anime-db-bot.exe on Windows.\n"
            "Mac: double-click Run.command to open Terminal and start the bot.\n"
            "Linux: double-click the executable where supported, or run Run.sh.\n"
            "Windows: double-click anime-db-bot.exe or Run.cmd.\n"
            "Unsigned macOS builds may need Gatekeeper approval on first launch.\n"
            "Pass --home DIRECTORY to store config/data/logs outside this folder.\n"
            "Logs appear in logs/launcher.log and logs/YYYY.MM.DD/.\n",
            encoding="utf-8",
        )
        if platform_name == "windows":
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as zipped:
                for item in sorted(folder.rglob("*")):
                    if item.is_file():
                        zipped.write(item, item.relative_to(folder.parent))
        else:
            with tarfile.open(archive, mode="w:gz", compresslevel=6) as tar:
                tar.add(folder, arcname=folder.name, recursive=True)
    print(archive)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows", "linux", "macos"))
    parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
    parser.add_argument("--version", required=True)
    options = parser.parse_args()
    package(options.platform, options.arch, options.version.removeprefix("v"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
