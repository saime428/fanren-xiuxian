import unittest

from journey_miniapp.runtime import run_journey_identity
from miniapp_shared.dwelling import MiniAppError


async def _no_sleep(_seconds):
    return None


def _snapshot(count, *, available=True, limit=8, remaining_seconds=0):
    return {"account": {"journey": {"wildExperience": {
        "available": available and count < limit, "dailyCount": count, "dailyLimit": limit,
        "dailyRemaining": limit - count, "remainingSeconds": remaining_seconds}}}}


class JourneyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fills_available_attempts_to_eight_with_cautious_actions(self):
        class Client:
            count = 3
            acted = 0

            async def details(self):
                return _snapshot(self.count)

            async def act(self):
                self.acted += 1
                self.count += 1
                return {**_snapshot(self.count), "actionResult": {"type": "wild_experience", "ok": True,
                                                                  "completed": True, "handled": True,
                                                                  "outcome": "victory", "dailyCount": self.count,
                                                                  "mode": {"key": "cautious"}}}

        client = Client()
        result = await run_journey_identity(client, now_ts=1.0, sleep=_no_sleep)

        self.assertEqual(("daily_complete", 8, 5), (result["status"], result["daily_count"], result["actions"]))
        self.assertEqual(5, client.acted)
        self.assertEqual([4, 5, 6, 7, 8], [item["count"] for item in result["attempts"]])

    async def test_server_cooldown_stops_the_loop_and_reports_timer(self):
        class Client:
            count = 2
            acted = 0

            async def details(self):
                return _snapshot(self.count, available=self.acted == 0, remaining_seconds=0 if self.acted == 0 else 10800)

            async def act(self):
                self.acted += 1
                self.count += 1
                return {**_snapshot(self.count, available=False, remaining_seconds=10800),
                        "actionResult": {"type": "wild_experience", "ok": True, "completed": True, "handled": True,
                                         "dailyCount": self.count}}

        result = await run_journey_identity(Client(), now_ts=1.0, sleep=_no_sleep)

        self.assertEqual(("cooldown", 3, 1, 10800), (result["status"], result["daily_count"], result["actions"],
                                                     result["retry_after"]))

    async def test_uncertain_transport_stops_without_replay(self):
        class Client:
            calls = 0

            async def details(self):
                return _snapshot(0)

            async def act(self):
                self.calls += 1
                raise MiniAppError("request_failed", uncertain=True)

        client = Client()
        result = await run_journey_identity(client, now_ts=1.0, sleep=_no_sleep)

        self.assertEqual(("mutation_uncertain", 0), (result["status"], result["pending_before_count"]))
        self.assertEqual(1, client.calls)

    async def test_unconfirmed_reply_is_reconciled_by_a_read(self):
        class Client:
            count = 5
            reads = 0

            async def details(self):
                self.reads += 1
                return _snapshot(self.count)

            async def act(self):
                self.count += 1
                return {"ok": True}  # no actionResult, no counter → must re-read

        client = Client()
        result = await run_journey_identity(client, now_ts=1.0, sleep=_no_sleep)

        self.assertEqual(("daily_complete", 8, 3), (result["status"], result["daily_count"], result["actions"]))

    async def test_explicit_daily_limit_error_is_reported(self):
        class Client:
            async def details(self):
                return _snapshot(2, available=True)

            async def act(self):
                raise MiniAppError("wild_experience_daily_limit")

        result = await run_journey_identity(Client(), now_ts=1.0, sleep=_no_sleep)
        self.assertEqual(("daily_limit", "wild_experience_daily_limit"), (result["status"], result["error"]))

    async def test_server_limit_below_eight_caps_the_target(self):
        class Client:
            count = 2

            async def details(self):
                return _snapshot(self.count, limit=2)

            async def act(self):
                raise AssertionError("must not act past the server limit")

        result = await run_journey_identity(Client(), now_ts=1.0, sleep=_no_sleep)
        self.assertEqual(("daily_complete", 2, 2), (result["status"], result["daily_count"], result["target"]))

    async def test_full_counter_with_overdue_reset_waits_for_the_server_to_roll(self):
        class Client:
            async def details(self):
                snapshot = _snapshot(8)
                snapshot["account"]["journey"]["wildExperience"]["resetAt"] = 1_800_000_000_000
                return snapshot

            async def act(self):
                raise AssertionError("must not act")

        stale = await run_journey_identity(Client(), now_ts=1_800_000_030, sleep=_no_sleep)
        self.assertEqual(("awaiting_reset", 120), (stale["status"], stale["retry_after"]))
        fresh = await run_journey_identity(Client(), now_ts=1_799_999_000, sleep=_no_sleep)
        self.assertEqual("daily_complete", fresh["status"])


if __name__ == "__main__":
    unittest.main()
