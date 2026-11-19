"""Safe, range-validated local configuration edits for native and Docker CLI.

Configuration is only persisted on explicit operator action. Secrets are never
returned in command results. Changes require a bot restart to become effective.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from dotenv import dotenv_values, set_key, unset_key

from core.settings import AppSettings, ConfigFileError, ROOT_DIR, project_env_files
from scripts.config_report import example_groups

SENSITIVE_MARKERS = (
    "TOKEN", "PASSWORD", "SECRET", "CLIENT_ID", "API_KEY",
    "DATABASE_URL", "REDIS_URL", "AWS_ACCESS_KEY", "AWS_SESSION",
)
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")


def _group_for(key: str, root: Path) -> str:
    for filename, keys in example_groups(root).items():
        if filename != ".env.example" and key in keys:
            return filename
    raise ConfigFileError(f"Unknown configuration key: {key}")


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _find_declared_path(key: str, root: Path) -> Path | None:
    for path in project_env_files(root):
        if any(
            (match := _KEY_LINE.match(line)) is not None and match.group(1) == key
            for line in _read_lines(path)
        ):
            return path
    return None


def _effective_for_validation(
    root: Path, key: str, value: str | None
) -> dict[str, str]:
    from core.settings import project_env_values

    current = project_env_values(root)
    effective = {**current, **os.environ}
    if value is None:
        effective.pop(key, None)
    else:
        effective[key] = value
    return effective


def inspect_key(key: str, *, root: Path = ROOT_DIR) -> dict[str, object]:
    group = _group_for(key, root)
    file = _find_declared_path(key, root)
    env_value = os.environ.get(key)
    if file is not None:
        actual = dotenv_values(file).get(key)
        source = str(file)
    elif env_value is not None:
        actual = env_value
        source = "process environment"
    else:
        actual = dotenv_values(root / ".env.example.d" / group).get(key)
        source = "example default"
    secret = any(mark in key for mark in SENSITIVE_MARKERS) or key.endswith("_URL")
    return {
        "key": key,
        "group": group,
        "source": source,
        "configured": bool(actual),
        "value": "<redacted>" if secret and actual else actual,
        "masked": secret,
    }


def update_key(
    key: str,
    value: str | None,
    *,
    root: Path = ROOT_DIR,
) -> dict[str, object]:
    """Persist one valid key in its existing file, preserving legacy layouts."""
    group = _group_for(key, root)
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("Environment values must be single-line strings.")
    # Reject invalid combinations before writing to disk. Treat the edited
    # value as authoritative even when this console has a stale process env.
    AppSettings.from_env(_effective_for_validation(root, key, value))
    target = _find_declared_path(key, root)
    if target is None:
        target = root / ".env" if (root / ".env").is_file() else root / ".env.d" / group
    if value is None:
        if not target.is_file():
            return {"key": key, "changed": False, "restart_required": False}
        removed, _ = unset_key(str(target), key)
        changed = bool(removed)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.touch(mode=0o600)
        result, _, _ = set_key(str(target), key, value, quote_mode="auto")
        changed = bool(result)
    if target.exists() and os.name != "nt":
        target.chmod(0o600)
    return {
        "key": key,
        "file": str(target),
        "changed": changed,
        "restart_required": changed,
        "note": (
            "Restart the bot to apply the new setting. "
            "Process environment variables override local files."
            if changed else "No file entry changed."
        ),
    }
