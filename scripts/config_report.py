"""Inspect the supported .env settings without printing secret values.

Run: python -m scripts.config_report --list
     python -m scripts.config_report --check [--strict]
"""
from __future__ import annotations

import argparse
import ast
import difflib
import math
import os
import re
from pathlib import Path
from typing import Mapping

from core.settings import (
    AppSettings,
    ConfigFileError,
    ROOT_DIR,
    RESOURCE_DIR,
    project_env_files,
    project_env_values,
)

_EXAMPLE_LINE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


def example_groups(root_dir: Path = RESOURCE_DIR) -> dict[str, tuple[str, ...]]:
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



def _invalid_scalar_keys(values: Mapping[str, str]) -> list[str]:
    """Flag invalid scalar env values even where legacy parsers use defaults."""
    tree = ast.parse(
        (RESOURCE_DIR / "core/settings.py").read_text(encoding="utf-8")
    )
    parsers = {"_int", "_float", "_bool", "_strict_int", "_strict_float"}
    types: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        kind = node.func.id
        if kind not in parsers or len(node.args) < 2:
            continue
        key = node.args[1]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            types[key.value] = (
                "boolean" if kind == "_bool"
                else "float" if "float" in kind
                else "integer"
            )

    invalid: set[str] = set()
    for key, kind in types.items():
        if key not in values:
            continue
        raw = str(values[key]).strip()
        try:
            if kind == "boolean":
                if raw.casefold() not in {
                    "0", "1", "true", "false", "yes", "no", "on", "off"
                }:
                    raise ValueError("invalid boolean")
            elif kind == "integer":
                int(raw)
            else:
                if not math.isfinite(float(raw)):
                    raise ValueError("non-finite")
        except ValueError:
            invalid.add(key)
    for key, raw in values.items():
        if key.startswith("UPSTREAM_RATE_LIMIT_") and key.endswith("_PER_MINUTE"):
            try:
                if int(str(raw)) <= 0:
                    raise ValueError("not positive")
            except ValueError:
                invalid.add(key)
    return sorted(invalid)


def validate_config(
    root_dir: Path = ROOT_DIR,
    *,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Validate settings and report keys missing from the shipped examples.

    No secret values appear in the return value, even when parsing fails.
    """
    values = project_env_values(root_dir)
    effective = dict(values)
    effective.update(os.environ if environment is None else environment)
    invalid = _invalid_scalar_keys(effective)
    if invalid:
        # Only key names are reported. Never render active secrets or IDs.
        raise ConfigFileError("Invalid numeric/boolean keys: " + ", ".join(invalid))
    AppSettings.from_env(effective)
    examples = root_dir if (root_dir / ".env.example.d").is_dir() else RESOURCE_DIR
    documented = {key for keys in example_groups(examples).values() for key in keys}
    return sorted(set(values) - documented)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List documented key names")
    parser.add_argument("--check", action="store_true", help="Validate local .env files")
    parser.add_argument(
        "--explain", metavar="KEY",
        help="Find the example file for one setting without exposing active values",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat undocumented local key names as configuration errors",
    )
    args = parser.parse_args()
    if not args.list and not args.check and not args.explain:
        args.check = True
    if args.explain:
        matches = [
            group for group, keys in example_groups().items()
            if args.explain in keys
        ]
        if not matches:
            print(f"Unknown setting: {args.explain}")
            return 2
        for group in matches:
            print(f"{args.explain}: .env.example.d/{group}")

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
            if isinstance(error, ConfigFileError) and str(error).startswith(
                "Invalid numeric/boolean keys:"
            ):
                print(f"Configuration validation failed: {error}")
            else:
                print(f"Configuration validation failed: {type(error).__name__}")
            return 2
        files = project_env_files()
        print(f"Configuration parsed successfully ({len(files)} project files).")
        if unexpected:
            print("Undocumented keys (names only):")
            known = {key for keys in example_groups().values() for key in keys}
            for key in unexpected:
                suggestions = difflib.get_close_matches(
                    key, known, n=1, cutoff=0.65
                )
                hint = f" (did you mean {suggestions[0]}?)" if suggestions else ""
                print(f"  {key}{hint}")
            if args.strict:
                return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
