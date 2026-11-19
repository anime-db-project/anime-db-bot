"""Secret-free, bounded snapshots for an operator console in another process.

A separate 'docker exec' or native CLI does not share process-local metrics or
circuit breakers with the Telegram worker. The worker periodically exports
a private, atomic JSON snapshot to the same writable runtime home.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from core.settings import ROOT_DIR

INTERVAL_SECONDS = 10.0
MAX_SNAPSHOT_AGE_SECONDS = 40.0


def status_path(home: Path = ROOT_DIR) -> Path:
    return home / "data" / "runtime" / "operator-status.json"


def snapshot_data(context: Any) -> dict[str, Any]:
    """Capture only public-safe counters, no error bodies or secrets."""
    from bots.telegram.admin import _status_lines

    descriptors = context.runtime_source_descriptors()
    metrics = context.runtime_metrics
    sources: list[dict[str, Any]] = []
    for descriptor in descriptors:
        health = context.source_health.snapshot(descriptor.key)
        sources.append({
            **asdict(descriptor),
            "state": health.state,
            "consecutive_failures": health.consecutive_failures,
            "probe_in_flight": health.probe_in_flight,
            "calls_1h": asdict(metrics.source_window(descriptor.key, 3600)),
            "calls_24h": asdict(metrics.source_window(descriptor.key, 86400)),
        })
    return {
        "schema": 1,
        "timestamp": time.time(),
        "worker_pid": os.getpid(),
        "uptime_seconds": metrics.uptime_seconds,
        "summary": _status_lines(
            "en",
            source_health=context.source_health,
            runtime_metrics=metrics,
            source_descriptors=descriptors,
        ),
        "sources": sources,
        "windows": {
            "1h": asdict(metrics.window(3600)),
            "5h": asdict(metrics.window(5 * 3600)),
            "10h": asdict(metrics.window(10 * 3600)),
            "24h": asdict(metrics.window(86400)),
        },
        "caches": [asdict(cache) for cache in metrics.cache_window(86400)],
    }


def _write_snapshot(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_live_snapshot(home: Path = ROOT_DIR) -> dict[str, Any] | None:
    """Reject old/malformed files instead of claiming stale stats are live."""
    try:
        data = json.loads(status_path(home).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            return None
        timestamp = float(data["timestamp"])
        if not 0 <= time.time() - timestamp <= MAX_SNAPSHOT_AGE_SECONDS:
            return None
        if not isinstance(data.get("sources"), list):
            return None
        return data
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return None


def remove_snapshot_if_owned(home: Path = ROOT_DIR) -> None:
    path = status_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("worker_pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass


async def publish_status_snapshots(context: Any) -> None:
    """Publish while polling; never interrupt Telegram on monitoring failures."""
    try:
        while True:
            try:
                data = snapshot_data(context)
                await asyncio.to_thread(_write_snapshot, status_path(), data)
            except Exception:
                logging.warning("Unable to publish local operator status", exc_info=True)
            await asyncio.sleep(INTERVAL_SECONDS)
    finally:
        await asyncio.to_thread(remove_snapshot_if_owned)
