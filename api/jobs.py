"""Bounded in-process background maintenance tasks for expensive API actions.

Index builds can run much longer than an HTTP timeout. Scheduling them as
jobs ensures the response is fast and callers can poll status without
retrying and duplicating a database replacement. Jobs are intentionally
process-local; an API restart loses their history.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import secrets
import time
from typing import Any, Mapping

from api.operations import ApiOperationError, OPERATIONS, execute_operation

LONG_OPERATIONS = frozenset(("mapping.update", "index.imdb", "index.baha"))


@dataclass(slots=True)
class JobRecord:
    job_id: str
    operation: str
    created_at: float
    updated_at: float
    state: str = "queued"
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "operation": self.operation,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, *, max_active: int = 1, max_history: int = 100) -> None:
        self.max_active = max_active
        self.max_history = max_history
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def list_jobs(self, role: str) -> list[dict[str, Any]]:
        if role != "admin":
            raise ApiOperationError(403, "Administrator scope required")
        return [j.as_dict() for j in reversed(self._jobs.values())]

    def job(self, job_id: str, role: str) -> dict[str, Any]:
        if role != "admin":
            raise ApiOperationError(403, "Administrator scope required")
        item = self._jobs.get(job_id)
        if item is None:
            raise ApiOperationError(404, "Job not found")
        return item.as_dict()

    def submit(
        self,
        context: Any,
        name: str,
        payload: Mapping[str, Any],
        role: str,
    ) -> dict[str, Any]:
        spec = OPERATIONS.get(name)
        if name not in LONG_OPERATIONS or spec is None:
            raise ApiOperationError(400, "This operation does not support jobs")
        if role != "admin":
            raise ApiOperationError(403, "Administrator scope required")
        if payload.get("confirm") is not True:
            raise ApiOperationError(409, "Explicit confirm=true is required")
        if len(self._tasks) >= self.max_active:
            raise ApiOperationError(429, "Maintenance job limit reached")
        now = time.time()
        job_id = secrets.token_urlsafe(16)
        record = JobRecord(job_id, name, now, now)
        self._jobs[job_id] = record
        while len(self._jobs) > self.max_history:
            key = next(iter(self._jobs))
            if key in self._tasks:
                break
            self._jobs.pop(key, None)

        async def run() -> None:
            record.state = "running"
            record.updated_at = time.time()
            try:
                record.result = await execute_operation(
                    context, name, dict(payload), role="admin"
                )
                record.state = "succeeded"
            except asyncio.CancelledError:
                record.state = "cancelled"
                raise
            except ApiOperationError as exc:
                record.state = "failed"
                record.error = exc.message
            except Exception:
                record.state = "failed"
                record.error = "Unexpected background maintenance failure"
            finally:
                record.updated_at = time.time()
                self._tasks.pop(job_id, None)

        task = asyncio.create_task(run(), name="api-job-" + name)
        self._tasks[job_id] = task
        return record.as_dict()

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
