import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from miniapp_shared.dwelling import DWELLING_EXTERNAL, DWELLING_START, JsonTransport, MiniAppError
from tower_miniapp.adapter import ALLOWED_PATHS, PAGODA_START
from tower_miniapp.runner import admitted, run_account
from tower_miniapp.state import read_identity, read_state, write_state

SH = ZoneInfo("Asia/Shanghai")


def _ts(hour, minute=0):
    return datetime(2026, 9, 19, hour, minute, tzinfo=SH).timestamp()


class _Me:
    username = "demo_player_01"


class _Client:
    async def get_me(self):
        return _Me()


class _EntryProtocol:
    async def _pinned_cave_entry(self, _client):
        return {"token": "df_PARENT"}

    async def _cave_webview(self, _client, _entry):
        return "query_id=TEST&hash=SECRET"


class _Bridge:
    account = "tg9100000001"
    client = _Client()
    _entry_protocol = _EntryProtocol()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def _no_sleep(_seconds):
    return None


class TowerRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_admission_gate_is_02_00_shanghai_with_account_offsets(self):
        self.assertFalse(admitted("tg9100000001", _ts(1, 59)))
        self.assertFalse(admitted("tg9100000001", _ts(0, 35)))
        self.assertTrue(admitted("tg9100000001", _ts(2, 0)))
        self.assertTrue(admitted("tg9100000001", _ts(20, 59)))
        self.assertFalse(admitted("tg9100000006", _ts(2, 9)))
        self.assertTrue(admitted("tg9100000006", _ts(2, 10)))

    async def test_same_day_terminal_state_skips_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_state(root, "tg9100000001", {"last_terminal_day": "2026-09-19", "status": "completed"})

            def forbidden(*_args):
                raise AssertionError("bridge must not be created")

            result = await run_account(root, "tg9100000001", day_key="2026-09-19", now=_ts(22),
                                       bridge_factory=forbidden, transport=object())
        self.assertEqual("daily_already_terminal", result["status"])

    async def test_before_admission_nothing_is_opened(self):
        def forbidden(*_args):
            raise AssertionError("bridge must not be created")

        with tempfile.TemporaryDirectory() as tmp:
            result = await run_account(Path(tmp), "tg9100000006", day_key="2026-09-19", now=_ts(0, 40),
                                       bridge_factory=forbidden, transport=object())
        self.assertEqual("waiting_admission", result["status"])

    async def test_every_identity_of_the_account_is_climbed_once(self):
        challenged = []

        def post(path, payload, *, mutation):
            if path == DWELLING_START:
                return {"identity": {"selectedPlayerId": 9100000001, "choices": [
                    {"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"},
                    {"playerId": 9200000001, "daoName": "示例绑定角色", "source": "bound_character"}]}}
            if path == DWELLING_EXTERNAL:
                return {"url": f"/miniapp/xianxia-pagoda?startapp=pagoda_{payload['playerId']}"}
            if path == PAGODA_START:
                return {"state": {"canChallenge": True, "todayHighest": 0}}
            challenged.append(payload["token"])
            return {"state": {"canChallenge": False, "todayHighest": 4},
                    "replay": {"clearedCount": 4, "endFloor": 5, "failedFloor": 5, "report": "x"}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = await run_account(root, "tg9100000001", day_key="2026-09-19", now=_ts(21, 5),
                                       bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=JsonTransport(ALLOWED_PATHS, post=post), sleep=_no_sleep)
            self.assertEqual("completed", result["status"])
            self.assertEqual(["pagoda_9100000001", "pagoda_9200000001"], challenged)
            self.assertEqual("2026-09-19", read_state(root, "tg9100000001")["last_terminal_day"])
            self.assertEqual("settled_confirmed", read_identity(root, "tg9100000001", 9200000001)["status"])

            # A second tick the same day must not open Telegram again.
            def forbidden(*_args):
                raise AssertionError("bridge must not be created")

            again = await run_account(root, "tg9100000001", day_key="2026-09-19", now=_ts(22),
                                      bridge_factory=forbidden, transport=object())
            self.assertEqual("daily_already_terminal", again["status"])

    async def test_uncertain_challenge_is_aged_from_its_first_outcome_and_replayed_after_the_wait(self):
        calls = {"challenge": 0, "fail_first": True}

        def post(path, payload, *, mutation):
            if path == DWELLING_START:
                return {"identity": {"selectedPlayerId": 9100000001, "choices": [
                    {"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"}]}}
            if path == DWELLING_EXTERNAL:
                return {"url": f"/miniapp/xianxia-pagoda?startapp=pagoda_{payload['playerId']}"}
            if path == PAGODA_START:
                done = calls["challenge"] >= 2
                return {"state": {"canChallenge": not done, "todayHighest": 19 if done else 0}}
            calls["challenge"] += 1
            if calls["fail_first"]:
                calls["fail_first"] = False
                raise MiniAppError("request_failed", uncertain=True)
            return {"state": {"canChallenge": False, "todayHighest": 19},
                    "replay": {"clearedCount": 19, "endFloor": 19, "failedFloor": 20, "report": "x"}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transport = JsonTransport(ALLOWED_PATHS, post=post)
            bridge = lambda _r, _a: _Bridge()  # noqa: E731
            first = await run_account(root, "tg9100000001", day_key="2026-09-22", now=_ts(0, 41),
                                      bridge_factory=bridge, transport=transport, force=True, sleep=_no_sleep)
            self.assertEqual(("retry", "mutation_uncertain"), (first["status"], first["identities"][0]["status"]))
            record = read_identity(root, "tg9100000001", 9100000001)
            self.assertAlmostEqual(record["uncertain_since_ts"], _ts(0, 41), delta=1)
            # Five minutes later: reconcile only, the first timestamp is kept.
            second = await run_account(root, "tg9100000001", day_key="2026-09-22", now=_ts(0, 46),
                                       bridge_factory=bridge, transport=transport, force=True, sleep=_no_sleep)
            self.assertEqual("mutation_uncertain", second["identities"][0]["status"])
            self.assertEqual(1, calls["challenge"])
            self.assertAlmostEqual(read_identity(root, "tg9100000001", 9100000001)["uncertain_since_ts"], _ts(0, 41), delta=1)
            # After the replay wait the challenge is issued again and settles.
            third = await run_account(root, "tg9100000001", day_key="2026-09-22", now=_ts(0, 56),
                                      bridge_factory=bridge, transport=transport, force=True, sleep=_no_sleep)
            self.assertEqual(("completed", "settled_confirmed"), (third["status"], third["identities"][0]["status"]))
            self.assertEqual(2, calls["challenge"])
            self.assertEqual(1, read_identity(root, "tg9100000001", 9100000001)["replay_count"])

    async def test_username_mismatch_fails_the_account(self):
        class WrongMe:
            username = "intruder"

        class WrongClient:
            async def get_me(self):
                return WrongMe()

        class WrongBridge(_Bridge):
            client = WrongClient()

        with tempfile.TemporaryDirectory() as tmp:
            result = await run_account(Path(tmp), "tg9100000001", day_key="2026-09-19", now=_ts(21, 5),
                                       bridge_factory=lambda _r, _a: WrongBridge(), transport=object(), sleep=_no_sleep)
            self.assertEqual(("account_failed", "account_identity_mismatch"), (result["status"], result["error"]))
            self.assertEqual(_ts(21, 5) + 900, result["next_due_at"])

            def forbidden(*_args):
                raise AssertionError("bridge must not be reopened before the retry backoff")

            again = await run_account(Path(tmp), "tg9100000001", day_key="2026-09-19", now=_ts(21, 10),
                                      bridge_factory=forbidden, transport=object())
            self.assertEqual("scheduled_wait", again["status"])


if __name__ == "__main__":
    unittest.main()
