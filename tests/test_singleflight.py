from __future__ import annotations

import asyncio
import unittest

from core.singleflight import SingleFlight


class SingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_uses_one_shared_task(self) -> None:
        flight: SingleFlight[str, int] = SingleFlight()
        calls = 0

        async def factory() -> int:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return 7

        results = await asyncio.gather(
            *(flight.run("same", factory) for _ in range(40))
        )
        self.assertEqual(results, [7] * 40)
        self.assertEqual(calls, 1)
        await asyncio.sleep(0)
        self.assertEqual(len(flight._tasks), 0)

    async def test_one_waiter_cancelling_keeps_shared_call(self) -> None:
        flight: SingleFlight[str, int] = SingleFlight()
        release = asyncio.Event()

        async def factory() -> int:
            await release.wait()
            return 10

        first = asyncio.create_task(flight.run("key", factory))
        second = asyncio.create_task(flight.run("key", factory))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, 10)
