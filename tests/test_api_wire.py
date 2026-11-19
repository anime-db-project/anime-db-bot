"""Provider wire responses preserve upstream quotas and health protection."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.identity import AnimeIdentity
from sources.runtime.rate_limited import RateLimitedSourceProxy
from sources.runtime.resilient import RetryingSourceProxy


class SourceWireProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_data_passes_through_quota_and_retry_proxy(self) -> None:
        original = SimpleNamespace(
            name="MyAnimeList",
            get_wire_details=AsyncMock(
                return_value={"providerSpecific": {"field": 17}}
            ),
        )
        limited = RateLimitedSourceProxy(original, "mal")
        resilient = RetryingSourceProxy(limited)
        identity = AnimeIdentity({"mal": "5114"})
        with patch(
            "sources.runtime.rate_limited.wait_for_upstream",
            new_callable=AsyncMock,
        ) as upstream:
            result = await resilient.get_wire_details(identity)
        self.assertEqual(result["providerSpecific"]["field"], 17)
        upstream.assert_awaited_once_with("mal", "metadata")
        original.get_wire_details.assert_awaited_once_with(identity)

    async def test_unsupported_adapter_never_bypasses_quota_wrapper(self) -> None:
        original = SimpleNamespace(name="Unknown")
        limited = RateLimitedSourceProxy(original, "mal")
        resilient = RetryingSourceProxy(limited)
        with patch(
            "sources.runtime.rate_limited.wait_for_upstream",
            new_callable=AsyncMock,
        ) as upstream:
            with self.assertRaises(NotImplementedError):
                await resilient.get_wire_details(AnimeIdentity({"mal": "1"}))
        upstream.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
