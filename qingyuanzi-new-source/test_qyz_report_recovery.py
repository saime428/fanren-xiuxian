"""Regressions for the October 7 false missed-battle report; no live clients."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import qingyuanzi_host as host
import qyz_battle_report as report
import test_qyz_battle_report as fixtures
from test_qyz_battle_report import FP, _journal, _write


class ReportRecoveryTests(unittest.TestCase):
    setUp = fixtures.BattleReportTests.setUp
    tearDown = fixtures.BattleReportTests.tearDown

    def test_partial_fleet_keeps_missing_accounts_in_denominator(self):
        for account in list(report.ACCOUNTS)[2:]:
            _journal(self.root, account, {"status": "settled_partial", "expected_windows": 16,
                "accepted_hits": 16, "server_perfects": 12,
                "protocol_result": {"finish_confirmed": True}})
        entry, records = report.collect(self.root / "state")
        text = report.build_report(entry, records, now_ts=self.entry_ts + 1000)[1]
        self.assertIn("开战 4/6，正式结算 4/6，命中 64/96，PERFECT 48", text)
        self.assertIn("@demo_player_01: 无本场记录", text)

    def test_corrected_board_sends_one_explicit_correction_after_false_missed_report(self):
        for account in list(report.ACCOUNTS)[2:]:
            _journal(self.root, account, {"status": "settled_partial", "expected_windows": 16,
                "accepted_hits": 16, "server_perfects": 12,
                "protocol_result": {"finish_confirmed": True}})
        _write(self.root / "state/world_boss_shared_board.json", {
            "message_id": 12406301, "published_at": "2026-09-19T13:47:16+08:00",
            "rows": [{"rank": 2, "username": "demo_player_03", "score": 3756}]})
        _write(self.root / "state/qyz_battle_report/notifications.json", {"delivered": [
            {"key": f"{FP[:16]}:deadline", "at": self.entry_ts + 960},
            {"key": "board:12406301:missed", "at": self.entry_ts + 1900}], "failures": []})
        sent = []
        sender = lambda text: sent.append(text) or {"message_id": 99, "success": True}
        for _ in range(2):
            report.run_once(self.root, now_ts=self.entry_ts + 7200, sender=sender)
        self.assertEqual(len(sent), 1)
        self.assertIn("【青元子战报", sent[0])
        self.assertIn("更正", sent[0])
        self.assertIn("命中 64/96", sent[0])
        self.assertIn("2. @demo_player_03", sent[0])
        self.assertIn("更正：", sent[0])

    def test_own_official_rank_never_claims_zero_participants(self):
        board = {"message_id": 77, "published_at": "2026-09-20T13:47:16+08:00",
                 "rows": [{"rank": 2, "username": "demo_player_03", "score": 3756}]}
        _, text = report.build_missed_event_report(board, {})
        self.assertNotIn("本方未参战 0/6", text)
        self.assertIn("本方已有账号上榜", text)


class PublicationTimestampTests(unittest.TestCase):
    def test_repeated_board_edits_preserve_telegram_publication_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.json"
            message = SimpleNamespace(id=12644802, chat_id=host.CANONICAL_CHAT_ID,
                date=datetime(2026, 10, 7, 5, 47, 16, tzinfo=timezone.utc),
                edit_date=datetime(2026, 10, 7, 6, 12, 3, tzinfo=timezone.utc))
            board = {"rows": [{"rank": 2, "username": "demo_player_03"}]}
            with patch.object(host, "SHARED_BOARD_STATE", path):
                for _ in range(2):
                    host._write_shared_board(message, board)
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved["published_at"], "2026-10-07T13:47:16+08:00")
                    self.assertTrue(report.board_belongs_to_entry(saved, {"published_at": "2026-10-07T13:40:13+08:00"}))

    def test_unknown_publication_time_does_not_create_a_false_new_board(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.json"
            with patch.object(host, "SHARED_BOARD_STATE", path):
                host._write_shared_board(SimpleNamespace(id=77, chat_id=host.CANONICAL_CHAT_ID), {"rows": [{}]})
            self.assertFalse(path.exists())

    def test_entry_uses_publication_time_even_when_admitted_later(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entry.json"
            message = SimpleNamespace(chat_id=host.CANONICAL_CHAT_ID,
                date=datetime(2026, 10, 7, 5, 40, 13))
            with patch.object(host, "SHARED_ENTRY_STATE", path):
                host._write_shared_entry(message, SimpleNamespace(message_id=12644770, fingerprint=FP))
            self.assertEqual(json.loads(path.read_text())["published_at"], "2026-10-07T13:40:13+08:00")


class BackgroundRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_or_cancelled_child_restarts_without_cancelling_battle(self):
        for failure in [OSError, asyncio.CancelledError]:
            with self.subTest(failure=failure):
                stop = asyncio.Event()
                battle = asyncio.create_task(asyncio.Event().wait())
                calls = []

                async def loop():
                    calls.append(1)
                    if len(calls) == 1:
                        raise failure()
                    self.assertFalse(battle.done())
                    stop.set()

                with patch.object(host.asyncio, "sleep", new=AsyncMock()):
                    await host.supervise_monitor_loop("tg9100000001", "health", loop, stop)
                self.assertEqual(len(calls), 2)
                battle.cancel()
                await asyncio.gather(battle, return_exceptions=True)

    async def test_shutdown_cancels_child_and_does_not_restart_it(self):
        started, ended = asyncio.Event(), asyncio.Event()
        calls = []

        async def loop():
            calls.append(1)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        task = asyncio.create_task(host.supervise_monitor_loop("tg9100000001", "health", loop, asyncio.Event()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ended.is_set())
        self.assertEqual(len(calls), 1)
