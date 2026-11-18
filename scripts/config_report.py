"""Inspect the supported .env settings without printing secret values.

Run: python -m scripts.config_report --list
     python -m scripts.config_report --check [--strict]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from core.settings import (
    AppSettings,
    ConfigFileError,
    ROOT_DIR,
    project_env_files,
    project_env_values,
)

_EXAMPLE_LINE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


def example_groups(root_dir: Path = ROOT_DIR) -> dict[str, tuple[str, ...]]:
    examples = root_dir / ".env.example.d"
    groups: dict[str, tuple[str, ...]] = {}
    for file in sorted(examples.glob("*.env")):
        keys = []
        for line in file.read_text(encoding="utf-8").splitlines():
            match = _EXAMPLE_LINE.match(line)
            if match is not None:
                keys.append(match.group(1))
        groups[file.name] = tuple(keys)
    standalone = root_dir / ".env.example"
    if standalone.is_file():
        legacy = []
        for line in standalone.read_text(encoding="utf-8").splitlines():
            match = _EXAMPLE_LINE.match(line)
            if match is not None:
                legacy.append(match.group(1))
        groups[".env.example"] = tuple(legacy)
    return groups


def validate_config(root_dir: Path = ROOT_DIR) -> list[str]:
    """Validate settings and report keys missing from the shipped examples.

    No secret values appear in the return value, even when parsing fails.
    """
    values = project_env_values(root_dir)
    AppSettings.from_env(values)
    documented = {key for keys in example_groups(root_dir).values() for key in keys}
    return sorted(set(values) - documented)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List documented key names")
    parser.add_argument("--check", action="store_true", help="Validate local .env files")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat undocumented local key names as configuration errors",
    )
    args = parser.parse_args()
    if not args.list and not args.check:
        args.check = True

    if args.list:
        for group, keys in example_groups().items():
            print(f"\n{group} ({len(keys)} options)")
            for key in keys:
                print(f"  {key}")
    if args.check:
        try:
            unexpected = validate_config()
        except (ValueError, ConfigFileError) as error:
            # Never print raw environment values or connection strings.
            print(f"Configuration validation failed: {type(error).__name__}")
            return 2
        files = project_env_files()
        print(f"Configuration parsed successfully ({len(files)} project files).")
        if unexpected:
            print("Undocumented keys (names only):")
            for key in unexpected:
                print(f"  {key}")
            if args.strict:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
