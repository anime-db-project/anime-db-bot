from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from api.http import create_app
from api.jobs import JobManager
from api.operations import ApiOperationError
from core.settings import AppSettings


ADMIN_KEY = "m" * 32
READ_KEY = "n" * 32


class ApiMaintenanceJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_runs_and_returns_result(self) -> None:
        manager = JobManager(max_active=1)
        worker = AsyncMock(return_value={"updated": True})
        with patch("api.jobs.execute_operation", worker):
            job = manager.submit(
                SimpleNamespace(), "mapping.update", {"confirm": True},
                "admin",
            )
            self.assertEqual(job["state"], "queued")
            await asyncio.sleep(0)
        completed = manager.job(job["id"], "admin")
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(completed["result"], {"updated": True})
        self.assertEqual(worker.await_count, 1)
        await manager.close()

    async def test_requires_admin_and_confirm(self) -> None:
        manager = JobManager()
        for role, data, expected in (
            ("read", {"confirm": True}, 403),
            ("admin", {}, 409),
        ):
            with self.subTest(role=role):
                with self.assertRaises(ApiOperationError) as ctx:
                    manager.submit(SimpleNamespace(), "index.imdb", data, role)
                self.assertEqual(ctx.exception.code, expected)
        await manager.close()

    async def test_does_not_start_overlapping_heavy_jobs(self) -> None:
        manager = JobManager(max_active=1)
        gate = asyncio.Event()

        async def wait_forever(*_args, **_kwargs):
            await gate.wait()
            return {"done": True}

        with patch("api.jobs.execute_operation", side_effect=wait_forever):
            first = manager.submit(
                SimpleNamespace(), "index.baha", {"confirm": True}, "admin"
            )
            await asyncio.sleep(0)
            with self.assertRaises(ApiOperationError) as ctx:
                manager.submit(
                    SimpleNamespace(), "mapping.update",
                    {"confirm": True}, "admin",
                )
            self.assertEqual(ctx.exception.code, 429)
            gate.set()
            await asyncio.sleep(0)
        self.assertEqual(manager.job(first["id"], "admin")["state"], "succeeded")
        await manager.close()


class ApiHttpJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = AppSettings.from_env({
            "API_AUTH_MODE": "apikey",
            "API_ADMIN_KEY": ADMIN_KEY,
            "API_READ_KEY": READ_KEY,
        })
        self.client = TestClient(
            TestServer(create_app(settings, context=SimpleNamespace(settings=settings)))
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_maintenance_returns_202_and_admin_can_poll(self) -> None:
        with patch(
            "api.jobs.execute_operation",
            new_callable=AsyncMock,
            return_value={"updated": True},
        ):
            response = await self.client.post(
                "/api/v1/mappings/update",
                headers={"X-API-Key": ADMIN_KEY},
                json={"confirm": True},
            )
            self.assertEqual(response.status, 202)
            job = (await response.json())["data"]
            self.assertTrue(job["id"])
            await asyncio.sleep(0)
            response = await self.client.get(
                "/api/v1/jobs/" + job["id"],
                headers={"X-API-Key": ADMIN_KEY},
            )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["data"]["state"], "succeeded")
        response = await self.client.get(
            "/api/v1/jobs", headers={"X-API-Key": READ_KEY}
        )
        self.assertEqual(response.status, 403)

    async def test_graphql_admin_build_returns_job_id(self) -> None:
        query = 'mutation { updateMappings(confirm:true) }'
        with patch(
            "api.jobs.execute_operation",
            new_callable=AsyncMock,
            return_value={"updated": True},
        ):
            response = await self.client.post(
                "/graphql",
                headers={"Authorization": "Bearer " + ADMIN_KEY},
                json={"query": query},
            )
            self.assertEqual(response.status, 200)
            data = await response.json()
            self.assertIn("data", data)
            self.assertTrue(data["data"]["updateMappings"]["id"])

    async def test_graphql_jobs_are_admin_only(self) -> None:
        response = await self.client.post(
            "/graphql",
            headers={"X-API-Key": READ_KEY},
            json={"query": "{ jobs }"},
        )
        data = await response.json()
        self.assertIn("errors", data)
        self.assertIn("Administrator", data["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
