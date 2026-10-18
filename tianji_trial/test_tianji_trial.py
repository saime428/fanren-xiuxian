import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tianji_trial.adapter import LiveAccountBridge, parse_identities
from tianji_trial.coordinator import run_account
from tianji_trial.protocol import TianjiTrialError, solve_tianji_trial_challenge
from tianji_trial.runtime import Identity, TianjiTrialRunner
from tianji_trial.transport import AccountAuth, DwellingTrialTransport

NOTIFICATIONS: list[Path] = []


def _record_notification(root: Path) -> dict:
    NOTIFICATIONS.append(root)
    return {"status": "recorded"}


def setUpModule() -> None:
    # Tests run the real coordinator against temporary roots; the operator
    # report must never leave the process (seven spurious partials on 2026-09-21).
    import os
    os.environ["MINIAPP_NOTIFY_DISABLED"] = "1"
    # Bridge connects go through transport's handshake gate; keep its locks out of the checkout.
    os.environ["TG_TELEGRAM_CONNECTION_GATE_PATH"] = str(Path(tempfile.mkdtemp(prefix="tianji-gate-")) / "gate.lock")


def tearDownModule() -> None:
    import os
    os.environ.pop("MINIAPP_NOTIFY_DISABLED", None)
    os.environ.pop("TG_TELEGRAM_CONNECTION_GATE_PATH", None)


class FakeTelegram:
    """The parts of a Telethon client that transport's connect/disconnect helpers use."""
    connected = False
    disconnected_calls = 0

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    @property
    def disconnected(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def disconnect(self):
        self.disconnected_calls += 1


class TianjiTrialProtocolTests(unittest.TestCase):
    def test_upstream_solvers_cover_all_five_modes(self) -> None:
        fixtures = [
            {
                "challengeId": "planarity",
                "mode": "tianjiPlanarityV1",
                "minDurationMs": 2200,
                "nodes": [
                    {"id": "a", "x": 10, "y": 10},
                    {"id": "b", "x": 90, "y": 90},
                    {"id": "c", "x": 90, "y": 10},
                    {"id": "d", "x": 10, "y": 90},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "c", "to": "d"},
                    {"from": "a", "to": "c"},
                    {"from": "b", "to": "d"},
                ],
            },
            {
                "challengeId": "stargaze",
                "mode": "tianjiStargazeV1",
                "stars": [
                    {"id": "sun", "angle": 20, "targetAngle": 90},
                    {"id": "moon", "angle": 180, "targetAngle": 180, "locked": True},
                ],
            },
            {
                "challengeId": "lights",
                "mode": "tianjiLightsOutV1",
                "gridSize": 4,
                "targetState": 1,
                "cells": [1] * 16,
            },
            {
                "challengeId": "memory",
                "mode": "tianjiMemoryV1",
                "cards": [
                    {"id": "a1", "pair": "a"},
                    {"id": "b1", "pair": "b"},
                    {"id": "a2", "pair": "a"},
                    {"id": "b2", "pair": "b"},
                ],
            },
            {
                "challengeId": "meridian",
                "mode": "tianjiMeridianV1",
                "sequence": ["p1", "p3", "p2"],
            },
        ]

        proofs = [solve_tianji_trial_challenge(fixture) for fixture in fixtures]

        self.assertEqual([proof["mode"] for proof in proofs], [item["mode"] for item in fixtures])
        self.assertEqual({"sun": 90.0, "moon": 180.0}, proofs[1]["angles"])
        self.assertEqual([], proofs[2]["events"])
        self.assertEqual([1] * 16, proofs[2]["cells"])
        self.assertEqual(["a1", "a2", "b1", "b2"], [event["id"] for event in proofs[3]["events"]])
        self.assertEqual(["p1", "p3", "p2"], [event["id"] for event in proofs[4]["events"]])

    def test_unknown_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(TianjiTrialError, "trial_mode_unsupported"):
            solve_tianji_trial_challenge({"challengeId": "x", "mode": "futureMode"})

    def test_planarity_preserves_locked_node(self) -> None:
        proof = solve_tianji_trial_challenge(
            {
                "challengeId": "locked-planarity",
                "mode": "tianjiPlanarityV1",
                "lockedNodeIds": ["0"],
                "nodes": [
                    {"id": "0", "x": 20, "y": 80, "locked": True},
                    {"id": "1", "x": 12, "y": 13},
                    {"id": "2", "x": 79, "y": 11},
                    {"id": "3", "x": 58, "y": 37},
                    {"id": "4", "x": 64, "y": 13},
                ],
                "edges": [
                    {"from": "0", "to": "1"},
                    {"from": "0", "to": "2"},
                    {"from": "0", "to": "3"},
                    {"from": "0", "to": "4"},
                    {"from": "1", "to": "2"},
                    {"from": "1", "to": "4"},
                    {"from": "2", "to": "3"},
                    {"from": "3", "to": "4"},
                ],
            }
        )

        self.assertEqual({"x": 20.0, "y": 80.0}, proof["positions"]["0"])
        self.assertTrue(
            all(4 <= point[axis] <= 96 for point in proof["positions"].values() for axis in ("x", "y"))
        )


class TianjiTrialResultTextTests(unittest.TestCase):
    def test_malformed_reward_fields_do_not_break_a_settled_summary(self):
        from tianji_trial.protocol import tianji_trial_result_text
        text = tianji_trial_result_text([
            {"result": {"reward_trace": 5, "bonus_trace": 1, "balance": 40, "grade": "S"}},
            {"result": {"reward_trace": "n/a", "bonus_trace": None, "balance": "?", "grade": "A"},
             "dailyProgress": {"completed": 2, "limit": 3}},
        ])
        self.assertIn("天机残痕 +5", text)
        self.assertIn("余额 40", text)
        self.assertIn("S/A", text)


class TianjiTrialAdapterTests(unittest.TestCase):
    def test_identity_choices_are_dynamic_unique_and_account_scoped(self) -> None:
        identities = parse_identities(
            {
                "identity": {
                    "selectedPlayerId": 100,
                    "choices": [
                        {"playerId": 100, "daoName": "主魂道号", "source": "personal"},
                        {"playerId": 200, "daoName": "绑定角色", "source": "bound_character"},
                    ],
                }
            }
        )

        self.assertEqual((100, 200), tuple(item.player_id for item in identities))
        self.assertEqual(("personal", "bound_character"), tuple(item.source for item in identities))

    def test_duplicate_player_identity_fails_closed(self) -> None:
        with self.assertRaisesRegex(TianjiTrialError, "miniapp_identity_invalid"):
            parse_identities(
                {
                    "identity": {
                        "choices": [
                            {"playerId": 100, "daoName": "a"},
                            {"playerId": 100, "daoName": "b"},
                        ]
                    }
                }
            )

    def test_failed_authorization_cleans_disposable_session_copy(self) -> None:
        class Client(FakeTelegram):
            async def is_user_authorized(self):
                return False

            async def get_me(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "state" / "accounts" / "tg9100000001" / "session.session"
            session.parent.mkdir(parents=True)
            session.write_bytes(b"fixture")
            client = Client()
            with (
                patch("tianji_trial.adapter.read_account_env", return_value={"TG_API_ID": "1", "TG_API_HASH": "x"}),
                patch("tianji_trial.adapter.telegram_client", return_value=client),
            ):
                bridge = LiveAccountBridge(root, "tg9100000001")
                with self.assertRaisesRegex(TianjiTrialError, "session_not_authorized"):
                    asyncio.run(bridge._connect())

            self.assertEqual(1, client.disconnected_calls)
            self.assertIsNone(bridge.client)
            self.assertIsNone(bridge.copy_dir)

    def test_client_construction_failure_removes_session_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "state" / "accounts" / "tg9100000001" / "session.session"
            session.parent.mkdir(parents=True)
            session.write_bytes(b"fixture")
            with (
                patch("tianji_trial.adapter.read_account_env", return_value={"TG_API_ID": "1", "TG_API_HASH": "x"}),
                patch("tianji_trial.adapter.telegram_client", side_effect=ValueError("TG_MTPROTO_SECRET invalid")),
            ):
                bridge = LiveAccountBridge(root, "tg9100000001")
                with self.assertRaises(TianjiTrialError) as caught:
                    asyncio.run(bridge._connect())
            self.assertEqual("telegram_connect_failed", caught.exception.code)
            self.assertEqual([], list((root / "tianji_trial" / "state" / "session_copies").iterdir()))
            self.assertIsNone(bridge.copy_dir)

    def test_close_removes_session_copy_even_when_disconnect_fails(self) -> None:
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp:
            bridge = LiveAccountBridge(Path(tmp), "tg9100000001")
            bridge.client, bridge.copy_dir = object(), Path(tempfile.mkdtemp(dir=tmp))
            copy_dir = bridge.copy_dir
            with patch("tianji_trial.adapter.disconnect_telegram_client", new=AsyncMock(side_effect=ConnectionError())):
                with self.assertRaises(ConnectionError):
                    asyncio.run(bridge.close())
            self.assertFalse(copy_dir.exists())
            self.assertEqual((None, None), (bridge.client, bridge.copy_dir))

    def test_connect_and_disconnect_go_through_root_transport(self) -> None:
        from unittest.mock import AsyncMock

        class Client(FakeTelegram):
            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return type("Me", (), {"id": 9100000001})()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "state" / "accounts" / "tg9100000001" / "session.session"
            session.parent.mkdir(parents=True)
            session.write_bytes(b"fixture")
            client = Client()
            with (
                patch("tianji_trial.adapter.read_account_env", return_value={"TG_API_ID": "1", "TG_API_HASH": "x"}),
                patch("tianji_trial.adapter.telegram_client", return_value=client) as factory,
                patch("tianji_trial.adapter.ensure_connected", new_callable=AsyncMock) as gate,
                patch("tianji_trial.adapter.disconnect_telegram_client", new_callable=AsyncMock) as close,
            ):
                async def run():
                    async with LiveAccountBridge(root, "tg9100000001"):
                        pass
                asyncio.run(run())
            gate.assert_awaited_once_with(client)
            close.assert_awaited_once_with(client)
            self.assertFalse(client.connected)  # never a bare client.connect()
            self.assertEqual({"request_retries": 1, "connection_retries": 1, "retry_delay": 1},
                             factory.call_args.kwargs)

    def test_transport_connection_error_is_redacted_and_marked_uncertain(self) -> None:
        class Client(FakeTelegram):
            async def connect(self):
                raise RuntimeError("SECRET_SESSION_VALUE")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "state" / "accounts" / "tg9100000001" / "session.session"
            session.parent.mkdir(parents=True)
            session.write_bytes(b"fixture")
            with (
                patch("tianji_trial.adapter.read_account_env", return_value={"TG_API_ID": "1", "TG_API_HASH": "x"}),
                patch("tianji_trial.adapter.telegram_client", return_value=Client()),
            ):
                bridge = LiveAccountBridge(root, "tg9100000001")
                with self.assertRaises(TianjiTrialError) as caught:
                    asyncio.run(bridge._connect())

            self.assertEqual("telegram_connect_failed", caught.exception.code)
            self.assertTrue(caught.exception.uncertain)
            self.assertNotIn("SECRET_SESSION_VALUE", str(caught.exception))

    def test_dwelling_discovery_error_is_redacted(self) -> None:
        class EntryProtocol:
            async def _pinned_cave_entry(self, _client):
                raise RuntimeError("SECRET_ENTRY_VALUE")

        with tempfile.TemporaryDirectory() as tmp:
            bridge = LiveAccountBridge(Path(tmp), "tg9100000001")
            bridge.client = object()
            bridge._entry_protocol = EntryProtocol()
            with self.assertRaises(TianjiTrialError) as caught:
                asyncio.run(bridge.authenticate())

        self.assertEqual("dwelling_entry_failed", caught.exception.code)
        self.assertNotIn("SECRET_ENTRY_VALUE", str(caught.exception))

    def test_authentication_moves_past_a_rotated_entry(self) -> None:
        class EntryProtocol:
            pinned_cave_entry = None

            async def _dwelling_entry_candidates(self, _client):
                return [{"token": "df_STALE1", "source": "pinned"}, {"token": "df_FRESH1", "source": "notice"}]

            async def _cave_webview(self, _client, entry):
                return f"init-{entry['token']}"

            def _remember_entry(self, entry):
                pass

        seen = []

        def http(path, payload, **_kwargs):
            seen.append(payload["token"])
            if payload["token"] == "df_STALE1":
                raise TianjiTrialError("dwelling_token_invalid")  # what post_json raises for an ok:false reply
            return {"identity": {"choices": [{"playerId": 100, "daoName": "主魂", "source": "personal"}]}}

        with tempfile.TemporaryDirectory() as tmp:
            bridge = LiveAccountBridge(Path(tmp), "tg9100000001", http_transport=http)
            bridge.client = object()
            bridge._entry_protocol = EntryProtocol()
            auth = asyncio.run(bridge.authenticate())

        self.assertEqual(["df_STALE1", "df_FRESH1"], seen)
        self.assertEqual(("df_FRESH1", "init-df_FRESH1"), (auth.dwelling_token, auth.init_data))


class DwellingTrialTransportTests(unittest.TestCase):
    def test_start_mints_identity_scoped_trial_token(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def auth_provider(force: bool = False) -> AccountAuth:
            self.assertFalse(force)
            return AccountAuth(init_data="signed", dwelling_token="dwelling", identities=())

        async def post_json(path: str, payload: dict):
            calls.append((path, dict(payload)))
            if path.endswith("/external"):
                return {"url": "/miniapp/xianxia-trial?startapp=trial_fixture"}
            return {"dailyProgress": {"completed": 0, "limit": 3}, "challenge": {"challengeId": "c", "mode": "tianjiMeridianV1", "sequence": ["p1"]}}

        transport = DwellingTrialTransport(auth_provider, post_json)
        result = asyncio.run(transport.start(123))

        self.assertEqual("c", result["challenge"]["challengeId"])
        self.assertEqual("tianji_trial", calls[0][1]["action"])
        self.assertEqual(123, calls[0][1]["playerId"])
        self.assertEqual("trial_fixture", calls[1][1]["token"])

    def test_start_refreshes_webview_and_trial_token_once_on_auth_expiry(self) -> None:
        auth_calls: list[bool] = []
        external_count = 0

        async def auth_provider(force: bool = False) -> AccountAuth:
            auth_calls.append(force)
            suffix = "new" if force else "old"
            return AccountAuth(init_data=f"signed-{suffix}", dwelling_token=f"dwelling-{suffix}", identities=())

        async def post_json(path: str, payload: dict):
            nonlocal external_count
            if path.endswith("/external"):
                external_count += 1
                return {"url": f"/miniapp/xianxia-trial?startapp=trial_{external_count}"}
            if payload["token"] == "trial_1":
                raise TianjiTrialError("trial_token_expired")
            return {"dailyProgress": {"completed": 3, "limit": 3}}

        result = asyncio.run(DwellingTrialTransport(auth_provider, post_json).start(123))

        self.assertEqual(3, result["dailyProgress"]["completed"])
        self.assertEqual([False, True], auth_calls)
        self.assertEqual(2, external_count)

    def test_finish_never_refreshes_or_replays_old_proof(self) -> None:
        auth_calls = 0
        external_count = 0

        async def auth_provider(force: bool = False) -> AccountAuth:
            nonlocal auth_calls
            auth_calls += 1
            return AccountAuth(init_data="signed", dwelling_token="dwelling", identities=())

        async def post_json(path: str, payload: dict):
            nonlocal external_count
            if path.endswith("/external"):
                external_count += 1
                return {"url": "/miniapp/xianxia-trial?startapp=trial_fixture"}
            if path.endswith("/start"):
                return {"dailyProgress": {"completed": 0, "limit": 3}, "challenge": {"challengeId": "c", "mode": "tianjiMeridianV1", "sequence": ["p1"]}}
            raise TianjiTrialError("hash_mismatch", uncertain=True)

        transport = DwellingTrialTransport(auth_provider, post_json)
        asyncio.run(transport.start(123))
        with self.assertRaisesRegex(TianjiTrialError, "hash_mismatch"):
            asyncio.run(transport.finish(123, {"challengeId": "c", "mode": "tianjiMeridianV1", "durationMs": 1000, "events": []}))

        self.assertEqual(1, auth_calls)
        self.assertEqual(1, external_count)


class TianjiTrialCoordinatorTests(unittest.TestCase):
    def test_scheduler_wrapper_accepts_only_the_six_business_accounts(self) -> None:
        text = Path("scripts/run_tianji_trial_account.sh").read_text(encoding="utf-8")

        for account in (
            "tg9100000001",
            "tg9100000002",
            "tg9100000003",
            "tg9100000004",
            "tg9100000005",
            "tg9100000006",
        ):
            self.assertIn(account, text)
        self.assertNotIn("tg9100000007)", text)
        self.assertIn('-m tianji_trial.coordinator --account "$account"', text)

    def test_account_waits_until_its_shanghai_daily_slot(self) -> None:
        class Bridge:
            def __init__(self, *_args):
                raise AssertionError("bridge must not open before daily slot")

        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_account(
                    Path(tmp),
                    "tg9100000001",
                    now=datetime(2026, 9, 10, 1, 29, tzinfo=ZoneInfo("Asia/Shanghai")),
                    bridge_factory=Bridge,
                    notify=_record_notification,
                )
            )

        self.assertEqual("waiting", result["status"])
        self.assertEqual("2026-09-10T01:30:00+08:00", result["scheduled_at"])

    def test_account_completes_every_dynamic_identity_and_then_skips_today(self) -> None:
        class Bridge:
            calls: list[tuple[int, str]] = []

            def __init__(self, _project_root: Path, account: str) -> None:
                self.account = account
                self.progress: dict[int, int] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _tb):
                return None

            async def authenticate(self, force: bool = False):
                self.assert_not_force(force)
                return AccountAuth(
                    init_data="signed",
                    dwelling_token="dwelling",
                    identities=(
                        Identity(100, "主魂", "personal"),
                        Identity(200, "绑定角色", "bound_character"),
                    ),
                )

            async def post_json(self, path: str, payload: dict):
                player_id = 100 if payload.get("token") == "trial_100" else 200
                if path.endswith("/external"):
                    player_id = int(payload["playerId"])
                    return {"url": f"/miniapp/xianxia-trial?startapp=trial_{player_id}"}
                if path.endswith("/start"):
                    Bridge.calls.append((player_id, "start"))
                    return {
                        "dailyProgress": {"completed": 0, "limit": 1},
                        "challenge": {"challengeId": f"c-{player_id}", "mode": "tianjiMeridianV1", "minDurationMs": 350, "sequence": ["p1"]},
                    }
                Bridge.calls.append((player_id, "finish"))
                return {
                    "dailyProgress": {"completed": 1, "limit": 1},
                    "result": {"daily_progress": 1, "daily_limit": 1, "grade": "甲等"},
                }

            @staticmethod
            def assert_not_force(force: bool) -> None:
                if force:
                    raise AssertionError("unexpected refresh")

        async def no_sleep(_seconds: float) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "tianji_trial/state/accounts/tg9100000001/summary.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(json.dumps({"account": "tg9100000001", "status": "completed",
                                           "last_date": "2026-09-10", "identities": []}))
            first = asyncio.run(
                run_account(
                    root,
                    "tg9100000001",
                    today="2026-09-10",
                    now=datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    bridge_factory=Bridge,
                    notify=_record_notification,
                    sleep=no_sleep,
                )
            )
            second = asyncio.run(
                run_account(
                    root,
                    "tg9100000001",
                    today="2026-09-10",
                    now=datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    bridge_factory=Bridge,
                    notify=_record_notification,
                    sleep=no_sleep,
                )
            )

        self.assertEqual("completed", first["status"])
        self.assertEqual("done", second["status"])
        self.assertEqual([(100, "start"), (100, "finish"), (200, "start"), (200, "finish")], Bridge.calls)


class CoordinatorReportTests(unittest.TestCase):
    def test_report_runs_after_the_telegram_client_is_closed(self) -> None:
        events = []

        class Bridge:
            def __init__(self, _root, _account):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                events.append("closed")

            async def authenticate(self, force=False):
                return AccountAuth(init_data="signed", dwelling_token="dwelling", identities=(Identity(100, "主魂"),))

            async def post_json(self, path, payload):
                if path.endswith("/external"):
                    return {"url": "/miniapp/xianxia-trial?startapp=trial_100"}
                return {"dailyProgress": {"completed": 3, "limit": 3}}

        def notify(root):
            events.append("notified")
            return {"status": "recorded"}

        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_account(Path(tmp), "tg9100000001", today="2026-09-10",
                                             now=datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                                             bridge_factory=Bridge, notify=notify))
        self.assertEqual(["closed", "notified"], events)
        self.assertEqual(("completed", {"status": "recorded"}), (result["status"], result["notification"]))


class TianjiTrialRunnerTests(unittest.TestCase):
    def test_runs_every_server_returned_challenge_before_success(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.finished: list[str] = []

            async def start(self, player_id: int):
                self.assert_player(player_id)
                return {
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": "trial-1",
                        "mode": "tianjiMeridianV1",
                        "minDurationMs": 350,
                        "sequence": ["p1"],
                    },
                }

            async def finish(self, player_id: int, proof: dict):
                self.assert_player(player_id)
                self.finished.append(proof["challengeId"])
                completed = len(self.finished)
                payload = {
                    "dailyProgress": {"completed": completed, "limit": 3},
                    "result": {
                        "grade": "甲等",
                        "reward_trace": 5,
                        "daily_progress": completed,
                        "daily_limit": 3,
                        "balance": completed * 5,
                    },
                }
                if completed < 3:
                    payload["nextChallenge"] = {
                        "challengeId": f"trial-{completed + 1}",
                        "mode": "tianjiMeridianV1",
                        "minDurationMs": 350,
                        "sequence": [f"p{completed + 1}"],
                    }
                return payload

            @staticmethod
            def assert_player(player_id: int) -> None:
                if player_id != 100:
                    raise AssertionError(player_id)

        async def no_sleep(_seconds: float) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport()
            runner = TianjiTrialRunner(Path(tmp), transport, sleep=no_sleep)
            result = asyncio.run(
                runner.run_identity(Identity(player_id=100, label="主魂"), today="2026-09-10")
            )
            state = json.loads((Path(tmp) / "identities" / "100.json").read_text(encoding="utf-8"))

        self.assertEqual("completed", result["status"])
        self.assertEqual(["trial-1", "trial-2", "trial-3"], transport.finished)
        self.assertEqual(3, state["completed"])
        self.assertEqual(3, state["limit"])
        self.assertEqual("2026-09-10", state["last_date"])


class TrialStartContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def challenge(challenge_id):
        return {"challengeId": challenge_id, "mode": "tianjiMeridianV1",
                "minDurationMs": 350, "sequence": ["p1"]}

    @staticmethod
    async def no_sleep(_seconds):
        pass

    async def test_challenge_only_start_completes_from_official_finish_progress(self):
        from unittest.mock import AsyncMock
        start = {"ok": True, "challenge": self.challenge("first")}
        settlements = []
        for count, challenge_id in enumerate(("first", "second", "third"), 1):
            settled = {"dailyProgress": {"completed": count, "limit": 3},
                       "result": {"challengeId": challenge_id, "grade": "甲等"}}
            if count < 3:
                settled["nextChallenge"] = self.challenge(("second", "third")[count - 1])
            settlements.append(settled)
        transport = type("Transport", (), {"start": AsyncMock(return_value=start),
                                           "finish": AsyncMock(side_effect=settlements)})()
        with tempfile.TemporaryDirectory() as directory:
            result = await TianjiTrialRunner(Path(directory), transport, sleep=self.no_sleep).run_identity(
                Identity(100, "主魂"), today="2026-09-14")
            state = json.loads((Path(directory) / "identities/100.json").read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual((state["completed"], state["limit"]), (3, 3))
        self.assertTrue(state["receipt_confirmed"])
        self.assertIsNone(state["pending_finish"])
        self.assertEqual([call.args[1]["challengeId"] for call in transport.finish.await_args_list],
                         ["first", "second", "third"])

    async def test_unknown_start_count_can_resume_at_the_last_server_stage(self):
        from unittest.mock import AsyncMock
        transport = type("Transport", (), {
            "start": AsyncMock(return_value={"challenge": self.challenge("last")}),
            "finish": AsyncMock(return_value={"result": {"challengeId": "last", "daily_progress": 3,
                                                         "daily_limit": 3, "grade": "甲等"}}),
        })()
        with tempfile.TemporaryDirectory() as directory:
            result = await TianjiTrialRunner(Path(directory), transport, sleep=self.no_sleep).run_identity(
                Identity(100, "主魂"), today="2026-09-14")
        self.assertEqual(result["status"], "completed")
        transport.finish.assert_awaited_once()

    async def test_missing_finish_progress_keeps_unknown_pending_and_obeys_backoff(self):
        from unittest.mock import AsyncMock, patch
        transport = type("Transport", (), {
            "start": AsyncMock(return_value={"challenge": self.challenge("pending")}),
            "finish": AsyncMock(return_value={"result": {"grade": "甲等"}}),
        })()
        with tempfile.TemporaryDirectory() as directory, patch("tianji_trial.runtime.time.time", return_value=1000):
            runner = TianjiTrialRunner(Path(directory), transport, sleep=self.no_sleep)
            result = await runner.run_identity(Identity(100, "主魂"), today="2026-09-14")
            state = json.loads((Path(directory) / "identities/100.json").read_text())
            self.assertEqual(result["status"], "retry")
            self.assertEqual(state["last_error"], "trial_progress_unconfirmed")
            self.assertFalse(state["receipt_confirmed"])
            self.assertIsNone(state["pending_finish"]["completed_before"])
            self.assertIsNone(state["pending_finish"]["limit"])
            self.assertNotIn("last_date", state)
            result = await TianjiTrialRunner(Path(directory), transport, sleep=self.no_sleep).run_identity(
                Identity(100, "主魂"), today="2026-09-14")
            self.assertEqual(result["error"], "finish_reply_pending")
            transport.finish.assert_awaited_once()
            transport.start.return_value = {"dailyProgress": {"completed": 3, "limit": 3}}
            recovered = await runner.run_identity(Identity(100, "主魂"), today="2026-09-14")
            self.assertEqual(recovered["status"], "already")
            transport.finish.assert_awaited_once()

    async def test_invalid_start_never_submits_or_preserves_yesterday_as_confirmed(self):
        from unittest.mock import AsyncMock
        for payload in ({}, {"challenge": {}},
                        {"ok": False, "challenge": self.challenge("bad")},
                        {"dailyProgress": {"completed": 0}, "challenge": self.challenge("bad")},
                        {"dailyProgress": {"completed": True, "limit": 3}, "challenge": self.challenge("bad")},
                        {"challenge": {"challengeId": "bad", "mode": "unsupported"}},
                        # a solver hitting malformed data raised a bare ValueError
                        {"challenge": {"challengeId": "bad", "mode": "tianjiLightsOutV1", "gridSize": "four"}}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "identities/100.json"
                path.parent.mkdir()
                path.write_text(json.dumps({"player_id": 100, "last_date": "2026-09-13",
                    "receipt_confirmed": True, "status": "completed", "completed": 3, "limit": 3}))
                transport = type("Transport", (), {"start": AsyncMock(return_value=payload),
                                                   "finish": AsyncMock()})()
                with self.assertRaises(TianjiTrialError):
                    await TianjiTrialRunner(root, transport, sleep=self.no_sleep).run_identity(
                        Identity(100, "主魂"), today="2026-09-14")
                state = json.loads(path.read_text())
                self.assertEqual(state["status"], "retry")
                self.assertFalse(state["receipt_confirmed"])
                self.assertEqual(state["attempt_date"], "2026-09-14")
                transport.finish.assert_not_awaited()

    async def test_wrong_finish_identity_or_nonadvancing_count_is_unconfirmed(self):
        from unittest.mock import AsyncMock
        for settled in (
            {"dailyProgress": {"completed": 1, "limit": 3}, "result": {"challengeId": "other"}},
            {"dailyProgress": {"completed": 0, "limit": 3}, "result": {"grade": "甲等"}},
            {"dailyProgress": {"completed": 3, "limit": 3}},
        ):
            with self.subTest(settled=settled), tempfile.TemporaryDirectory() as directory:
                transport = type("Transport", (), {
                    "start": AsyncMock(return_value={"challenge": self.challenge("expected")}),
                    "finish": AsyncMock(return_value=settled),
                })()
                result = await TianjiTrialRunner(Path(directory), transport, sleep=self.no_sleep).run_identity(
                    Identity(100, "主魂"), today="2026-09-14")
                self.assertEqual(result["status"], "retry")
                state = json.loads((Path(directory) / "identities/100.json").read_text())
                self.assertFalse(state["receipt_confirmed"])
                self.assertEqual(state["pending_finish"]["challenge_id"], "expected")


class TrialReceiptRetryTests(unittest.TestCase):
    def test_missing_progress_never_defaults_to_a_successful_or_available_state(self):
        from tianji_trial.protocol import tianji_trial_progress
        for payload in ({}, {"dailyProgress": {"completed": 3}},
                        {"dailyProgress": {"completed": True, "limit": 3}}):
            with self.assertRaises(TianjiTrialError):
                tianji_trial_progress(payload)

    def test_unknown_finish_recovers_from_official_progress_after_restart(self):
        from unittest.mock import patch
        class Transport:
            def __init__(self):
                self.finishes = 0
                self.complete = False
            async def start(self, player_id):
                if self.complete:
                    return {"dailyProgress": {"completed": 3, "limit": 3}}
                return {"dailyProgress": {"completed": 2, "limit": 3},
                        "challenge": {"challengeId": "last", "mode": "tianjiMeridianV1",
                                      "minDurationMs": 350, "sequence": ["p1"]}}
            async def finish(self, player_id, proof):
                self.finishes += 1
                raise TianjiTrialError("transport_unknown", uncertain=True)
        async def no_sleep(_seconds):
            pass
        with tempfile.TemporaryDirectory() as directory:
            transport = Transport()
            identity = Identity(player_id=100, label="主魂")
            with patch("tianji_trial.runtime.time.time", return_value=1000):
                first = asyncio.run(TianjiTrialRunner(Path(directory), transport, sleep=no_sleep).run_identity(identity, today="2026-09-13"))
            self.assertEqual(first["status"], "retry")
            saved = json.loads((Path(directory) / "identities/100.json").read_text())
            self.assertFalse(saved["receipt_confirmed"])
            self.assertEqual(saved["pending_finish"]["challenge_id"], "last")
            with patch("tianji_trial.runtime.time.time", return_value=1121):
                asyncio.run(TianjiTrialRunner(Path(directory), transport, sleep=no_sleep).run_identity(identity, today="2026-09-13"))
            self.assertEqual(transport.finishes, 2)
            transport.complete = True
            recovered = asyncio.run(TianjiTrialRunner(Path(directory), transport, sleep=no_sleep).run_identity(identity, today="2026-09-13"))
            self.assertEqual(recovered["status"], "already")
            self.assertEqual(transport.finishes, 2)


if __name__ == "__main__":
    unittest.main()
