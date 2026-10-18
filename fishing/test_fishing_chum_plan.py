"""@demo_player_01's daily nest plan (妖腥窝, then 灵草窝 twice) and server-decided quota."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ACCOUNTS = ("tg9100000001", "tg9100000002", "tg9100000003", "tg9100000004", "tg9100000005", "tg9100000006")

from miniapp_fishing_integration import (  # noqa: E402
    UpstreamFishingProtocol, account_options, daily_quota_done, load_fishing_config, merge_confirmed_cast_count,
)
from test_miniapp_fishing_integration import _fake_bridge  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from miniapp_shared.summaries import fishing_notification  # noqa: E402

PLAN = ["demon", "grass", "grass"]
CHALLENGE = {"challengeId": "challenge-1", "targetLow": 41, "targetHigh": 68, "fishPower": 1.7,
             "minDurationMs": 5200, "maxDurationMs": 70000, "fishSeed": "fixture"}


def shop(*, demon_used=0, grass_used=0, active=None, rice_bait=0, demon_bait=97):
    """The live 2026-09-25 shop shape (keys, costs, limits) with chosen counters."""
    return {"shop": {
        "activeChum": active,
        "baits": [
            {"key": "spirit_rice", "itemId": "item_fishing_bait_spirit_rice", "name": "灵米饵", "count": rice_bait},
            {"key": "demon_blood", "itemId": "item_fishing_bait_demon_blood", "name": "妖血饵", "count": demon_bait},
        ],
        "chums": [
            {"key": "rice", "name": "米糠小窝", "dailyLimit": 2, "usedToday": 0, "remainingToday": 2, "uses": 4,
             "affordable": False, "cost": [{"itemId": "item_fishing_bait_plain", "name": "凡饵", "owned": 0, "qty": 2}]},
            {"key": "grass", "name": "灵草窝", "dailyLimit": 2, "usedToday": grass_used, "remainingToday": 2 - grass_used,
             "uses": 5, "affordable": rice_bait >= 3,
             "cost": [{"itemId": "item_fishing_bait_spirit_rice", "name": "灵米饵", "owned": rice_bait, "qty": 3},
                      {"itemId": "mat_101", "name": "凝血草", "owned": 5363, "qty": 5}]},
            {"key": "demon", "name": "妖腥窝", "dailyLimit": 1, "usedToday": demon_used, "remainingToday": 1 - demon_used,
             "uses": 6, "affordable": demon_bait >= 2,
             "cost": [{"itemId": "item_fishing_bait_demon_blood", "name": "妖血饵", "owned": demon_bait, "qty": 2},
                      {"itemId": "mat_102", "name": "一阶妖丹", "owned": 3091, "qty": 3}]},
        ],
    }}


def live_config_path() -> Path:
    """The operator's gitignored config; a checkout without it has nothing live to check."""
    path = Path(__file__).resolve().parent / "state" / "miniapp_fishing.json"
    if not path.is_file():
        raise unittest.SkipTest("live fishing config (gitignored state) is not present")
    return path


def options():
    return {"auto": {"pond": "luanxing", "bait": "demon_blood", "chum": "none"},
            "buy_bait": {"enabled": True, "when_empty": True, "quantity": 10},
            "apply_chum": {"enabled": False, "when_selected": True},
            "chum_plan": list(PLAN), "daily_cast_limit": None}


def ok(data):
    return {"ok": True, "status_code": 200, "data": data}


SETTLE = [ok({"token": "next-token"}), ok({"challenge": CHALLENGE}), ok({"confirmed": True}), ok({"ready": True, "caught": True})]


class NextPlannedChumTests(unittest.TestCase):
    def test_plan_follows_server_counters_in_order(self):
        cases = [
            (shop(), ("demon", "due")),
            (shop(demon_used=1), ("grass", "due")),
            (shop(demon_used=1, grass_used=1), ("grass", "due")),
            (shop(demon_used=1, grass_used=2), ("", "plan_complete")),
            # A live nest is never stacked, even if a plan step is due.
            (shop(demon_used=1, active={"key": "demon", "name": "妖腥窝", "remaining": 3}), ("", "active")),
            # An exhausted nest (remaining 0) lets the next step go down.
            (shop(demon_used=1, active={"key": "demon", "name": "妖腥窝", "remaining": 0}), ("grass", "due")),
            # Manual 灵草窝 first: the plan still lays 妖腥窝, then one more 灵草窝.
            (shop(grass_used=1), ("demon", "due")),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, UpstreamFishingProtocol.next_planned_chum(data, PLAN))


class ChumPlanRunTests(unittest.TestCase):
    def test_first_cast_lays_the_demon_nest_then_casts_demon_blood_at_luanxing(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop()),
            ok({"ok": True}),
            ok(shop(demon_used=1, demon_bait=95, active={"key": "demon", "name": "妖腥窝", "remaining": 6})),
            *SETTLE,
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(["start_snapshot", "shop", "chum", "chum_verify", "next", "start", "finish", "result"],
                         result["steps"])
        calls = bridge.http_transport.calls
        chum = next(call for call in calls if call["url"].endswith("/chum"))
        self.assertEqual("demon", chum["payload"]["chumKey"])
        cast = next(call for call in calls if call["url"].endswith("/next"))
        self.assertEqual(("luanxing", "item_fishing_bait_demon_blood"),
                         (cast["payload"]["pondKey"], cast["payload"]["baitItemId"]))
        self.assertEqual(("applied", "demon"), (result["chum"]["status"], result["chum"]["chum"]))

    def test_grass_nest_buys_exactly_the_missing_spirit_rice_first(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop(demon_used=1, rice_bait=0)),
            ok({"confirmed": True}),
            ok(shop(demon_used=1, rice_bait=3)),
            ok({"ok": True}),
            ok(shop(demon_used=1, grass_used=1, active={"key": "grass", "name": "灵草窝", "remaining": 5})),
            *SETTLE,
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        buy = next(call for call in bridge.http_transport.calls if call["url"].endswith("/buy-bait"))
        self.assertEqual(("spirit_rice", 3), (buy["payload"]["baitKey"], buy["payload"]["quantity"]))
        self.assertEqual(["start_snapshot", "shop", "chum_buy_bait", "chum_shop_refresh", "chum", "chum_verify",
                          "next", "start", "finish", "result"], result["steps"])
        self.assertEqual({"demon": 1, "grass": 1, "rice": 0}, result["chum"]["used_today"])

    def test_active_nest_casts_without_touching_the_chum_endpoint(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop(demon_used=1, grass_used=1, active={"key": "grass", "name": "灵草窝", "remaining": 2})),
            *SETTLE,
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertFalse(any(call["url"].endswith(("/chum", "/buy-bait")) for call in bridge.http_transport.calls))
        self.assertEqual("active", result["chum"]["status"])
        self.assertEqual("settled_reconciled", result["status"])

    def test_lost_chum_reply_without_server_evidence_never_casts(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop()),
            {"ok": False, "status_code": 0, "error": "response_unconfirmed"},
            ok(shop()),
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertEqual("chum_unconfirmed", result["status"])
        self.assertFalse(any(call["url"].endswith("/next") for call in bridge.http_transport.calls))
        self.assertGreater(result["retry_not_before_ts"], 0)

    def test_lost_chum_reply_confirmed_by_counters_is_not_sent_again(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop()),
            {"ok": False, "status_code": 0, "error": "response_unconfirmed"},
            ok(shop(demon_used=1, active={"key": "demon", "name": "妖腥窝", "remaining": 6})),
            *SETTLE,
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertEqual(1, sum(call["url"].endswith("/chum") for call in bridge.http_transport.calls))
        self.assertEqual(("applied", "settled_reconciled"), (result["chum"]["status"], result["status"]))

    def test_unaffordable_nest_is_reported_and_the_cast_still_happens(self):
        bridge, _ = _fake_bridge("tg9100000001", [
            ok({"session": {"phase": "lobby"}}),
            ok(shop(demon_used=1, rice_bait=0)),
            {"ok": False, "status_code": 200, "explicit_rejection": True, "error": "fishing_bait_unaffordable"},
            ok(shop(demon_used=1, rice_bait=0)),
            *SETTLE,
        ])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertEqual(("unaffordable", "grass"), (result["chum"]["status"], result["chum"]["chum"]))
        self.assertFalse(any(call["url"].endswith("/chum") for call in bridge.http_transport.calls))
        self.assertEqual("settled_reconciled", result["status"])


class ServerQuotaTests(unittest.TestCase):
    def test_server_quota_keeps_counting_past_five_until_the_server_refuses(self):
        state = {}
        for index in range(7):
            state = merge_confirmed_cast_count(state, {"status": "settled_reconciled", "server_result_ready": True,
                                                        "settlement_id": f"s{index}"}, limit=None)
        self.assertEqual(7, state["daily_cast_count"])
        self.assertFalse(daily_quota_done(state))
        state = merge_confirmed_cast_count(state, {"status": "next_failed",
                                                    "error_code": "fishing_daily_limit_reached"}, limit=None)
        self.assertEqual((7, True), (state["daily_cast_count"], daily_quota_done(state)))

    def test_default_accounts_keep_the_five_cast_quota(self):
        state = {}
        for index in range(5):
            state = merge_confirmed_cast_count(state, {"server_result_ready": True, "settlement_id": f"s{index}"})
        self.assertTrue(daily_quota_done(state))
        self.assertTrue(daily_quota_done({"daily_cast_count": 5}))  # results written before this change

    def test_reel_in_run_keeps_the_days_nest_evidence(self):
        previous = merge_confirmed_cast_count({}, {"chum": {"status": "active", "used_today": {"demon": 1}}}, limit=None)
        merged = merge_confirmed_cast_count(previous, {"status": "settled_reconciled"}, limit=None)
        self.assertEqual({"demon": 1}, merged["chum"]["used_today"])


class OverrideConfigTests(unittest.TestCase):
    def write(self, raw):
        path = Path(tempfile.mkdtemp()) / "fishing.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def raw(self, **override):
        return {"enabled": True, "accounts": list(ACCOUNTS),
                "account_overrides": {"tg9100000001": {"pond": "luanxing", "bait": "demon_blood",
                                                       "chum_plan": PLAN, "daily_cast_limit": "server", **override}}}

    def test_override_applies_only_to_its_account(self):
        config = load_fishing_config(self.write(self.raw()))
        mine, other = account_options(config, "tg9100000001"), account_options(config, "tg9100000002")
        self.assertEqual(({"pond": "luanxing", "bait": "demon_blood"}, PLAN, None),
                         ({k: mine["auto"][k] for k in ("pond", "bait")}, mine["chum_plan"], mine["daily_cast_limit"]))
        self.assertEqual(("qingxi", "plain", [], 5),
                         (other["auto"]["pond"], other["auto"]["bait"], other["chum_plan"], other["daily_cast_limit"]))

    def test_only_demo_player_01_may_leave_plain_bait(self):
        # Operator 2026-09-26: "除了demo_player_01其他保持凡饵", at every pond.
        raw = self.raw()
        raw["account_overrides"]["tg9100000002"] = {"bait": "spirit_rice"}
        with self.assertRaisesRegex(ValueError, "must be plain"):
            load_fishing_config(self.write(raw))
        raw = self.raw()
        raw["auto"] = {"pond": "highest_unlocked", "bait": "demon_blood", "chum": "none"}
        with self.assertRaisesRegex(ValueError, "must be plain"):
            load_fishing_config(self.write(raw))
        raw = self.raw()
        raw["account_overrides"]["tg9100000002"] = {"bait": "plain", "pond": "hantan"}
        other = account_options(load_fishing_config(self.write(raw)), "tg9100000002")
        self.assertEqual(("hantan", "plain"), (other["auto"]["pond"], other["auto"]["bait"]))

    def test_invalid_overrides_are_rejected(self):
        for bad in ({"chum_plan": ["none"]}, {"chum_plan": ["moon"]}, {"daily_cast_limit": 0},
                    {"daily_cast_limit": True}, {"bait": "gold"}, {"surprise": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_fishing_config(self.write(self.raw(**bad)))
        raw = self.raw()
        raw["accounts"] = [account for account in ACCOUNTS if account != "tg9100000001"]
        with self.assertRaises(ValueError):
            load_fishing_config(self.write(raw))

    def test_live_config_file_is_valid_and_matches_the_operator_plan(self):
        config = load_fishing_config(live_config_path())
        mine = account_options(config, "tg9100000001")
        self.assertIn("tg9100000001", config["accounts"])
        self.assertEqual(("demon_blood", PLAN, None), (mine["auto"]["bait"], mine["chum_plan"], mine["daily_cast_limit"]))
        for account in config["accounts"]:
            if account != "tg9100000001":
                self.assertEqual("plain", account_options(config, account)["auto"]["bait"], account)


class SummaryTests(unittest.TestCase):
    def test_summary_names_the_server_quota_and_the_nests(self):
        rows = [{"account": "tg9100000001", "daily_cast_count": 16, "daily_cast_limit": None,
                 "server_daily_limit_reached": True, "confirmed_settlement_ids": [f"s{i}" for i in range(16)],
                 "chum": {"status": "plan_complete", "used_today": {"rice": 0, "grass": 2, "demon": 1}}},
                {"account": "tg9100000002", "daily_cast_count": 5, "daily_cast_limit": 5,
                 "confirmed_settlement_ids": [f"t{i}" for i in range(5)]}]
        key, text = fishing_notification({"status": "daily_complete"}, rows, now_ts=1790400000)
        self.assertTrue(key.endswith(":complete"))
        self.assertIn("16 竿（服务器上限已到）", text)
        self.assertIn("打窝 灵草窝×2、妖腥窝×1", text)
        self.assertIn("5/5 竿", text)


if __name__ == "__main__":
    unittest.main()


def ponds(exp):
    return [{"key": "qingxi", "requiredExp": 0, "currentExp": exp, "unlocked": True},
            {"key": "hantan", "requiredExp": 800, "currentExp": exp, "unlocked": exp >= 800},
            {"key": "luanxing", "requiredExp": 2400, "currentExp": exp, "unlocked": exp >= 2400}]


class PondSelectionTests(unittest.TestCase):
    def test_highest_pond_the_proficiency_meets_wins(self):
        for exp, expected in ((6156, "luanxing"), (2400, "luanxing"), (2399, "hantan"), (799, "qingxi"), (0, "qingxi")):
            with self.subTest(exp=exp):
                self.assertEqual(expected, UpstreamFishingProtocol.select_pond({"shop": {"ponds": ponds(exp)}},
                                                                              "highest_unlocked"))

    def test_locked_flag_and_missing_catalog_fall_back_safely(self):
        rows = ponds(6156)
        rows[2]["unlocked"] = False
        self.assertEqual("hantan", UpstreamFishingProtocol.select_pond({"shop": {"ponds": rows}}, "highest_unlocked"))
        self.assertEqual("qingxi", UpstreamFishingProtocol.select_pond({"shop": {}}, "highest_unlocked"))
        # An explicit pond is never second-guessed.
        self.assertEqual("hantan", UpstreamFishingProtocol.select_pond({"shop": {"ponds": ponds(6156)}}, "hantan"))

    def test_cast_uses_the_selected_pond_and_reports_it(self):
        data = shop(demon_used=1, grass_used=1, active={"key": "grass", "name": "灵草窝", "remaining": 2})
        data["shop"]["ponds"] = ponds(1200)
        bridge, _ = _fake_bridge("tg9100000002", [ok({"session": {"phase": "lobby"}}), ok(data), *SETTLE])
        opts = {**options(), "auto": {"pond": "highest_unlocked", "bait": "demon_blood", "chum": "none"}, "chum_plan": []}
        result = asyncio.run(bridge.run_account("tg9100000002", opts))
        cast = next(call for call in bridge.http_transport.calls if call["url"].endswith("/next"))
        self.assertEqual(("hantan", "hantan"), (cast["payload"]["pondKey"], result["pond"]))

    def test_live_config_picks_ponds_by_proficiency_for_every_account(self):
        config = load_fishing_config(live_config_path())
        for account in config["accounts"]:
            self.assertEqual("highest_unlocked", account_options(config, account)["auto"]["pond"])
