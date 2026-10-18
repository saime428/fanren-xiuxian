import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from journey_miniapp.adapter import ALLOWED_PATHS
from journey_miniapp.coordinator import run_coordinator
from journey_miniapp.runner import ACCOUNT_MINUTE_OFFSETS, admitted, run_account, seconds_since_midnight
from journey_miniapp.state import (
    ALLOWED_ACCOUNTS,
    IDENTITY_SCOPE,
    read_identity,
    read_state,
    scoped_identities,
    write_state,
)
from miniapp_shared.dwelling import DWELLING_DETAILS, DWELLING_START, JsonTransport
from tianji_trial.runtime import Identity

SH = ZoneInfo("Asia/Shanghai")


def _ts(hour, minute=0, second=0, day=20):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=SH).timestamp()


class _Me:
    username = "demo_player_05"


class _Client:
    async def get_me(self):
        return _Me()


class _EntryProtocol:
    async def _pinned_cave_entry(self, _client):
        return {"token": "df_PARENT"}

    async def _cave_webview(self, _client, _entry):
        return "query_id=TEST&hash=SECRET"


class _Bridge:
    account = "tg9100000005"
    client = _Client()
    _entry_protocol = _EntryProtocol()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def _no_sleep(_seconds):
    return None


def _wild(count, available, remaining_seconds=0, reset_at_ms=0):
    wild = {"available": available, "dailyCount": count, "dailyLimit": 8, "dailyRemaining": 8 - count,
            "remainingSeconds": remaining_seconds}
    if reset_at_ms:
        wild["resetAt"] = reset_at_ms
    return {"journey": {"wildExperience": wild}}


def _transport(board, actions, *, reset_at_ms=0):
    def post(path, payload, *, mutation):
        if path == DWELLING_START:
            return {"identity": {"choices": [{"playerId": 9100000005, "daoName": "无为子", "source": "personal"}]},
                    "account": _wild(board["count"], True, reset_at_ms=reset_at_ms)}
        if path == DWELLING_DETAILS:
            available = board["count"] == 0
            return {"account": _wild(board["count"], available, 0 if available else 9000, reset_at_ms=reset_at_ms)}
        actions.append((payload["mode"], payload["playerId"]))
        board["count"] += 1
        return {"account": _wild(board["count"], False, 9000, reset_at_ms=reset_at_ms),
                "actionResult": {"type": "wild_experience", "ok": True, "completed": True, "handled": True,
                                 "dailyCount": board["count"], "outcome": "victory"}}

    return JsonTransport(ALLOWED_PATHS, post=post)


class JourneyRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_admission_gate_is_00_15_shanghai_with_three_minute_offsets(self):
        self.assertFalse(admitted("tg9100000001", _ts(0, 14)))
        self.assertTrue(admitted("tg9100000001", _ts(0, 15)))
        self.assertFalse(admitted("tg9100000002", _ts(0, 17)))
        self.assertTrue(admitted("tg9100000002", _ts(0, 18)))
        self.assertFalse(admitted("tg9100000006", _ts(0, 29)))
        self.assertTrue(admitted("tg9100000006", _ts(0, 30)))
        self.assertEqual(15, ACCOUNT_MINUTE_OFFSETS["tg9100000006"])

    async def test_before_admission_nothing_is_opened(self):
        def forbidden(*_args):
            raise AssertionError("bridge must not be created")
        with tempfile.TemporaryDirectory() as tmp:
            result = await run_account(Path(tmp), "tg9100000006", day_key="2026-09-20", now=_ts(0, 5),
                                       bridge_factory=forbidden, transport=object())
        self.assertEqual("waiting_admission", result["status"])

    def test_scope_runs_all_six_accounts_but_only_chunyuanzi_on_demo_player_01(self):
        self.assertEqual(("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004", "tg9100000005",
                          "tg9100000006"), ALLOWED_ACCOUNTS)
        self.assertEqual({"tg9100000001": {"player_id": 9200000001, "label": "示例绑定角色"}}, IDENTITY_SCOPE)
        main = Identity(9100000001, "示例修士一", "personal")
        bound = Identity(9200000001, "示例绑定角色", "bound_character")
        self.assertEqual([bound], scoped_identities("tg9100000001", (main, bound)))
        self.assertEqual([main, bound], scoped_identities("tg9100000005", (main, bound)))
        # Label fallback only when the pinned player id is absent.
        renamed = Identity(9999, "示例绑定角色", "bound_character")
        self.assertEqual([renamed], scoped_identities("tg9100000001", (main, renamed)))
        self.assertEqual([], scoped_identities("tg9100000001", (main,)))
        with self.assertRaises(ValueError):
            write_state(Path("/nonexistent"), "tg9100000007", {})
        self.assertEqual(90, seconds_since_midnight(_ts(0, 1, 30)))

    async def test_demo_player_01_acts_only_with_chunyuanzi_and_skips_main_soul(self):
        actions = []
        counts = {9200000001: 0, 9100000001: 0}

        class VMe:
            username = "demo_player_01"

        class VClient:
            async def get_me(self):
                return VMe()

        class VBridge(_Bridge):
            account = "tg9100000001"
            client = VClient()

        def post(path, payload, *, mutation):
            if path == DWELLING_START:
                return {"identity": {"selectedPlayerId": 9100000001, "choices": [
                    {"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"},
                    {"playerId": 9200000001, "daoName": "示例绑定角色", "source": "bound_character"}]}}
            pid = payload["playerId"]
            if path == DWELLING_DETAILS:
                return {"account": _wild(counts[pid], counts[pid] < 8)}
            actions.append((payload["mode"], pid))
            counts[pid] += 1
            return {"account": _wild(counts[pid], counts[pid] < 8),
                    "actionResult": {"type": "wild_experience", "ok": True, "completed": True, "handled": True,
                                     "dailyCount": counts[pid]}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = await run_account(root, "tg9100000001", day_key="2026-09-20", now=_ts(0, 16),
                                       bridge_factory=lambda _r, _a: VBridge(),
                                       transport=JsonTransport(ALLOWED_PATHS, post=post), sleep=_no_sleep)
            self.assertEqual("daily_complete", result["status"])
            self.assertEqual([("cautious", 9200000001)] * 8, actions)
            self.assertEqual(0, counts[9100000001])
            self.assertEqual([{"player_id": 9100000001, "identity": "示例修士一", "source": "personal"}],
                             result["skipped_identities"])
            self.assertEqual(["示例绑定角色"], [item["identity"] for item in result["identities"]])
            self.assertEqual({}, read_identity(root, "tg9100000001", 9100000001))

    async def test_demo_player_01_without_chunyuanzi_fails_loudly(self):
        class VMe:
            username = "demo_player_01"

        class VClient:
            async def get_me(self):
                return VMe()

        class VBridge(_Bridge):
            account = "tg9100000001"
            client = VClient()

        def post(path, payload, *, mutation):
            if path == DWELLING_START:
                return {"identity": {"choices": [{"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"}]}}
            raise AssertionError("must not act")

        with tempfile.TemporaryDirectory() as tmp:
            result = await run_account(Path(tmp), "tg9100000001", day_key="2026-09-20", now=_ts(0, 16),
                                       bridge_factory=lambda _r, _a: VBridge(),
                                       transport=JsonTransport(ALLOWED_PATHS, post=post), sleep=_no_sleep)
        self.assertEqual(("account_failed", "scope_identity_missing"), (result["status"], result["error"]))

    async def test_first_tick_acts_then_persists_server_cooldown(self):
        board = {"count": 0}
        actions = []
        now = _ts(0, 27, 30)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = await run_account(root, "tg9100000005", day_key="2026-09-20", now=now,
                                       bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=_transport(board, actions), sleep=_no_sleep)
            self.assertEqual("waiting_server", result["status"])
            self.assertEqual([("cautious", 9100000005)], actions)
            record = read_identity(root, "tg9100000005", 9100000005)
            self.assertEqual(("cooldown", 1, now + 9000), (record["status"], record["daily_count"],
                                                            record["next_due_at"]))
            self.assertEqual(now + 9000, read_state(root, "tg9100000005")["next_due_at"])

            def forbidden(*_args):
                raise AssertionError("bridge must not be created while the server timer runs")

            again = await run_account(root, "tg9100000005", day_key="2026-09-20", now=now + 10,
                                      bridge_factory=forbidden, transport=object())
            self.assertEqual("scheduled_wait", again["status"])

    async def test_stale_full_counter_right_after_midnight_is_not_cached_as_today(self):
        board = {"count": 8}
        actions = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = await run_account(root, "tg9100000005", day_key="2026-09-20", now=_ts(0, 27, 40),
                                       bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=_transport(board, actions), sleep=_no_sleep)
            self.assertEqual("waiting_server", result["status"])
            self.assertEqual([], actions)
            record = read_identity(root, "tg9100000005", 9100000005)
            self.assertEqual(("awaiting_reset", "stale_counter_after_midnight"),
                             (record["status"], record["server_note"]))
            self.assertEqual(_ts(0, 27, 40) + 120, record["next_due_at"])

            # Later in the day the same full counter is a real completion.
            later = await run_account(root, "tg9100000005", day_key="2026-09-20", now=_ts(9, 0),
                                      bridge_factory=lambda _r, _a: _Bridge(),
                                      transport=_transport(board, actions), sleep=_no_sleep)
            self.assertEqual("daily_complete", later["status"])
            self.assertNotIn("next_due_at", later)

    async def test_server_reset_at_reopens_the_quota_on_the_same_calendar_day(self):
        reset_at = _ts(4, 0)
        board = {"count": 8}
        actions = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full = await run_account(root, "tg9100000005", day_key="2026-09-20", now=_ts(1, 0),
                                     bridge_factory=lambda _r, _a: _Bridge(),
                                     transport=_transport(board, actions, reset_at_ms=int(reset_at * 1000)),
                                     sleep=_no_sleep)
            self.assertEqual(("daily_complete", reset_at + 60), (full["status"], full["next_due_at"]))

            def forbidden(*_args):
                raise AssertionError("bridge must not be created before the server reset")

            held = await run_account(root, "tg9100000005", day_key="2026-09-20", now=_ts(3, 0),
                                     bridge_factory=forbidden, transport=object())
            self.assertEqual("daily_already_complete", held["status"])

            board["count"] = 0
            reopened = await run_account(root, "tg9100000005", day_key="2026-09-20", now=reset_at + 120,
                                         bridge_factory=lambda _r, _a: _Bridge(),
                                         transport=_transport(board, actions), sleep=_no_sleep)
            self.assertEqual("waiting_server", reopened["status"])
            self.assertEqual([("cautious", 9100000005)], actions)

    async def test_completed_day_is_cached_and_new_day_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_state(root, "tg9100000005", {"day": "2026-09-20", "status": "daily_complete"})

            def forbidden(*_args):
                raise AssertionError("bridge must not be created")

            done = await run_account(root, "tg9100000005", day_key="2026-09-20", now=_ts(12), bridge_factory=forbidden,
                                     transport=object())
            self.assertEqual("daily_already_complete", done["status"])

            opened = []

            def opening(*_args):
                opened.append(1)
                raise RuntimeError("stop here")

            new_day = await run_account(root, "tg9100000005", day_key="2026-09-21", now=_ts(12, day=21),
                                        bridge_factory=opening, transport=object())
            self.assertEqual([1], opened)
            self.assertEqual(("account_failed", "RuntimeError"), (new_day["status"], new_day["error"]))


class JourneyCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_all_six_accounts_in_order(self):
        seen = []

        async def fake_run(_root, account, **kwargs):
            seen.append(account)
            return {"account": account, "status": "daily_complete"}

        with tempfile.TemporaryDirectory() as tmp:
            results = await run_coordinator(Path(tmp), day_key="2026-09-20", now=1.0, bridge_factory=object(),
                                            transport=object(), run_account_fn=fake_run, sleep_fn=_no_sleep)
        self.assertEqual(list(ALLOWED_ACCOUNTS), seen)
        self.assertEqual("tg9100000001", seen[0])
        self.assertEqual(6, len(results))


if __name__ == "__main__":
    unittest.main()
