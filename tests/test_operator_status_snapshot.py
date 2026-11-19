from __future__ import annotations

import json
import os
from pathlib import Path
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from core.runtime_metrics import RuntimeMetricsRegistry, RuntimeSourceDescriptor
from services.runtime.source_health import SourceHealthRegistry
from services.runtime.status_snapshot import (
    _write_snapshot,
    read_live_snapshot,
    remove_snapshot_if_owned,
    snapshot_data,
    status_path,
)


class OperatorStatusSnapshotTests(unittest.TestCase):
    def test_snapshot_is_live_and_secret_free(self) -> None:
        metrics = RuntimeMetricsRegistry()
        health = SourceHealthRegistry(failure_threshold=3, cooldown_seconds=30)
        context = SimpleNamespace(
            runtime_metrics=metrics,
            source_health=health,
            runtime_source_descriptors=lambda: (
                RuntimeSourceDescriptor(key="mal", display_name="MyAnimeList"),
            ),
        )
        payload = snapshot_data(context)
        self.assertIn("status", "\n".join(payload["summary"]).casefold())
        self.assertEqual(payload["sources"][0]["key"], "mal")
        self.assertEqual(payload["worker_pid"], os.getpid())
        self.assertNotIn("last_error", json.dumps(payload))
        self.assertEqual(payload["schema"], 1)

    def test_read_rejects_invalid_or_expired_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            path = status_path(home)
            self.assertIsNone(read_live_snapshot(home))
            _write_snapshot(
                path, {"schema": 1, "timestamp": time.time(),
                       "worker_pid": os.getpid(), "sources": []}
            )
            self.assertIsNotNone(read_live_snapshot(home))
            _write_snapshot(
                path, {"schema": 1, "timestamp": time.time() - 3600,
                       "worker_pid": os.getpid(), "sources": []}
            )
            self.assertIsNone(read_live_snapshot(home))
            path.write_text("{invalid-json", encoding="utf-8")
            self.assertIsNone(read_live_snapshot(home))

    def test_shutdown_removes_only_owned_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            home = Path(temp)
            path = status_path(home)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"worker_pid": os.getpid() + 100}), encoding="utf-8"
            )
            remove_snapshot_if_owned(home)
            self.assertTrue(path.exists())
            path.write_text(
                json.dumps({"worker_pid": os.getpid()}), encoding="utf-8"
            )
            remove_snapshot_if_owned(home)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
