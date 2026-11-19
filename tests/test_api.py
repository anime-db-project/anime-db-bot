from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from api.http import create_app
from api.operations import ApiOperationError, OPERATIONS, execute_operation
from core.settings import AppSettings


READ_KEY = "r" * 32
ADMIN_KEY = "a" * 32


def settings_with(**overrides: str) -> AppSettings:
    values = {
        "API_AUTH_MODE": "apikey",
        "API_READ_KEY": READ_KEY,
        "API_ADMIN_KEY": ADMIN_KEY,
        "API_HOST": "127.0.0.1",
    }
    values.update(overrides)
    return AppSettings.from_env(values)


class ApiAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = settings_with()
        self.context = SimpleNamespace(settings=self.settings)
        self.app = create_app(self.settings, context=self.context)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.activation = patch(
            "api.operations.activate_app_context",
            side_effect=lambda *_args: nullcontext(),
        )
        self.activation.start()

    async def asyncTearDown(self) -> None:
        self.activation.stop()
        await self.client.close()

    async def test_public_health_and_versions(self) -> None:
        response = await self.client.get("/api/v1/health")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["data"]["status"], "ok")
        response = await self.client.get("/api/versions")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["data"]["versions"], ["v1"])

    async def test_private_routes_require_key(self) -> None:
        response = await self.client.get("/api/v1/status")
        self.assertEqual(response.status, 401)
        response = await self.client.get(
            "/api/v1/status", headers={"Authorization": "Bearer invalid"}
        )
        self.assertEqual(response.status, 401)

    async def test_read_token_can_ping_and_read_openapi(self) -> None:
        headers = {"Authorization": f"Bearer {READ_KEY}"}
        response = await self.client.get("/api/v1/ping", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["data"]["ok"])
        response = await self.client.get("/api/v1/openapi.json", headers=headers)
        self.assertEqual(response.status, 200)
        schema = await response.json()
        self.assertEqual(schema["openapi"], "3.1.0")
        self.assertIn("/api/v1/anime", schema["paths"])

    async def test_read_key_cannot_access_admin_or_mutations(self) -> None:
        headers = {"X-API-Key": READ_KEY}
        response = await self.client.get("/api/v1/users", headers=headers)
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            "/api/v1/actions/ban",
            headers=headers, json={"user_id": 123, "confirm": True}
        )
        self.assertEqual(response.status, 403)

    async def test_admin_mutations_need_explicit_confirmation(self) -> None:
        response = await self.client.post(
            "/api/v1/actions/unban",
            headers={"X-API-Key": ADMIN_KEY},
            json={"user_id": 123},
        )
        self.assertEqual(response.status, 409)
        with patch(
            "api.operations.OperatorConsole.execute",
            new_callable=AsyncMock,
            return_value={"unbanned": True},
        ) as execute:
            response = await self.client.post(
                "/api/v1/actions/unban",
                headers={"X-API-Key": ADMIN_KEY},
                json={"user_id": 123, "confirm": True},
            )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["data"]["unbanned"])
        execute.assert_awaited_once()

    async def test_reject_cross_site_origin_without_cors_allowlist(self) -> None:
        response = await self.client.get(
            "/api/v1/health", headers={"Origin": "https://malicious.invalid"}
        )
        self.assertEqual(response.status, 403)

    async def test_graphql_executes_typed_query(self) -> None:
        response = await self.client.post(
            "/graphql", headers={"Authorization": f"Bearer {READ_KEY}"},
            json={"query": "{ apiVersion ping }"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["data"]["apiVersion"], "v1")
        self.assertEqual(data["data"]["ping"]["transport"], "local-console")

    async def test_graphql_mutation_needs_admin_scope(self) -> None:
        query = (
            'mutation { unbanUser(userId:123, confirm:true) }'
        )
        response = await self.client.post(
            "/graphql",
            headers={"Authorization": f"Bearer {READ_KEY}"},
            json={"query": query},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertIn("errors", data)
        self.assertIn("Administrator", data["errors"][0]["message"])

    async def test_graphql_field_budget_rejects_oversized_query(self) -> None:
        query = "{ " + " ".join(f"v{i}: apiVersion" for i in range(80)) + " }"
        response = await self.client.post(
            "/graphql", headers={"X-API-Key": READ_KEY},
            json={"query": query},
        )
        data = await response.json()
        self.assertIn("errors", data)
        self.assertIn("budget", data["errors"][0]["message"])

    async def test_graphql_introspection_can_be_disabled(self) -> None:
        from api.graphql import execute_graphql
        from api.jobs import JobManager

        settings = settings_with(API_GRAPHQL_INTROSPECTION="false")
        app_context = SimpleNamespace(settings=settings)
        result = await execute_graphql(
            app_context,
            role="read",
            query="{ __schema { types { name } } }",
            variables=None,
            operation_name=None,
            jobs=JobManager(),
        )
        self.assertIn("errors", result)

    async def test_graphql_root_operation_budget(self) -> None:
        query = "{ " + " ".join(f"r{i}: ping" for i in range(10)) + " }"
        response = await self.client.post(
            "/graphql", headers={"X-API-Key": READ_KEY},
            json={"query": query},
        )
        data = await response.json()
        self.assertIn("errors", data)
        self.assertIn("budget", data["errors"][0]["message"])

    async def test_read_key_cannot_change_configuration(self) -> None:
        response = await self.client.put(
            "/api/v1/config/LOG_LEVEL",
            headers={"X-API-Key": READ_KEY},
            json={"value": "DEBUG", "confirm": True},
        )
        self.assertEqual(response.status, 403)

    async def test_rest_rejects_bad_body(self) -> None:
        response = await self.client.post(
            "/api/v1/actions/ping", headers={"X-API-Key": READ_KEY},
            json=["unexpected"],
        )
        self.assertEqual(response.status, 400)


class ApiSettingsTests(unittest.TestCase):
    def test_server_is_off_and_loopback_by_default(self) -> None:
        settings = AppSettings.from_env({})
        self.assertFalse(settings.api.enabled)
        self.assertEqual(settings.api.host, "127.0.0.1")
        self.assertEqual(settings.api.port, 8181)

    def test_server_rejects_missing_or_short_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "API_READ_KEY"):
            AppSettings.from_env({}).api.validate_server()
        with self.assertRaisesRegex(ValueError, "24 characters"):
            AppSettings.from_env({"API_ADMIN_KEY": "short"}).api.validate_server()

    def test_cors_wildcards_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "API_CORS_ORIGINS"):
            AppSettings.from_env({"API_CORS_ORIGINS": "*"})
        good = AppSettings.from_env(
            {"API_CORS_ORIGINS": "https://app.example.com,http://localhost:3000"}
        )
        self.assertEqual(len(good.api.cors_origins), 2)

    def test_local_mode_requires_loopback_bind(self) -> None:
        settings = settings_with(API_AUTH_MODE="local", API_HOST="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "loopback"):
            settings.api.validate_server()

    def test_all_mutations_are_registered_admin_only(self) -> None:
        for name, spec in OPERATIONS.items():
            if spec.writes:
                self.assertEqual(spec.scope, "admin", name)


if __name__ == "__main__":
    unittest.main()
