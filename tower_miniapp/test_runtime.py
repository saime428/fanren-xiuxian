import unittest

from miniapp_shared.dwelling import MiniAppError
from tower_miniapp.runtime import run_tower_identity


async def _no_sleep(_seconds):
    return None


class TowerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_once_and_confirm_by_server_replay(self):
        class Client:
            calls = []

            async def start(self):
                self.calls.append("start")
                return {"state": {"canChallenge": True, "todayHighest": 0}}

            async def challenge(self):
                self.calls.append("challenge")
                return {"state": {"canChallenge": False, "todayHighest": 7},
                        "replay": {"clearedCount": 7, "endFloor": 8, "failedFloor": 8, "report": "止步"}}

        result = await run_tower_identity(Client(), sleep=_no_sleep)

        self.assertEqual("settled_confirmed", result["status"])
        self.assertEqual(["start", "challenge"], Client.calls)
        self.assertEqual(7, result["replay"]["cleared_count"])

    async def test_already_recorded_day_is_terminal_without_challenge(self):
        class Client:
            async def start(self):
                return {"state": {"canChallenge": False, "todayHighest": 5}}

            async def challenge(self):
                raise AssertionError("must not challenge")

        result = await run_tower_identity(Client(), sleep=_no_sleep)
        self.assertEqual(("already_recorded", True), (result["status"], result["ok"]))

    async def test_uncertain_challenge_is_reconciled_by_read_only_start(self):
        class Client:
            starts = 0
            challenges = 0

            async def start(self):
                self.starts += 1
                if self.starts >= 3:
                    return {"state": {"canChallenge": False, "todayHighest": 6}}
                return {"state": {"canChallenge": True, "todayHighest": 0}}

            async def challenge(self):
                self.challenges += 1
                raise MiniAppError("request_failed", uncertain=True)

        client = Client()
        result = await run_tower_identity(client, sleep=_no_sleep)

        self.assertEqual("reconciled_terminal", result["status"])
        self.assertEqual(1, client.challenges)

    async def test_pending_uncertain_day_never_fires_second_challenge(self):
        class Client:
            challenges = 0

            async def start(self):
                return {"state": {"canChallenge": True, "todayHighest": 0}}

            async def challenge(self):
                self.challenges += 1
                return {}

        client = Client()
        result = await run_tower_identity(client, pending_uncertain=True, sleep=_no_sleep)

        self.assertEqual("mutation_uncertain", result["status"])
        self.assertEqual(0, client.challenges)

    async def test_pending_uncertain_is_replayed_once_the_server_still_shows_no_attempt_after_the_wait(self):
        """2026-09-22 @demo_player_04: one timed-out challenge left the day unclimbed for 23 hours."""
        class Client:
            challenges = 0

            async def start(self):
                if self.challenges:
                    return {"state": {"canChallenge": False, "todayHighest": 19}}
                return {"state": {"canChallenge": True, "todayHighest": 0}}

            async def challenge(self):
                self.challenges += 1
                return {"state": {"canChallenge": False, "todayHighest": 19},
                        "replay": {"clearedCount": 19, "endFloor": 19, "failedFloor": 20, "report": "x"}}

        client = Client()
        early = await run_tower_identity(client, pending_uncertain=True, pending_age_seconds=300, sleep=_no_sleep)
        self.assertEqual(("mutation_uncertain", 0), (early["status"], client.challenges))
        late = await run_tower_identity(client, pending_uncertain=True, pending_age_seconds=900, sleep=_no_sleep)
        self.assertEqual(("settled_confirmed", 1, 1), (late["status"], client.challenges, late["replay_count"]))
        # The replay budget is bounded: a third uncertain outcome stops replaying.
        capped = await run_tower_identity(Client(), pending_uncertain=True, pending_age_seconds=9000,
                                          replay_count=2, sleep=_no_sleep)
        self.assertEqual(("mutation_uncertain", "replay_limit_reached"), (capped["status"], capped["error"]))

    async def test_explicit_server_rejection_is_not_terminal(self):
        class Client:
            async def start(self):
                return {"state": {"canChallenge": True, "todayHighest": 0}}

            async def challenge(self):
                raise MiniAppError("pagoda_power_insufficient")

        result = await run_tower_identity(Client(), sleep=_no_sleep)
        self.assertEqual(("challenge_rejected", "pagoda_power_insufficient"), (result["status"], result["error"]))


if __name__ == "__main__":
    unittest.main()
