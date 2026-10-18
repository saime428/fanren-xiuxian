"""Operator 2026-09-28: every cast of every account is reported with what it caught."""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

from fishing_catch import extract_catch
from miniapp_fishing_integration import merge_confirmed_cast_count
from test_fishing_chum_plan import CHALLENGE, ok, options, shop
from test_miniapp_fishing_integration import _fake_bridge

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from miniapp_shared.summaries import FISHING_TEXT_LIMIT, fishing_notification  # noqa: E402


class ExtractCatchTests(unittest.TestCase):
    def test_upstream_reply_shapes(self):
        cases = [
            # fish as an object with a rarity key from session.rarityLabels
            ({"ready": True, "result": {"caught": True, "fish": {"name": "银须灵鲢", "weight": 2.88, "rarity": "spirit"},
                                        "rewards": [{"name": "幸运符", "qty": 1}]}},
             ("银须灵鲢", "灵鱼", "2.88斤", [{"name": "幸运符", "qty": 1}], True)),
            # fish as a name, grade label and weight text at the top level
            ({"ready": True, "caught": True, "fish": "青鳞小鲫", "rarityLabel": "凡鱼", "weight": "0.8斤"},
             ("青鳞小鲫", "凡鱼", "0.8斤", [], True)),
            # fishName plus bonusLoot with count
            ({"ready": True, "result": {"fishName": "赤鳞妖鲶", "grade": "妖鱼", "bonusLoot": [{"name": "一阶妖丹", "count": 2}]}},
             ("赤鳞妖鲶", "妖鱼", "", [{"name": "一阶妖丹", "qty": 2}], None)),
            # only the settlement text
            ({"ready": True, "result": {"message": "浮漂一沉，竟是一尾【金纹灵鲤】！品阶：珍鱼 重量：3.1斤 伴生机缘：【幸运符】x2"}},
             ("金纹灵鲤", "珍鱼", "3.1斤", [{"name": "幸运符", "qty": 2}], None)),
            # the shape this game's own upstream client reads (fanrenxiuxian-auto)
            ({"ready": True, "result": {"ready": True, "caught": True, "fish": {"name": "银须灵鲢", "weight": 2.8765},
                                        "rarityLabel": "灵鱼", "rarity": "spirit", "expGain": 12}},
             ("银须灵鲢", "灵鱼", "2.88斤", [], True)),
        ]
        for reply, (fish, grade, weight, rewards, caught) in cases:
            with self.subTest(fish=fish):
                catch = extract_catch(reply)
                self.assertEqual((fish, grade, weight, rewards), (catch["fish"], catch["grade"], catch["weight"], catch["rewards"]))
                self.assertIs(True, catch["caught"])

    def test_empty_cast_and_unknown_reply(self):
        for reply in ({"ready": True, "caught": False}, {"ready": True, "result": {"rarity": "miss"}},
                      {"ready": True, "result": {"ready": True, "caught": False, "message": "水下灵影擦钩而过，竟是【残影】"}}):
            with self.subTest(reply=reply):
                self.assertEqual(("", "空竿", False), (extract_catch(reply)["fish"], extract_catch(reply)["grade"],
                                                       extract_catch(reply)["caught"]))
        unknown = extract_catch({"ready": True, "caught": True, "score": 96})
        self.assertEqual(("", True), (unknown["fish"], unknown["caught"]))


class RecordedCatchTests(unittest.TestCase):
    def test_a_settled_cast_records_its_catch_and_a_redacted_reply_sample(self):
        reply = {"ready": True, "result": {"caught": True, "fish": {"name": "银须灵鲢", "weight": 2.88, "rarity": "spirit"},
                                           "rewards": [{"name": "幸运符", "qty": 1}], "token": "secret-token"}}
        data = shop(demon_used=1, grass_used=2, active={"key": "grass", "name": "灵草窝", "remaining": 3})
        bridge, _ = _fake_bridge("tg9100000001", [ok({"session": {"phase": "lobby"}}), ok(data),
                                                  ok({"token": "next-token"}), ok({"challenge": CHALLENGE}),
                                                  ok({"confirmed": True}), ok(reply)])
        result = asyncio.run(bridge.run_account("tg9100000001", options()))
        self.assertEqual("settled_reconciled", result["status"])
        self.assertEqual(("银须灵鲢", "灵鱼", "2.88斤"), (result["catch"]["fish"], result["catch"]["grade"], result["catch"]["weight"]))
        self.assertRegex(result["catch"]["at"], r"^\d\d:\d\d$")
        self.assertNotIn("result_sample", result)  # the sample goes to runs.jsonl only
        line = json.loads(bridge._pending_path("tg9100000001").with_name("runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual("银须灵鲢", line["catch"]["fish"])
        self.assertIn("银须灵鲢", line["result_sample"])
        self.assertNotIn("secret-token", line["result_sample"])

    def test_catches_accumulate_once_per_settlement_and_reset_on_a_new_day(self):
        catch = {"caught": True, "fish": "青鳞小鲫", "grade": "凡鱼", "weight": "0.8斤", "rewards": [], "at": "00:01"}
        first = merge_confirmed_cast_count({}, {"settlement_id": "a:1", "server_result_ready": True, "catch": catch},
                                           limit=None)
        second = merge_confirmed_cast_count(first, {"settlement_id": "a:2", "server_result_ready": True,
                                                    "catch": {**catch, "caught": False, "fish": "", "grade": "空竿"}},
                                            limit=None)
        again = merge_confirmed_cast_count(second, {"settlement_id": "a:2", "server_result_ready": True, "catch": catch},
                                           limit=None)
        reel_only = merge_confirmed_cast_count(again, {"status": "next_failed", "error_code": "fishing_daily_limit_reached"},
                                               limit=None)
        self.assertEqual(["a:1", "a:2"], [row["settlement_id"] for row in reel_only["catches"]])
        self.assertEqual(2, reel_only["daily_cast_count"])
        yesterday = {**reel_only, "settlement_date": "2000-01-01"}
        self.assertEqual([], merge_confirmed_cast_count(yesterday, {"status": "lobby"}, limit=None)["catches"])


class CastReportTests(unittest.TestCase):
    def rows(self, casts_per_account=5):
        fish = [{"caught": True, "fish": "青鳞小鲫", "grade": "凡鱼", "weight": "0.8斤", "rewards": [], "at": "00:01", "exp": 8},
                {"caught": False, "fish": "", "grade": "空竿", "weight": "", "rewards": [], "at": "00:03", "exp": 2},
                {"caught": True, "fish": "银须灵鲢", "grade": "灵鱼", "weight": "2.88斤",
                 "rewards": [{"name": "幸运符", "qty": 1}], "at": "00:05", "exp": 16}]
        catches = [{"settlement_id": f"s{i}", **fish[i % 3]} for i in range(casts_per_account)]
        return [{"account": "tg9100000001", "daily_cast_count": casts_per_account, "daily_cast_limit": None,
                 "server_daily_limit_reached": True, "confirmed_settlement_ids": [f"s{i}" for i in range(casts_per_account)],
                 "caught": True, "catches": catches},
                {"account": "tg9100000002", "daily_cast_count": 5, "daily_cast_limit": 5,
                 "confirmed_settlement_ids": list("abcde"), "caught": True}]

    def test_every_cast_is_listed_with_its_catch_and_a_tally(self):
        key, text = fishing_notification({"status": "daily_complete"}, self.rows(), now_ts=1790400000)
        self.assertTrue(key.endswith(":complete"))
        self.assertIn("@demo_player_01: 5 竿（服务器上限已到），今日结算 5 次\n  1. 00:01 凡鱼 青鳞小鲫 0.8斤\n  2. 00:03 空竿\n"
                      "  3. 00:05 灵鱼 银须灵鲢 2.88斤 +幸运符×1\n", text)
        self.assertIn("  4. 00:01 凡鱼 青鳞小鲫 0.8斤\n  5. 00:03 空竿\n  鱼获：凡鱼×2、空竿×2、灵鱼×1，钓术经验 +36\n", text)
        self.assertNotIn("@demo_player_01: 5 竿（服务器上限已到），今日结算 5 次，最后一竿", text)
        # An account without recorded catches keeps the old one-line summary.
        self.assertIn("@demo_player_02: 5/5 竿，今日结算 5 次，最后一竿上钩", text)

    def test_a_report_too_long_for_one_message_drops_detail_before_casts(self):
        _key, text = fishing_notification({"status": "daily_complete"}, self.rows(casts_per_account=40), now_ts=1790400000)
        self.assertLessEqual(len(text), FISHING_TEXT_LIMIT)
        self.assertIn("鱼获：", text)


if __name__ == "__main__":
    unittest.main()
