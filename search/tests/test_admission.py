"""Protect shared admission capacity and cleanup."""

import asyncio
from contextlib import suppress
import unittest

from search.api.shared.admission import Busy, Slots


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_rejects_excess_claims_without_consuming_a_slot(self):
        slots = Slots(2)
        async with slots.claim():
            self.assertEqual(1, slots.used)
            async with slots.claim():
                self.assertEqual(2, slots.used)

                async def excess_claim():
                    async with slots.claim():
                        self.fail("an excess request entered the protected work")

                with self.assertRaisesRegex(Busy, "^service busy$"):
                    await asyncio.wait_for(excess_claim(), timeout=1)
                self.assertEqual(2, slots.used)
            self.assertEqual(1, slots.used)
        self.assertEqual(0, slots.used)

    async def test_exception_releases_capacity_for_the_next_request(self):
        slots = Slots(1)
        with self.assertRaisesRegex(ValueError, "^request failed$"):
            async with slots.claim():
                raise ValueError("request failed")
        self.assertEqual(0, slots.used)
        async with slots.claim():
            self.assertEqual(1, slots.used)
        self.assertEqual(0, slots.used)

    async def test_cancelling_a_pending_request_releases_capacity(self):
        slots = Slots(1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_slot():
            async with slots.claim():
                entered.set()
                await release.wait()

        task = asyncio.create_task(hold_slot())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertEqual(1, slots.used)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(0, slots.used)
            async with slots.claim():
                self.assertEqual(1, slots.used)
            self.assertEqual(0, slots.used)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
