import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import qyz_battle_report as report

SH = timezone(timedelta(hours=8))
FP = "617b147e0a62ef8293e4dae358a7ea9d836b46783144f0cee7d4f6bc1f74445f"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _journal(root: Path, account: str, record: dict) -> None:
    _write(root / "state" / "accounts" / account / "world_boss_state.json",
           {"world_boss_events": [{"fingerprint": "old", "status": "settled_perfect"}, {"fingerprint": FP, **record}]})


class BattleReportTests(unittest.TestCase):
    def test_damage_uses_the_reference_units_and_hides_zero(self):
        self.assertEqual(report._damage_text(677568117.0), "6.78亿亿")
        self.assertEqual(report._damage_text(1234), "1,234亿")
        self.assertEqual([report._damage_text(value) for value in (0, None, "x", float("nan"))], [""] * 4)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root / "state" / "world_boss_shared_entry.json",
               {"chat_id": -1009400000004, "message_id": 12406242, "fingerprint": FP,
                "published_at": "2026-09-19T13:40:12"})
        self.entry_ts = datetime(2026, 9, 19, 13, 40, 12, tzinfo=SH).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_waits_until_every_account_is_terminal_then_sends_once(self):
        settled = {"status": "settled_partial", "expected_windows": 16, "accepted_hits": 16, "server_perfects": 14,
                   "confirmed_phases": [{"phase": "begin_confirmed", "at": self.entry_ts + 98.2}],
                   "protocol_result": {"finish_confirmed": True, "server_damage_multiplier_min": 1.0,
                                       "server_result": {"grade": "甲等", "score": 100, "realtime_damage_yi": 677568117.0}}}
        failed = {"status": "runtime_error", "error": "world_boss_turnstile_timeout", "expected_windows": 16,
                  "protocol_result": {"finish_confirmed": False, "phase": "begin_submitted",
                                      "diagnostics": {"browser": {"browser_event": "widget_timeout"}}}}
        for account in report.ACCOUNTS:
            _journal(self.root, account, settled if account == "tg9100000001" else failed)
        # One account still fighting: nothing is sent yet.
        _journal(self.root, "tg9100000006", {"status": "hit_confirmed", "mutation_started": True, "expected_windows": 16})
        sent = []

        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": 9, "success": True}

        self.assertEqual("not_due", report.run_once(self.root, now_ts=self.entry_ts + 240, sender=sender)["status"])
        _journal(self.root, "tg9100000006", failed)
        outcome = report.run_once(self.root, now_ts=self.entry_ts + 250, sender=sender)
        self.assertEqual(("delivered", f"{FP[:16]}:final"), (outcome["status"], outcome["key"]))
        text = sent[0]
        self.assertIn("【青元子战报】09-19 13:40 入口 12406242：开战 1/6，正式结算 1/6，命中 16/96，PERFECT 14", text)
        self.assertIn("@demo_player_01: 部分完美 命中 16/16 PERFECT 14 甲等 100分 伤害 6.78亿亿，开战 +98s", text)
        self.assertIn("@demo_player_06: 未开战，world_boss_turnstile_timeout（begin_submitted），浏览器 widget_timeout", text)
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=self.entry_ts + 300, sender=sender)["status"])
        self.assertEqual(1, len(sent))

    def test_deadline_summary_is_sent_when_an_account_never_finishes(self):
        for account in report.ACCOUNTS:
            _journal(self.root, account, {"status": "battle_in_progress", "mutation_started": True, "expected_windows": 16,
                                          "accepted_hits": 3, "server_perfects": 3})
        sent = []

        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": 10, "success": True}

        self.assertEqual("not_due", report.run_once(self.root, now_ts=self.entry_ts + 600, sender=sender)["status"])
        outcome = report.run_once(self.root, now_ts=self.entry_ts + 901, sender=sender)
        self.assertEqual(f"{FP[:16]}:deadline", outcome["key"])
        self.assertIn("（超时汇总，含无记录或未结束账号）", sent[0])
        self.assertIn("@demo_player_02: 进行中（battle_in_progress）", sent[0])
        # A later final for the same event does not send a second message.
        for account in report.ACCOUNTS:
            _journal(self.root, account, {"status": "settled_perfect", "expected_windows": 16, "accepted_hits": 16,
                                          "server_perfects": 16, "protocol_result": {"finish_confirmed": True,
                                                                                   "server_result": {"grade": "甲等", "score": 100}}})
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=self.entry_ts + 1000, sender=sender)["status"])
        self.assertEqual(1, len(sent))

    def test_newer_official_board_without_an_admitted_entry_reports_the_missed_event_once(self):
        """2026-09-21: the host dropped the entry, journals stayed on 09-20, only the board arrived."""
        _write(self.root / "state" / "world_boss_shared_entry.json",
               {"chat_id": -1009400000004, "message_id": 12421535, "fingerprint": FP,
                "published_at": "2026-09-20T13:40:15"})
        for account in report.ACCOUNTS:
            _journal(self.root, account, {"status": "settled_partial", "expected_windows": 16, "accepted_hits": 16,
                                          "server_perfects": 15, "protocol_result": {"finish_confirmed": True,
                                                                                   "server_result": {"grade": "甲等", "score": 100}}})
        _write(self.root / "state" / "world_boss_shared_board.json",
               {"chat_id": -1009400000004, "message_id": 12436634, "published_at": "2026-09-21T13:47:37",
                "participants": 30, "outcome": "天道败退", "row_count": 2,
                "rows": [{"rank": 1, "username": "sample_peer_16", "score": 3760, "attacks": 16, "damage": "7.42亿亿"},
                         {"rank": 2, "username": "sample_peer_10", "score": 3760, "attacks": 16, "damage": "7.38亿亿"}]})
        sent = []

        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": 11, "success": True}

        now = datetime(2026, 9, 21, 13, 48, tzinfo=SH).timestamp()
        outcome = report.run_once(self.root, now_ts=now, sender=sender)
        self.assertEqual(("delivered", "board:12436634:missed"), (outcome["status"], outcome["key"]))
        self.assertIn("【青元子战报】09-21 13:47 结算榜 12436634：未记录对应入口，本场参战情况待核实", sent[0])
        self.assertIn("贡献榜前三（参战 30 人）：1. @sample_peer_16 3760分 强攻16 伤害7.42亿亿", sent[0])
        self.assertIn("本方上榜：无（前 2 名内无本方账号）", sent[0])
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=now + 60, sender=sender)["status"])
        self.assertEqual(1, len(sent))
        # The 09-20 board (same day as the entry) never triggers a missed-event report.
        _write(self.root / "state" / "world_boss_shared_board.json",
               {"chat_id": -1009400000004, "message_id": 12421596, "published_at": "2026-09-20T13:47:17",
                "rows": [{"rank": 1, "username": "sample_peer_10", "score": 3760, "attacks": 16, "damage": "7.66亿亿"}]})
        outcome = report.run_once(self.root, now_ts=now + 120, sender=sender)
        self.assertEqual(("delivered", f"{FP[:16]}:final"), (outcome["status"], outcome["key"]))
        self.assertNotIn("本方未参战", sent[1])
        self.assertIn("贡献榜前三：1. @sample_peer_10 3760分 强攻16 伤害7.66亿亿", sent[1])
        self.assertEqual(2, len(sent))

    def test_ranks_that_land_after_the_report_follow_once(self):
        """2026-09-26: report 13:43:38, official post 13:48:01, so the ranks never reached Telegram."""
        for account in report.ACCOUNTS:
            _journal(self.root, account, {"status": "settled_partial", "expected_windows": 16, "accepted_hits": 16,
                                          "server_perfects": 15, "protocol_result": {"finish_confirmed": True,
                                                                                   "server_result": {"grade": "甲等", "score": 100}}})
        sent = []

        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": len(sent), "success": True}

        self.assertEqual(f"{FP[:16]}:final", report.run_once(self.root, now_ts=self.entry_ts + 206, sender=sender)["key"])
        self.assertNotIn("贡献榜", sent[0])
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=self.entry_ts + 300, sender=sender)["status"])
        _write(self.root / "state" / "world_boss_shared_board.json",
               {"chat_id": -1009400000004, "message_id": 12406301, "published_at": "2026-09-19T13:48:01",
                "participants": 49, "outcome": "天道败退", "row_count": 3,
                "rows": [{"rank": 1, "username": "demo_player_02", "score": 3760, "attacks": 16, "damage": "7.78亿亿"},
                         {"rank": 2, "username": "sample_peer_13", "score": 3760, "attacks": 16, "damage": "7.56亿亿"},
                         {"rank": 3, "username": "demo_player_06", "score": 3760, "attacks": 16, "damage": "7.48亿亿"}]})
        outcome = report.run_once(self.root, now_ts=self.entry_ts + 480, sender=sender)
        self.assertEqual(("delivered", f"{FP[:16]}:board"), (outcome["status"], outcome["key"]))
        self.assertIn("【青元子贡献榜】09-19 13:40 入口 12406242，天道败退", sent[1])
        self.assertIn("贡献榜前三（参战 49 人）：1. @demo_player_02 3760分 强攻16 伤害7.78亿亿", sent[1])
        self.assertIn("本方上榜：1. @demo_player_02 3760分 强攻16 伤害7.78亿亿；3. @demo_player_06 3760分 强攻16 伤害7.48亿亿", sent[1])
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=self.entry_ts + 540, sender=sender)["status"])
        self.assertEqual(2, len(sent))

    def test_report_built_after_the_post_carries_the_ranks_and_sends_no_followup(self):
        for account in report.ACCOUNTS:
            _journal(self.root, account, {"status": "settled_partial", "expected_windows": 16, "accepted_hits": 16,
                                          "server_perfects": 15, "protocol_result": {"finish_confirmed": True,
                                                                                   "server_result": {"grade": "甲等", "score": 100}}})
        _write(self.root / "state" / "world_boss_shared_board.json",
               {"chat_id": -1009400000004, "message_id": 12406301, "published_at": "2026-09-19T13:47:17",
                "rows": [{"rank": 1, "username": "sample_peer_10", "score": 3760, "attacks": 16, "damage": "7.66亿亿"}]})
        sent = []

        def sender(text):
            sent.append(text)
            return {"target": "telegram:1", "message_id": len(sent), "success": True}

        self.assertEqual(f"{FP[:16]}:final", report.run_once(self.root, now_ts=self.entry_ts + 600, sender=sender)["key"])
        self.assertIn("贡献榜前三：1. @sample_peer_10", sent[0])
        self.assertEqual("already_delivered", report.run_once(self.root, now_ts=self.entry_ts + 660, sender=sender)["status"])
        self.assertEqual(1, len(sent))

    def test_a_rejected_finish_after_confirmed_hits_is_reported_as_begun(self):
        """2026-09-22 @demo_player_04: five server hits, finish rejected, report said 未开战."""
        failed = {"status": "runtime_error", "error": "boss_client_clock_mismatch", "expected_windows": 16,
                  "confirmed_phases": [{"phase": "begin_confirmed", "at": self.entry_ts + 210}],
                  "receipts": [{"index": i, "accepted": True, "server_perfect": False} for i in range(1, 6)],
                  "protocol_result": {"finish_confirmed": False, "phase": "finish_submitted", "accepted_hits": 5,
                                      "server_perfects": 0, "begin_confirmed": True, "local_dead": True}}
        settled = {"status": "settled_partial", "expected_windows": 16, "accepted_hits": 16, "server_perfects": 14,
                   "protocol_result": {"finish_confirmed": True, "server_result": {"grade": "甲等", "score": 100}}}
        for account in report.ACCOUNTS:
            _journal(self.root, account, failed if account == "tg9100000004" else settled)
        sent = []
        outcome = report.run_once(self.root, now_ts=self.entry_ts + 400,
                                  sender=lambda text: (sent.append(text) or {"target": "t", "message_id": 3, "success": True}))
        self.assertEqual("delivered", outcome["status"])
        self.assertIn("开战 6/6，正式结算 5/6，命中 85/96", sent[0])
        self.assertIn("@demo_player_04: 已开战未正式结算，命中 5/16 PERFECT 0，boss_client_clock_mismatch（finish_submitted），开战 +210s", sent[0])

    def test_missing_entry_or_journal_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual("not_due", report.run_once(Path(tmp), now_ts=1.0, sender=lambda _t: {})["status"])


if __name__ == "__main__":
    unittest.main()


class SettlementBoardTests(unittest.TestCase):
    BOARD = (
        "━━━━━━━━━━━━━━━\n**【世界通告｜真仙试锋败退】**\n━━━━━━━━━━━━━━━\n"
        "**战果**\n- 结果：**天道败退**\n- 参战：**33** 人\n\n**贡献榜**\n"
        "1. @sample_peer_10 - 3760 分｜强攻 16｜伤害 7.66亿亿\n"
        "2. @sample_peer_02 - 3760 分｜强攻 16｜伤害 7.39亿亿\n"
        "3. @sample_peer_17 - 3760 分｜强攻 16｜伤害 7.31亿亿\n"
        "10. @demo_player_04 - 3714 分｜强攻 16｜伤害 6.93亿亿\n\n**保底结算**\n- @sample_peer_10：伐仙功 +7\n"
    )

    def test_official_post_is_parsed_and_ranked_against_our_accounts(self):
        from qingyuanzi_host import parse_settlement_board
        board = parse_settlement_board(self.BOARD)
        self.assertEqual((board["participants"], board["outcome"], board["row_count"]), (33, "天道败退", 4))
        self.assertEqual(board["rows"][0], {"rank": 1, "username": "sample_peer_10", "score": 3760, "attacks": 16, "damage": "7.66亿亿"})
        board["published_at"] = "2026-09-20T13:47:17"
        lines = report.board_lines(board, report._entry_ts({"published_at": "2026-09-20T13:40:15"}))
        self.assertEqual(lines[0], "贡献榜前三（参战 33 人）：1. @sample_peer_10 3760分 强攻16 伤害7.66亿亿；2. @sample_peer_02 3760分 强攻16 伤害7.39亿亿；3. @sample_peer_17 3760分 强攻16 伤害7.31亿亿")
        self.assertEqual(lines[1], "本方上榜：10. @demo_player_04 3714分 强攻16 伤害6.93亿亿")
        self.assertEqual(lines[2], "与第一名分差：最小 46 分，最大 46 分")
        # A board from another day never decorates this event.
        self.assertEqual([], report.board_lines({**board, "published_at": "2026-09-19T13:47:17"},
                                                report._entry_ts({"published_at": "2026-09-20T13:40:15"})))
        self.assertIsNone(parse_settlement_board("**你的本命元婴** 等级 49"))
