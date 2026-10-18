"""Focused safety tests for the separately gated six-account fishing runner."""
from __future__ import annotations

import asyncio
import json
import itertools
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

EXPECTED_ACCOUNTS = (
    "tg9100000001",
    "tg9100000002",
    "tg9100000003",
    "tg9100000004",
    "tg9100000005",
    "tg9100000006",
)
# _authenticated connects through transport's handshake gate; keep its lock files out of the checkout.
os.environ.setdefault("TG_TELEGRAM_CONNECTION_GATE_PATH", str(Path(tempfile.mkdtemp(prefix="fishing-gate-")) / "gate.lock"))

from miniapp_fishing_integration import (  # noqa: E402
    DEFAULT_CONFIG,
    UpstreamFishingProtocol,
    load_fishing_config,
)


_fixture_settlements = itertools.count()
def settled_fixture():
    return {"status": "settled_reconciled", "settlement_id": f"fixture:{next(_fixture_settlements)}", "server_result_ready": True}


class _FakeMe:
    def __init__(self, account):
        self.id = int(account.removeprefix("tg"))


class _FakeClient:
    def __init__(self, account, url="https://asc.aiopenai.app/#tgWebAppData=initData%3Dsuper-secret&tgWebAppStartParam=fish_token-1234"):
        self.account = account
        self.url = url
        self.connected = False
        self.closed = False
        self.webview_calls = []

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    @property
    def disconnected(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return _FakeMe(self.account)

    async def get_entity(self, value):
        return value

    async def get_input_entity(self, value):
        return value

    async def request_webview(self, *, bot_username, start_param):
        self.webview_calls.append((bot_username, start_param))
        return self.url

    async def disconnect(self):
        self.closed = True


class _FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return self.replies.pop(0)


def _fake_bridge(account, http_replies):
    client = _FakeClient(account)
    root = Path(tempfile.mkdtemp())
    session = root / account / "session.session"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"fixture")
    return UpstreamFishingProtocol(
        session_root=root, lock_root=root / "locks",
        client_factory=lambda **kwargs: client,
        http_transport=_FakeHttp([
            {"ok": True, "status_code": 200, "data": {"identity": {"choices": [], "selectedPlayerId": 1001}}},
            {"ok": True, "status_code": 200, "data": {"url": "https://t.me/fanrenxiuxian_bot?startapp=fish_fixture-456"}},
            *http_replies,
        ]),
        pinned_cave_entry=lambda client: {
            "url": "https://t.me/fanrenxiuxian_bot?startapp=dwelling_fixture-123",
        },
        cave_webview_request=lambda client, entry: "https://asc.aiopenai.app/#tgWebAppData=cave-init-fixture",
        session_exists=lambda path: True,
        env_loader=lambda account: {"TG_API_ID": "1", "TG_API_HASH": "hash"},
        yield_check=lambda now_ts: None,  # the 青元子 clock window must not depend on when tests run
    ), client


class FishingProtocolBridgeTests(unittest.TestCase):
    def test_dwelling_external_accepts_the_source_relative_fishing_route(self):
        bridge, _client = _fake_bridge("tg9100000001", [])
        bridge.http_transport = _FakeHttp([
            {"ok": True, "status_code": 200, "data": {"url": "/miniapp/xianxia-fishing?startapp=fish_relative-456"}},
        ])
        self.assertEqual(
            "fish_relative-456",
            asyncio.run(bridge._fishing_token_from_dwelling(init_data="x", token="dwelling-123")),
        )

    def test_login_uses_pinned_fanren_entry_webview_then_dwelling_external_fishing(self):
        account = "tg9100000001"
        root = Path(tempfile.mkdtemp())
        session = root / account / "session.session"
        session.parent.mkdir(parents=True)
        session.write_bytes(b"fixture")
        client = _FakeClient(account)
        calls = []
        pinned_entry = {
            "button_text": "洞府入口",
            "url": "https://t.me/fanrenxiuxian_bot?startapp=dwelling_approved-123",
        }
        http = _FakeHttp([
            {"ok": True, "status_code": 200, "data": {"identity": {"choices": [], "selectedPlayerId": 1001}}},
            {"ok": True, "status_code": 200, "data": {"url": "https://t.me/fanrenxiuxian_bot?startapp=fish_approved-456"}},
        ])
        bridge = UpstreamFishingProtocol(
            session_root=root,
            client_factory=lambda **kwargs: client,
            http_transport=http,
            pinned_cave_entry=lambda client: pinned_entry,
            cave_webview_request=lambda client, entry: calls.append(("dwelling", entry["bot_username"], entry["token"])) or "https://asc.aiopenai.app/#tgWebAppData=dwelling-init-secret",
            session_exists=lambda path: True,
            env_loader=lambda account: {"TG_API_ID": "1", "TG_API_HASH": "hash"},
        )

        _client, init_data, fish_token, copy_dir = asyncio.run(bridge._authenticated(account))
        asyncio.run(bridge._close_client(_client, copy_dir))

        self.assertEqual(("dwelling-init-secret", "fish_approved-456"), (init_data, fish_token))
        self.assertEqual([("dwelling", "fanrenxiuxian_bot", "dwelling_approved-123")], calls)
        self.assertEqual(["/api/miniapp/xianxia-dwelling/start", "/api/miniapp/xianxia-dwelling/external"], [
            urlparse(request["url"]).path for request in http.calls
        ])
        self.assertEqual("fishing", http.calls[1]["payload"]["action"])
        self.assertEqual(1001, http.calls[1]["payload"]["playerId"])
        self.assertTrue(client.closed)

    def test_direct_fanren_entry_requires_the_production_pinned_group(self):
        from miniapp_fishing_integration import FISHING_PINNED_ENTRY_GROUP
        self.assertEqual("@example_game_group", FISHING_PINNED_ENTRY_GROUP)
        # No direct fishing WebView callback is accepted as a discovery source;
        # discovery is always the group's pinned dwelling entry.
        bridge, _client = _fake_bridge("tg9100000001", [])
        # Test injection stands in for the pinned-message reader; it must yield
        # the canonical fanren dwelling deep link rather than a fishing URL.
        entry = bridge.pinned_cave_entry(_client)
        self.assertIn("fanrenxiuxian_bot?startapp=dwelling_", entry["url"])

    def test_pinned_entry_candidate_accepts_the_actual_hantianzun_dwelling_url(self):
        from miniapp_fishing_integration import extract_pinned_dwelling_url
        message = types.SimpleNamespace(
            message="【洞府公共入口】 https://t.me/hantianzun23_bot?startapp=df_E15ay6U3DAPZjrBNKkvyGc",
            raw_text="【洞府公共入口】 https://t.me/hantianzun23_bot?startapp=df_E15ay6U3DAPZjrBNKkvyGc",
            entities=[],
            reply_markup=None,
        )
        self.assertEqual(
            "https://t.me/hantianzun23_bot?startapp=df_E15ay6U3DAPZjrBNKkvyGc",
            extract_pinned_dwelling_url(message),
        )
        self.assertEqual(
            "hantianzun23_bot",
            UpstreamFishingProtocol._canonical_launch(
                {"url": extract_pinned_dwelling_url(message)},
                path="/api/miniapp/xianxia-dwelling",
                token_prefix="",
                error="bad",
            )["bot_username"],
        )

    def test_login_rejects_noncanonical_pinned_entry(self):
        bridge, _client = _fake_bridge("tg9100000001", [])
        bridge.pinned_cave_entry = lambda client: {
            "url": "https://asc.aiopenai.app/miniapp/xianxia-fishing?startapp=fish_direct-123",
        }

        with self.assertRaisesRegex(RuntimeError, "dwelling_entry_failed") as caught:
            asyncio.run(bridge._authenticated("tg9100000001"))
        self.assertIn("pinned_cave_entry_invalid", str(caught.exception.__cause__))
        self.assertTrue(_client.closed)

    def test_bridge_uses_a_private_session_copy_not_the_live_qyz_sqlite_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "accounts" / "tg9100000001" / "session.session"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"session-fixture")
            captured = []
            bridge = UpstreamFishingProtocol(
                session_root=root / "accounts",
                lock_root=root / "locks",
                session_copy_root=root / "copies",
                client_factory=lambda **kwargs: captured.append(kwargs["session"]) or _FakeClient("tg9100000001"),
                http_transport=_FakeHttp([
                    {"ok": True, "data": {"identity": {"choices": [], "selectedPlayerId": 1001}}},
                    {"ok": True, "data": {"url": "https://t.me/fanrenxiuxian_bot?startapp=fish_fixture-456"}},
                ]),
                pinned_cave_entry=lambda client: {"url": "https://t.me/fanrenxiuxian_bot?startapp=dwelling_fixture-123"},
                cave_webview_request=lambda client, entry: "https://asc.aiopenai.app/#tgWebAppData=cave-init-fixture",
                env_loader=lambda account: {"TG_API_ID": "1", "TG_API_HASH": "hash"},
            )
            client, _init_data, _token, copy_dir = asyncio.run(bridge._authenticated("tg9100000001"))
            asyncio.run(bridge._close_client(client, copy_dir))
            self.assertEqual(1, len(captured))
            self.assertNotEqual(str(source), captured[0])
            self.assertFalse(Path(captured[0]).exists())
            self.assertEqual(b"session-fixture", source.read_bytes())

    def test_dwelling_external_preserves_server_error_code(self):
        bridge, _ = _fake_bridge("tg9100000003", [])
        bridge.http_transport = _FakeHttp([{"ok": False, "status_code": 409, "data": {"error": "fishing_rod_missing"}}])
        with self.assertRaisesRegex(RuntimeError, "fishing_rod_missing"):
            asyncio.run(bridge._fishing_token_from_dwelling(init_data="x", token="dwelling-123", player_id=1))

    def test_pending_challenge_is_settled_with_source_derived_v2_proof(self):
        bridge, _ = _fake_bridge("tg9100000002", [
            {"ok": True, "data": {"session": {"phase": "challenge"}, "challenge": {"challengeId": "pending-1", "targetLow": 41, "targetHigh": 68, "fishPower": 1.7, "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}}},
            {"ok": True, "data": {"confirmed": True}},
            {"ok": True, "data": {"ready": True, "caught": True}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000002", {"auto": DEFAULT_CONFIG["auto"]}))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "finish", "result"], result["steps"])

    def test_one_bounded_operation_reads_shop_then_starts_and_reconciles_unknown_finish_without_retry(self):
        bridge, client = _fake_bridge("tg9100000001", [
            {"ok": True, "status_code": 200, "data": {"session": {"phase": "lobby"}}},
            {"ok": True, "status_code": 200, "data": {"shop": {"baits": [{"key": "demon_blood", "itemId": "bait-1", "count": 1}]}}},
            {"ok": True, "status_code": 200, "data": {"token": "next-token"}},
            {"ok": True, "status_code": 200, "data": {"session": {"phase": "challenge"}, "challenge": {"challengeId": "challenge-1", "targetLow": 41, "targetHigh": 68, "fishPower": 1.7, "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}}},
            {"ok": False, "status_code": 0, "error": "timeout", "error_type": "transport"},
            {"ok": True, "status_code": 200, "data": {"ready": True, "result": {"fish": "青鲤"}}},
        ])

        result = asyncio.run(bridge.run_account("tg9100000001", {"auto": {**DEFAULT_CONFIG["auto"], "bait": "demon_blood"}}))

        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "shop", "next", "start", "finish", "result"], result["steps"])
        self.assertEqual(1, result["finish_attempts"])
        # The fish_ token comes from dwelling/external; no separate fishing WebView is requested.
        self.assertEqual([], client.webview_calls)

    def test_no_new_cast_starts_while_qyz_fights(self):
        bridge, _ = _fake_bridge("tg9100000002", [{"ok": True, "data": {"session": {"phase": "lobby"}}}])
        bridge.yield_check = lambda now_ts: {"status": "yielding_to_qyz", "reason": "daily_battle_clock_window"}
        result = asyncio.run(bridge.run_account("tg9100000002", {"auto": DEFAULT_CONFIG["auto"]}))
        self.assertEqual(("yielding_to_qyz", ["start_snapshot"]), (result["status"], result["steps"]))
        self.assertEqual([], bridge.http_transport.replies)  # no shop, nest, bait or next request

    def test_zero_count_bait_requires_a_confirmed_purchase_before_next(self):
        bridge, _ = _fake_bridge("tg9100000002", [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            # Initial run_account shop and prepare_account's guarded re-check
            # both see the zero-count catalog entry before purchase.
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 0}]}}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 0}]}}},
            {"ok": True, "data": {"confirmed": True, "quantity": 10}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 10}]}}},
            {"ok": True, "data": {"token": "next-token"}},
            {"ok": True, "data": {"challenge": {"challengeId": "challenge-1", "targetLow": 41, "targetHigh": 68, "fishPower": 1.7, "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}}},
            {"ok": True, "data": {"confirmed": True}},
            {"ok": True, "data": {"ready": True, "caught": True}},
        ])
        options = {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"},
                   "buy_bait": {"enabled": True, "when_empty": True, "quantity": 10},
                   "apply_chum": {"enabled": False, "when_selected": True}}
        result = asyncio.run(bridge.run_account("tg9100000002", options))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "shop", "shop_refresh", "next", "start", "finish", "result"], result["steps"])

    def test_bait_and_chum_only_report_success_after_server_confirmation(self):
        bridge, _ = _fake_bridge("tg9100000002", [
            {"ok": True, "status_code": 200, "data": {"shop": {"baits": []}}},
            {"ok": True, "status_code": 200, "data": {"confirmed": True, "quantity": 2}},
            {"ok": True, "status_code": 200, "data": {"confirmed": True, "applied": "rice"}},
        ])

        result = asyncio.run(bridge.prepare_account("tg9100000002", {
            "auto": {"bait": "demon_blood", "chum": "rice"},
            "buy_bait": {"enabled": True, "when_empty": True, "quantity": 2},
            "apply_chum": {"enabled": True, "when_selected": True},
        }))

        self.assertEqual("confirmed", result["buy_bait"])
        self.assertEqual("confirmed", result["chum"])

ACCOUNTS = EXPECTED_ACCOUNTS


class FishingIntegrationTests(unittest.TestCase):
    def test_fishing_runtime_has_no_qyz_runtime_import(self):
        text = Path(__file__).with_name("miniapp_fishing_integration.py").read_text(encoding="utf-8")
        self.assertNotIn("from qingyuanzi_host import", text)
        self.assertNotIn("qingyuanzi-new-source", text)

    def test_default_config_follows_the_fishing_feature_and_targets_exactly_host_accounts(self):
        # Enabling fishing for an account in the accounts config is the opt-in.
        self.assertTrue(DEFAULT_CONFIG["enabled"])
        self.assertEqual(list(ACCOUNTS), DEFAULT_CONFIG["accounts"])
        self.assertEqual({"pond", "bait", "chum"}, set(DEFAULT_CONFIG["auto"]))
        self.assertIn("buy_bait", DEFAULT_CONFIG)
        self.assertIn("apply_chum", DEFAULT_CONFIG)
        self.assertNotIn("rod_transfer", DEFAULT_CONFIG)

    def test_legacy_rod_transfer_section_is_ignored_not_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fishing.json"
            path.write_text(json.dumps({**DEFAULT_CONFIG, "rod_transfer": {"enabled": True, "listing_accounts": []}}),
                            encoding="utf-8")
            self.assertNotIn("rod_transfer", load_fishing_config(path))

    def test_new_cast_waits_in_scope_until_bite_and_then_challenges(self):
        bridge, _ = _fake_bridge("tg9100000003", [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 1}]}}},
            {"ok": True, "data": {"token": "next-token"}},
            {"ok": True, "data": {"session": {"phase": "waiting", "biteAt": 1000, "serverNow": 1000}}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000003", {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"}}))
        self.assertEqual("waiting", result["status"])
        self.assertEqual(["start_snapshot", "shop", "next", "start"], result["steps"])

    def test_config_allows_the_five_non_tg752_accounts_for_plain_bait_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fishing.json"
            data = json.loads(json.dumps(DEFAULT_CONFIG))
            data["accounts"] = [account for account in EXPECTED_ACCOUNTS if account != "tg9100000001"]
            data["auto"]["bait"] = "plain"
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = load_fishing_config(path)
            self.assertEqual(data["accounts"], loaded["accounts"])
            self.assertEqual("plain", loaded["auto"]["bait"])

    def test_state_strings_use_the_shared_redactor(self):
        from miniapp_fishing_integration import _safe
        text = _safe({"error": "bad https://t.me/hantianzun23_bot?startapp=df_secret1 initData=abc signature=xyz"})
        self.assertEqual({"error": "bad [url] initData=<redacted> signature=<redacted>"}, text)

    def test_config_rejects_credential_keys_and_unknown_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fishing.json"
            path.write_text(
                json.dumps({"enabled": False, "accounts": ["not-an-account"], "api_hash": "secret"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "credentials|accounts"):
                load_fishing_config(path)

class FishingReceiptRecoveryTests(unittest.TestCase):
    def test_result_without_id_needs_a_matching_live_challenge_scope(self):
        bridge, _ = _fake_bridge("tg9100000002", [])
        response = {"ok": True, "data": {"ready": True, "caught": True}}
        self.assertIsNone(bridge._ready_result(response, "old-cast"))
        self.assertIsNone(bridge._ready_result(response, "old-cast", scope_challenge_id="new-cast"))
        self.assertIsNotNone(bridge._ready_result(response, "old-cast", scope_challenge_id="old-cast"))
        self.assertIsNotNone(bridge._ready_result(
            {"ok": True, "data": {"ready": True, "challengeId": "old-cast"}}, "old-cast"))

    def test_http_200_business_failure_is_not_confirmed(self):
        bridge, _ = _fake_bridge("tg9100000002", [])
        bridge.http_transport = _FakeHttp([{"ok": True, "data": {"ok": False, "error": "fishing_daily_limit_reached"}}])
        reply = bridge._request("next", token="fixture", init_data="fixture")
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["explicit_rejection"])

    def test_unconfirmed_finish_retries_same_challenge_after_reconciliation(self):
        challenge = {"challengeId": "retry-cast", "targetLow": 41, "targetHigh": 68,
                     "fishPower": 1.7, "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}
        start = {"ok": True, "data": {"session": {"phase": "challenge"}, "challenge": challenge}}
        pending_result = {"ok": True, "data": {"ready": False}}
        bridge, _ = _fake_bridge("tg9100000002", [start,
            {"ok": False, "data": {}, "status_code": 0, "error": "timeout"}, *[pending_result for _ in range(9)]])
        with patch("miniapp_fishing_integration.asyncio.sleep", new_callable=unittest.mock.AsyncMock), \
                patch("miniapp_fishing_integration.time.time", return_value=1000) as clock:
            first = asyncio.run(bridge.run_account("tg9100000002", {"auto": DEFAULT_CONFIG["auto"]}))
            self.assertEqual(first["status"], "finish_pending_settlement")
            bridge.http_transport.replies.extend([
                start, pending_result, {"ok": True, "data": {"confirmed": True}},
                {"ok": True, "data": {"ready": True, "challengeId": "retry-cast", "caught": True}},
            ])
            clock.return_value = 1121
            second = asyncio.run(bridge.run_account("tg9100000002", {"auto": DEFAULT_CONFIG["auto"]}))
        self.assertEqual(second["status"], "settled_reconciled")
        finishes = [request for request in bridge.http_transport.calls if request["url"].endswith("/finish")]
        self.assertEqual(len(finishes), 2)
        self.assertEqual(finishes[0]["payload"]["fishingProof"]["challengeId"], finishes[1]["payload"]["fishingProof"]["challengeId"])
        self.assertFalse(any(request["url"].endswith("/next") for request in bridge.http_transport.calls))
        saved = bridge._pending_path("tg9100000002").read_text()
        self.assertNotIn("super-secret", saved)
        self.assertNotIn("initData", saved)

    def test_official_lobby_and_scope_rejection_release_legacy_result_without_counting_it(self):
        account = "tg9100000005"
        challenge = {"challengeId": "new-cast", "targetLow": 41, "targetHigh": 68,
                     "fishPower": 1.7, "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}
        bridge, _ = _fake_bridge(account, [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            {"ok": False, "data": {"error": "fishing_token_scope"}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 1}]}}},
            {"ok": True, "data": {"token": "fish_next"}},
            {"ok": True, "data": {"token": "fish_rotated", "challenge": challenge}},
            {"ok": True, "data": {"confirmed": True}},
            {"ok": True, "data": {"result": {"ready": True, "caught": True}}},
        ])
        bridge._save_pending(account, pending=True, status="finish_pending", challenge_id="legacy-cast", retry_not_before_ts=0)
        with patch("miniapp_fishing_integration.asyncio.sleep", new_callable=unittest.mock.AsyncMock):
            result = asyncio.run(bridge.run_account(account, {"auto": {"pond": "qingxi", "bait": "plain"}}))
        self.assertEqual([f"{account}:new-cast"], result["confirmed_settlement_ids"])
        saved = json.loads(bridge._pending_path(account).read_text())
        self.assertEqual("legacy-cast", saved["unconfirmed_results"][0]["challenge_id"])
        self.assertEqual("unconfirmed", saved["unconfirmed_results"][0]["status"])
        self.assertEqual("fishing_token_scope", saved["unconfirmed_results"][0]["reason"])
        self.assertIn("unconfirmed_result_archived", result["steps"])
        finish = next(r for r in bridge.http_transport.calls if r["url"].endswith("/finish"))
        self.assertEqual("fish_rotated", finish["payload"]["token"])
        self.assertFalse(bridge._scope_path(account).exists())
        self.assertNotIn("fish_rotated", bridge._pending_path(account).read_text())

    def test_scope_rejection_in_waiting_session_never_starts_another_cast(self):
        account = "tg9100000006"
        bridge, _ = _fake_bridge(account, [
            {"ok": True, "data": {"session": {"phase": "waiting"}}},
            {"ok": False, "data": {"error": "fishing_token_scope"}},
        ])
        bridge._save_pending(account, pending=True, challenge_id="old", retry_not_before_ts=0)
        result = asyncio.run(bridge.run_account(account, {}))
        self.assertEqual("waiting", result["status"])
        self.assertEqual([], result["confirmed_settlement_ids"])
        self.assertFalse(any(r["url"].endswith("/next") for r in bridge.http_transport.calls))
        self.assertEqual("old", json.loads(bridge._pending_path(account).read_text())["unconfirmed_results"][0]["challenge_id"])

    def test_ambiguous_recovery_or_unknown_phase_cannot_release_pending(self):
        cases = [
            ("lobby", {"ok": False, "status_code": 0, "data": {}, "error": "timeout"}),
            ("lobby", {"ok": True, "data": {"result": {"ready": False}}}),
            ("lobby", {"ok": True, "data": {"result": {"ready": True, "challengeId": "another-cast"}}}),
            ("unknown", {"ok": False, "data": {"error": "fishing_token_scope"}}),
        ]
        for phase, reply in cases:
            with self.subTest(phase=phase, reply=reply):
                bridge, _ = _fake_bridge("tg9100000006", [
                    {"ok": True, "data": {"session": {"phase": phase}}}, reply])
                bridge._save_pending("tg9100000006", pending=True, challenge_id="old", retry_not_before_ts=0)
                result = asyncio.run(bridge.run_account("tg9100000006", {}))
                self.assertEqual("finish_pending_settlement", result["status"])
                self.assertEqual([], result["confirmed_settlement_ids"])
                self.assertTrue(json.loads(bridge._pending_path("tg9100000006").read_text())["pending"])
                self.assertFalse(any(r["url"].endswith("/next") for r in bridge.http_transport.calls))

    def test_restart_reuses_private_cast_scope_without_minting_a_new_external_token(self):
        account = "tg9100000006"
        original, _ = _fake_bridge(account, [])
        original._save_pending(account, pending=True, challenge_id="old", retry_not_before_ts=0)
        original._save_scope(account, "old", "fish_original_private")
        self.assertEqual(0o600, original._scope_path(account).stat().st_mode & 0o777)
        restarted, _ = _fake_bridge(account, [])
        restarted.lock_root = original.lock_root
        restarted.http_transport = _FakeHttp([
            {"ok": True, "data": {"token": "fish_rotated_private", "session": {"phase": "settled"}}},
            {"ok": True, "data": {"result": {"ready": True, "caught": True}}},
        ])
        result = asyncio.run(restarted.run_account(account, {}))
        self.assertEqual(f"{account}:old", result["settlement_id"])
        self.assertEqual(["/api/miniapp/xianxia-fishing/start", "/api/miniapp/xianxia-fishing/result"],
                         [urlparse(r["url"]).path for r in restarted.http_transport.calls])
        self.assertEqual("fish_original_private", restarted.http_transport.calls[0]["payload"]["token"])
        self.assertEqual("fish_rotated_private", restarted.http_transport.calls[1]["payload"]["token"])
        self.assertFalse(restarted._scope_path(account).exists())
        self.assertNotIn("private", restarted._pending_path(account).read_text())

    def test_cached_scope_transport_failure_keeps_credential_and_pending_result(self):
        account = "tg9100000006"
        bridge, _ = _fake_bridge(account, [])
        bridge._save_pending(account, pending=True, challenge_id="old", retry_not_before_ts=0)
        bridge._save_scope(account, "old", "fish_original")
        bridge.http_transport = _FakeHttp([{"ok": False, "data": {}, "status_code": 0, "error": "timeout"}])
        result = asyncio.run(bridge.run_account(account, {}))
        self.assertEqual("start_snapshot_failed", result["status"])
        self.assertEqual("fish_original", bridge._pending_scope_token(account))
        self.assertEqual(1, len(bridge.http_transport.calls))

    def test_closed_cast_reads_result_in_its_saved_scope(self):
        account = "tg9100000006"
        for error in ("fishing_token_used", "fishing_session_closed"):
            with self.subTest(error=error):
                bridge, _ = _fake_bridge(account, [])
                bridge._save_pending(account, pending=True, challenge_id="closed", retry_not_before_ts=0)
                bridge._save_scope(account, "closed", "fish_original")
                bridge.http_transport = _FakeHttp([
                    {"ok": False, "data": {"error": error}},
                    {"ok": True, "data": {"result": {"ready": True}}},
                ])
                result = asyncio.run(bridge.run_account(account, {}))
                self.assertEqual(f"{account}:closed", result["settlement_id"])
                self.assertIn("closed_result_recovery", result["steps"])
                self.assertFalse(bridge._scope_path(account).exists())

    def test_expired_cached_scope_refreshes_only_after_explicit_server_rejection(self):
        account = "tg9100000006"
        bridge, _ = _fake_bridge(account, [
            {"ok": True, "data": {"session": {"phase": "waiting"}}},
            {"ok": False, "data": {"error": "fishing_token_scope"}},
        ])
        bridge._save_pending(account, pending=True, challenge_id="old", retry_not_before_ts=0)
        bridge._save_scope(account, "old", "fish_expired")
        bridge.http_transport.replies.insert(0, {"ok": False, "data": {"error": "fishing_token_expired"}})
        result = asyncio.run(bridge.run_account(account, {}))
        self.assertEqual("waiting", result["status"])
        self.assertIn("start_scope_refresh", result["steps"])
        self.assertFalse(bridge._scope_path(account).exists())

    def test_other_account_scope_is_rejected_before_network(self):
        bridge, _ = _fake_bridge("tg9100000006", [])
        bridge._save_pending("tg9100000006", pending=True, challenge_id="old", retry_not_before_ts=0)
        bridge._save_scope("tg9100000006", "old", "fish_private")
        path = bridge._scope_path("tg9100000006")
        value = json.loads(path.read_text()); value["account"] = "tg9100000005"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "account mismatch"):
            asyncio.run(bridge.run_account("tg9100000006", {}))
        self.assertEqual([], bridge.http_transport.calls)

    def test_duplicate_or_transport_only_settlement_does_not_increase_daily_quota(self):
        from miniapp_fishing_integration import merge_confirmed_cast_count
        self.assertEqual(merge_confirmed_cast_count({}, {"status": "settled_reconciled"})["daily_cast_count"], 0)
        receipt = settled_fixture()
        first = merge_confirmed_cast_count({}, receipt)
        second = merge_confirmed_cast_count(first, receipt)
        self.assertEqual(first["daily_cast_count"], 1)
        self.assertEqual(second["daily_cast_count"], 1)
        self.assertEqual(merge_confirmed_cast_count(second, settled_fixture())["daily_cast_count"], 2)


if __name__ == "__main__":
    unittest.main()


class _EntryMessage:
    def __init__(self, message_id, text="", *, button_url=None, day=1):
        import datetime as _dt
        self.id = message_id
        self.raw_text = self.message = text
        self.entities = []
        self.reply_markup = None
        if button_url:
            self.reply_markup = types.SimpleNamespace(rows=[types.SimpleNamespace(
                buttons=[types.SimpleNamespace(url=button_url)])])
        self.date = _dt.datetime(2026, 9, day, tzinfo=_dt.timezone.utc)


class _EntryClient:
    """Two game groups; pinned filter, channel pin, recent scan and fixed-id reads."""
    def __init__(self, groups):
        self.groups = groups  # ref -> {"id": int, "pinned": [...], "recent": [...], "by_id": {}}
        self.calls = []

    async def get_entity(self, ref):
        self.calls.append(("get_entity", ref))
        if ref not in self.groups:
            raise ValueError("no such group")
        return types.SimpleNamespace(id=self.groups[ref]["id"], ref=ref)

    async def __call__(self, request):
        self.calls.append(("call", type(request).__name__))
        raise RuntimeError("full channel unavailable")

    async def get_messages(self, entity, limit=None, filter=None, ids=None, wait_time=None):
        self.calls.append(("get_messages", limit, ids, wait_time))
        if ids is not None:
            if isinstance(entity, int):
                peer_id = int(str(entity).removeprefix("-100"))
                group = next((g for g in self.groups.values() if g["id"] == peer_id), None)
            else:
                group = self.groups.get(getattr(entity, "ref", None))
            return group["by_id"].get(ids) if group else None
        group = self.groups[entity.ref]
        return list(group["pinned"] if filter is not None else group["recent"])


class DwellingEntryDiscoveryTests(unittest.TestCase):
    NEW = "https://t.me/hantianzun16_bot?startapp=df_pNEWTOKEN1234"
    OLD = "https://t.me/hantianzun34_bot?startapp=df_8OLDTOKEN1234"

    def _protocol(self, tmp):
        return UpstreamFishingProtocol(session_root=Path(tmp), lock_root=Path(tmp) / "locks",
                                       session_exists=lambda path: True,
                                       env_loader=lambda account: {"TG_API_ID": "1", "TG_API_HASH": "hash"})

    def test_newest_pinned_entry_across_both_groups_wins(self):
        from miniapp_fishing_integration import DWELLING_ENTRY_GROUPS
        import miniapp_fishing_integration as module
        command_group, game_group = DWELLING_ENTRY_GROUPS
        client = _EntryClient({
            command_group: {"id": 9400000004, "pinned": [_EntryMessage(1, "群规", day=20)], "recent": [], "by_id": {}},
            game_group: {"id": 9400000005,
                         "pinned": [_EntryMessage(1170936, "【洞府公共入口】", button_url=self.NEW, day=21)],
                         "recent": [_EntryMessage(1093963, "【洞府公共入口】", button_url=self.OLD, day=11)],
                         "by_id": {}},
        })
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, "DWELLING_ENTRY_CACHE_PATH", Path(tmp) / "cache.json"):
            entry = asyncio.run(self._protocol(tmp)._pinned_cave_entry(client))
            self.assertEqual("hantianzun16_bot", entry["bot_username"])
            self.assertEqual("df_pNEWTOKEN1234", entry["token"])
            cache = json.loads((Path(tmp) / "cache.json").read_text())
            self.assertEqual((cache["peer_id"], cache["message_id"], cache["source"]), (-1009400000005, 1170936, "pinned"))
            self.assertNotIn("TOKEN", json.dumps(cache))

    def test_unpinned_notice_needs_a_bot_button_and_the_newest_notice_wins(self):
        from miniapp_fishing_integration import DWELLING_ENTRY_GROUPS
        import miniapp_fishing_integration as module
        command_group, game_group = DWELLING_ENTRY_GROUPS
        forged = "https://t.me/hantianzun99_bot?startapp=df_FORGEDTOKEN99"
        client = _EntryClient({
            command_group: {"id": 9400000004, "pinned": [], "recent": [
                _EntryMessage(5, f"【洞府公共入口】 {forged}", day=22)], "by_id": {}},  # text-only: a user could post this
            game_group: {"id": 9400000005, "pinned": [], "recent": [
                _EntryMessage(1093963, "【洞府公共入口】", button_url=self.OLD, day=11),
                _EntryMessage(1170936, "【洞府公共入口】", button_url=self.NEW, day=21)], "by_id": {}},
        })
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, "DWELLING_ENTRY_CACHE_PATH", Path(tmp) / "cache.json"):
            entry = asyncio.run(self._protocol(tmp)._pinned_cave_entry(client))
            self.assertEqual("hantianzun16_bot", entry["bot_username"])

    def test_falls_back_to_cached_message_then_legacy_id_then_fails(self):
        from miniapp_fishing_integration import (DWELLING_ENTRY_GROUPS, FISHING_PINNED_ENTRY_MESSAGE_ID,
                                                 FISHING_PINNED_ENTRY_PEER_ID)
        import miniapp_fishing_integration as module
        command_group, game_group = DWELLING_ENTRY_GROUPS
        cached_msg = _EntryMessage(777, "【洞府公共入口】", button_url=self.NEW, day=15)
        legacy_msg = _EntryMessage(FISHING_PINNED_ENTRY_MESSAGE_ID, "【洞府公共入口】", button_url=self.OLD, day=1)
        groups = {command_group: {"id": 9400000004, "pinned": [], "recent": [], "by_id": {}},
                  game_group: {"id": 9400000005, "pinned": [], "recent": [], "by_id": {777: cached_msg, FISHING_PINNED_ENTRY_MESSAGE_ID: legacy_msg}}}
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            with patch.object(module, "DWELLING_ENTRY_CACHE_PATH", cache_path):
                cache_path.write_text(json.dumps({"peer_id": FISHING_PINNED_ENTRY_PEER_ID, "message_id": 777}))
                entry = asyncio.run(self._protocol(tmp)._pinned_cave_entry(_EntryClient(groups)))
                self.assertEqual("hantianzun16_bot", entry["bot_username"])
                cache_path.unlink()
                entry = asyncio.run(self._protocol(tmp)._pinned_cave_entry(_EntryClient(groups)))
                self.assertEqual("hantianzun34_bot", entry["bot_username"])
                groups[game_group]["by_id"].clear()
                with self.assertRaisesRegex(RuntimeError, "pinned_cave_entry_missing"):
                    asyncio.run(self._protocol(tmp)._pinned_cave_entry(_EntryClient(groups)))


class EntryCacheAndFloodTests(unittest.TestCase):
    NEW = DwellingEntryDiscoveryTests.NEW
    OLD = DwellingEntryDiscoveryTests.OLD

    def _groups(self, by_id=None):
        from miniapp_fishing_integration import DWELLING_ENTRY_GROUPS
        command_group, game_group = DWELLING_ENTRY_GROUPS
        return {command_group: {"id": 9400000004, "pinned": [], "recent": [], "by_id": {}},
                game_group: {"id": 9400000005, "pinned": [],
                             "recent": [_EntryMessage(1170936, "【洞府公共入口】", button_url=self.NEW, day=21)],
                             "by_id": by_id or {}}}

    def test_cached_entry_costs_one_read_and_refresh_scans_the_groups(self):
        import miniapp_fishing_integration as module
        client = _EntryClient(self._groups({777: _EntryMessage(777, "【洞府公共入口】", button_url=self.OLD, day=15)}))
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            cache_path.write_text(json.dumps({"peer_id": -1009400000005, "message_id": 777}))
            with patch.object(module, "DWELLING_ENTRY_CACHE_PATH", cache_path):
                protocol = DwellingEntryDiscoveryTests._protocol(self, tmp)
                cached = asyncio.run(protocol._dwelling_entry_candidates(client))
                self.assertEqual([("hantianzun34_bot", "cache")], [(c["bot_username"], c["source"]) for c in cached])
                self.assertEqual([("get_messages", None, 777, None)], client.calls)
                client.calls.clear()
                fresh = asyncio.run(protocol._dwelling_entry_candidates(client, refresh=True))
        self.assertEqual("hantianzun16_bot", fresh[0]["bot_username"])
        self.assertIn(("get_messages", module.DWELLING_ENTRY_SCAN_LIMIT, None, 1), client.calls)  # paced pages

    def test_flood_wait_stops_discovery_instead_of_being_swallowed(self):
        from telethon.errors import FloodWaitError
        import miniapp_fishing_integration as module

        class Flooded(_EntryClient):
            async def get_entity(self, ref):
                self.calls.append(("get_entity", ref))
                raise FloodWaitError(request=None, capture=120)

        client = Flooded(self._groups())
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, "DWELLING_ENTRY_CACHE_PATH", Path(tmp) / "none.json"):
            with self.assertRaises(FloodWaitError):
                asyncio.run(DwellingEntryDiscoveryTests._protocol(self, tmp)._dwelling_entry_candidates(client))
        self.assertEqual(1, len(client.calls))  # the second group was not tried

    def test_login_moves_past_a_rejected_cached_entry(self):
        bridge, _client = _fake_bridge("tg9100000003", [])
        bridge.pinned_cave_entry = None
        passes = []

        async def candidates(client, *, refresh=False):
            passes.append(refresh)
            if not refresh:
                return [{"url": self.OLD, "token": "df_8OLDTOKEN1234", "bot_username": "hantianzun34_bot", "source": "cache"}]
            return [{"url": self.NEW, "token": "df_pNEWTOKEN1234", "bot_username": "hantianzun16_bot", "source": "pinned",
                     "peer": -1009400000005, "message_id": 1170936, "ts": 1.0}]

        bridge._dwelling_entry_candidates = candidates
        bridge._remember_entry = lambda entry: passes.append(entry["token"])
        bridge.http_transport = _FakeHttp([
            {"ok": False, "status_code": 403, "data": {"ok": False, "error": "dwelling_token_expired"}},
            {"ok": True, "data": {"identity": {"choices": [], "selectedPlayerId": 1001}}},
            {"ok": True, "data": {"url": "https://t.me/fanrenxiuxian_bot?startapp=fish_fixture-456"}},
        ])
        client, _init, token, copy_dir = asyncio.run(bridge._authenticated("tg9100000003"))
        asyncio.run(bridge._close_client(client, copy_dir))
        self.assertEqual("fish_fixture-456", token)
        self.assertEqual([False, True, "df_pNEWTOKEN1234"], passes)
        self.assertEqual(["df_8OLDTOKEN1234", "df_pNEWTOKEN1234"],
                         [call["payload"]["token"] for call in bridge.http_transport.calls[:2]])


class LostCastRegressionTests(unittest.TestCase):
    """2026-09-23 @demo_player_04: the server counted 5 casts, 4 settled."""
    CHALLENGE = {"challengeId": "bite-1", "targetLow": 41, "targetHigh": 68, "fishPower": 1.7,
                 "minDurationMs": 100, "maxDurationMs": 70000, "fishSeed": "fixture"}

    def test_waiting_snapshot_is_reeled_in_the_same_run(self):
        bridge, _ = _fake_bridge("tg9100000004", [
            {"ok": True, "data": {"session": {"phase": "waiting", "biteAt": 1200, "serverNow": 1000}}},
            {"ok": True, "data": {"session": {"phase": "challenge"}, "challenge": dict(self.CHALLENGE)}},
            {"ok": True, "data": {"confirmed": True}},
            {"ok": True, "data": {"ready": True, "caught": True}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000004", {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"}}))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "start_bite", "finish", "result"], result["steps"])
        self.assertEqual(1, len(result["confirmed_settlement_ids"]))

    def test_lost_next_reply_with_a_live_challenge_is_reeled_immediately(self):
        bridge, _ = _fake_bridge("tg9100000004", [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 1}]}}},
            {"ok": False, "status_code": 0, "error": "response_unconfirmed", "error_type": "ReadTimeout"},
            {"ok": True, "data": {"session": {"phase": "challenge"}, "challenge": dict(self.CHALLENGE)}},
            {"ok": True, "data": {"confirmed": True}},
            {"ok": True, "data": {"ready": True, "caught": False}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000004", {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"}}))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "shop", "next", "start_after_unconfirmed_next", "finish", "result"], result["steps"])

    def test_lost_next_reply_with_a_lobby_snapshot_did_not_consume_a_cast(self):
        bridge, _ = _fake_bridge("tg9100000004", [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 1}]}}},
            {"ok": False, "status_code": 0, "error": "response_unconfirmed", "error_type": "ReadTimeout"},
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000004", {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"}}))
        self.assertEqual(("next_failed", False), (result["status"], result["cast_applied"]))
        self.assertEqual([], result["confirmed_settlement_ids"])
        self.assertEqual(0, result["unconfirmed_casts_today"])

    def test_lost_next_reply_whose_cast_already_expired_is_kept_as_evidence(self):
        bridge, _ = _fake_bridge("tg9100000004", [
            {"ok": True, "data": {"session": {"phase": "lobby"}}},
            {"ok": True, "data": {"shop": {"baits": [{"key": "plain", "itemId": "bait-1", "count": 1}]}}},
            {"ok": False, "status_code": 0, "error": "response_unconfirmed", "error_type": "ReadTimeout"},
            {"ok": True, "data": {"session": {"phase": "missed"}}},
        ])
        result = asyncio.run(bridge.run_account("tg9100000004", {"auto": {"pond": "qingxi", "bait": "plain", "chum": "none"}}))
        self.assertEqual(("cast_unconfirmed", "cast_consumed_unsettled", True),
                         (result["status"], result["error_code"], result["cast_applied"]))
        self.assertEqual(1, result["unconfirmed_casts_today"])
        pending = json.loads((bridge.lock_root / "commands" / "tg9100000004" / "pending.json").read_text())
        self.assertEqual("missed", pending["unconfirmed_casts"][0]["official_phase"])
        runs = (bridge.lock_root / "commands" / "tg9100000004" / "runs.jsonl").read_text().splitlines()
        self.assertEqual("cast_unconfirmed", json.loads(runs[-1])["status"])
        self.assertNotIn("initData", runs[-1])

