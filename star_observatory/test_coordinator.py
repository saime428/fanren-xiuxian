import tempfile
import unittest
from pathlib import Path

from star_observatory.coordinator import run_coordinator
from star_observatory.state import ALLOWED_ACCOUNTS


class StarObservatoryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_exactly_the_two_star_palace_accounts_in_order(self):
        seen = []

        async def fake_runner(_root, account, **kwargs):
            seen.append((account, kwargs["force"]))
            return {"account": account, "status": "wait"}

        async def no_sleep(_seconds):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            result = await run_coordinator(Path(tmp), now=1.0, bridge_factory=object(), transport=object(),
                                           account_runner=fake_runner, sleep_fn=no_sleep)

        # The accounts enabling star_observatory, in accounts-config order.
        self.assertEqual(("tg9100000001", "tg9100000004", "tg9100000005"), ALLOWED_ACCOUNTS)
        self.assertEqual([("tg9100000001", False), ("tg9100000004", False), ("tg9100000005", False)], seen)
        self.assertEqual("completed", result["status"])
        self.assertEqual(3, len(result["accounts"]))


if __name__ == "__main__":
    unittest.main()
