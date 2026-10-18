import tempfile
import unittest
from pathlib import Path

from tower_miniapp.coordinator import run_coordinator
from tower_miniapp.state import ALLOWED_ACCOUNTS


class TowerCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_exact_six_business_accounts_in_order(self):
        seen = []

        async def fake_run(_root, account, **kwargs):
            seen.append(account)
            return {"account": account, "status": "completed"}

        async def no_sleep(_seconds):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            results = await run_coordinator(Path(tmp), day_key="2026-09-19", now=1.0, bridge_factory=object(),
                                            transport=object(), run_account_fn=fake_run, sleep_fn=no_sleep)

        self.assertEqual(("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004", "tg9100000005",
                          "tg9100000006"), ALLOWED_ACCOUNTS)
        self.assertEqual(list(ALLOWED_ACCOUNTS), seen)
        self.assertEqual(6, len(results))


    async def test_a_failing_account_is_reported_and_idle_results_skip_the_gap(self):
        sleeps = []

        async def fake_run(_root, account, **kwargs):
            if account == "tg9100000002":
                raise RuntimeError("boom")
            return {"account": account, "status": "waiting_admission" if account == "tg9100000003" else "completed"}

        async def record_sleep(seconds):
            sleeps.append(seconds)

        results = await run_coordinator(Path("."), day_key="2026-09-19", now=1.0, bridge_factory=object(),
                                        transport=object(), run_account_fn=fake_run, sleep_fn=record_sleep,
                                        accounts=("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004"))
        self.assertEqual({"ok": False, "account": "tg9100000002", "status": "account_run_failed",
                          "error": "RuntimeError"}, results[1])
        self.assertEqual([3.0, 3.0], sleeps)  # none after the idle third account, none after the last

if __name__ == "__main__":
    unittest.main()
