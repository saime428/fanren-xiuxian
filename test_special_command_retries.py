"""Exercise real state transitions with isolated clocks, clients and files."""
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import send_shendubiguan as retreat
import probe_yuanyingchuqiao_status as yuanying_probe
from command_confirmation import PendingCommand, validate_command_identity
from doufa_receipts import apply_receipt
from scripts import xiuxian_healthcheck as health


# These fixtures exercise the explicitly unselected legacy transport. The
# active MiniApp route and its no-group-fallback contract have separate tests
# in miniapp_commands/test_integration.py.
@patch("miniapp_commands.runner.migrated", new=lambda *_: False)
class RetreatRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_status_reply_survives_restart_without_resending_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = PendingCommand(root, flow="shendubiguan", command=".查看闭关",
                target="@example_game_group", topic_id=7310786, actor_id=9100000001,
                filename="status_probe_pending.json", clock=lambda: 1000)
            pending.begin(120)
            pending.sent(SimpleNamespace(id=10))
            environment = {"TG_API_ID": "1", "TG_API_HASH": "fixture", "TG_SESSION_NAME": str(root / "fixture"),
                "TG_TARGET": "@example_game_group", "TG_TOPIC_ID": "7310786", "TG_STATE_DIR": str(root),
                "TG_FLOW_SLUG": "shendubiguan", "XIU_ACCOUNT_SLUG": "tg9100000001",
                "TG_MIN_REPEAT_SECONDS": "0", "TG_LAST_ATTEMPT_PATH": str(root / "last_attempt.json"),
                "TG_LAST_SUCCESS_PATH": str(root / "last_success.json")}
            client = SimpleNamespace(start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(
                id=9100000001, username="demo_player_01")), disconnect=AsyncMock())
            async def retry(action, **kwargs):
                return await action()
            reply = {"id": 11, "sender_id": 8917921351, "text": "你正在深度闭关，预计还需 1小时",
                     "ts": 1001, "match_kind": "direct"}
            with patch.dict(os.environ, environment, clear=True), patch.object(retreat, "load_dotenv"), \
                    patch.object(retreat, "telegram_client", return_value=client), \
                    patch.object(retreat, "with_telegram_retry", side_effect=retry), \
                    patch.object(retreat, "collect_recent_messages", new_callable=AsyncMock, return_value=[]), \
                    patch.object(retreat, "collect_reply_snapshot", new_callable=AsyncMock, return_value=[reply]), \
                    patch.object(retreat, "send_topic_command", new_callable=AsyncMock) as send, \
                    patch.object(retreat.time, "time", return_value=1060):
                await retreat.run_once()
            send.assert_not_awaited()
            self.assertFalse(json.loads((root / "status_probe_pending.json").read_text())["pending"])
            probe = json.loads((root / "status_probe.json").read_text())
            self.assertEqual(probe["sent_message_id"], 10)
            self.assertEqual(probe["not_before_ts"], 4601)
            self.assertTrue(probe["acked"])
            self.assertFalse((root / "last_success.json").exists())

    async def test_no_action_receipt_resumes_without_a_new_status_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"TG_API_ID": "1", "TG_API_HASH": "fixture", "TG_SESSION_NAME": str(root / "fixture"),
                "TG_TARGET": "@example_game_group", "TG_TOPIC_ID": "7310786", "TG_STATE_DIR": str(root),
                "TG_FLOW_SLUG": "shendubiguan", "XIU_ACCOUNT_SLUG": "tg9100000001",
                "TG_MIN_REPEAT_SECONDS": "0", "TG_LAST_ATTEMPT_PATH": str(root / "last_attempt.json"),
                "TG_LAST_SUCCESS_PATH": str(root / "last_success.json")}
            client = SimpleNamespace(start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(
                id=9100000001, username="demo_player_01")), disconnect=AsyncMock())
            async def retry(action, **kwargs):
                return await action()
            replies = [
                [{"id": 2, "sender_id": 8917921351, "text": "你并未处于深度闭关之中", "ts": 1000, "match_kind": "direct"}],
                [],
                [{"id": 6, "sender_id": 8917921351, "text": "你已进入深度闭关状态，预计 8小时 后出关", "ts": 1121, "match_kind": "direct"}],
            ]
            with patch.dict(os.environ, environment, clear=True), patch.object(retreat, "load_dotenv"), \
                    patch.object(retreat, "telegram_client", return_value=client), \
                    patch.object(retreat, "with_telegram_retry", side_effect=retry), \
                    patch.object(retreat, "ensure_connected", new_callable=AsyncMock), \
                    patch.object(retreat, "collect_recent_messages", new_callable=AsyncMock, return_value=[]), \
                    patch.object(retreat, "collect_reply_snapshot", new_callable=AsyncMock, return_value=[]), \
                    patch.object(retreat, "wait_for_replies", new_callable=AsyncMock, side_effect=replies), \
                    patch.object(retreat, "send_topic_command", new_callable=AsyncMock,
                                 side_effect=[SimpleNamespace(id=1), SimpleNamespace(id=3), SimpleNamespace(id=5)]) as send, \
                    patch.object(retreat.time, "time", return_value=1000) as clock:
                await retreat.run_once()
                self.assertFalse((root / "last_success.json").exists())
                self.assertTrue(json.loads((root / "command_pending.json").read_text())["pending"])
                clock.return_value = 1060
                await retreat.run_once()
                self.assertEqual(send.await_count, 2)
                clock.return_value = 1121
                await retreat.run_once()
                self.assertEqual([call.kwargs["text"] for call in send.await_args_list],
                                 [".查看闭关", ".深度闭关", ".深度闭关"])
            self.assertEqual(json.loads((root / "last_success.json").read_text())["reply_message_id"], 6)
            self.assertFalse(json.loads((root / "command_pending.json").read_text())["pending"])


@patch("miniapp_commands.runner.migrated", new=lambda *_: False)
class YuanyingProbeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retreat_status_overrides_help_without_inventing_an_eight_hour_expiry(self):
        wrapper = (Path(__file__).parent / 'scripts/lib/run_yuanyingchuqiao_send.sh').read_text()
        for status_line, expected_code, expected_status in (
                ('状态: 元婴闭关', 10, 'active_without_countdown'),
                ('狀態：元嬰閉關', 10, 'active_without_countdown'),
                ('**状态**: `元婴闭关`', 10, 'active_without_countdown'),
                ('状态: 窍中温养', 0, 'available')):
            with self.subTest(status_line=status_line), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pending = PendingCommand(root, flow='yuanyingchuqiao', command='.元婴状态',
                    target='@example_game_group', topic_id=7310786, actor_id=9100000001,
                    filename='status_probe_pending.json', clock=lambda: 995)
                pending.begin(120)
                pending.sent(SimpleNamespace(id=30))
                (root / 'last_success.json').write_text(json.dumps({
                    'actor_id': 9100000001, 'flow_slug': 'yuanyingchuqiao', 'message': '.元婴出窍',
                    'target': '@example_game_group', 'topic_id': 7310786, 'succeeded': True,
                    'acked': True, 'sent_message_id': 20, 'ack_reply_message_id': 21,
                    'ack_reply_sender_id': 8944702077, 'actual_send_ts': 900, 'ts': 901}))
                # Official status shape observed after the natural send (12311307).
                reply = SimpleNamespace(id=31, sender_id=8944702077, fwd_from=None,
                    message=f'你的本命元婴\n\n等级: 36 级\n{status_line}\n'
                            '已积累修为: 约 90 点 (发言时自动结算)\n\n'
                            '使用 .元婴出窍 或 .元婴闭关 派遣元婴。',
                    date=datetime.fromtimestamp(996, timezone.utc), edit_date=None,
                    reply_to=SimpleNamespace(reply_to_msg_id=30, reply_to_top_id=7310786))
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.get_me.return_value = SimpleNamespace(id=9100000001)
                settings = {'TG_API_ID': '1', 'TG_API_HASH': 'fixture', 'TG_SESSION_NAME': 'fixture',
                    'TG_TARGET': '@example_game_group', 'TG_TOPIC_ID': '7310786', 'TG_STATE_DIR': directory,
                    'TG_FLOW_SLUG': 'yuanyingchuqiao', 'XIU_ACCOUNT_SLUG': 'tg9100000001'}
                for name in ('TG_STATUS_PROBE_IN_PROGRESS_MARKERS', 'TG_STATUS_PROBE_CLEAR_MARKERS'):
                    settings[name] = re.search(rf"^export {name}='([^']+)'$", wrapper, re.MULTILINE).group(1)
                with patch.dict(os.environ, settings, clear=True), patch.object(yuanying_probe, 'load_dotenv'), \
                        patch.object(yuanying_probe, 'telegram_client', return_value=client), \
                        patch.object(yuanying_probe, 'ensure_connected', new_callable=AsyncMock), \
                        patch.object(yuanying_probe, 'collect_recent_messages', new_callable=AsyncMock, return_value=[reply]), \
                        patch.object(yuanying_probe, 'collect_direct_replies_to_message', new_callable=AsyncMock, return_value=[]), \
                        patch.object(yuanying_probe.time, 'time', return_value=1000):
                    self.assertEqual(await yuanying_probe._main_async(), expected_code)
                client.send_message.assert_not_awaited()
                result = json.loads((root / 'status_probe.json').read_text())
                self.assertEqual(result['status'], expected_status)
                self.assertEqual(result['reply_message_id'], 31)
                self.assertNotIn('not_before_ts', result)
                self.assertFalse(json.loads(pending.path.read_text())['pending'])

    async def test_many_copies_retry_with_bounded_history_and_any_old_reply_can_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 10000.0
            pending = PendingCommand(root, flow='yuanyingchuqiao', command='.元婴状态',
                target='@example_game_group', topic_id=7310786, actor_id=42,
                filename='status_probe_pending.json', clock=lambda: now)
            pending.data.update(pending=True, first_attempt_ts=8000, next_retry_ts=9999,
                                attempts=[{'message_id': mid, 'sent_at_ts': 8000 + mid} for mid in range(10, 1001, 10)])
            pending.save()
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.get_me.return_value = SimpleNamespace(id=42)
            client.send_message.return_value = SimpleNamespace(id=1010)
            async def gate(action, **_kwargs):
                return await action()
            settings = {'TG_API_ID': '1', 'TG_API_HASH': 'fixture', 'TG_SESSION_NAME': 'fixture',
                        'TG_TARGET': '@example_game_group', 'TG_TOPIC_ID': '7310786',
                        'TG_STATE_DIR': directory, 'TG_FLOW_SLUG': 'yuanyingchuqiao', 'XIU_ACCOUNT_SLUG': 'tg42'}
            with patch.dict(os.environ, settings, clear=True), patch.object(yuanying_probe, 'load_dotenv'), \
                    patch.object(yuanying_probe, 'telegram_client', return_value=client), \
                    patch.object(yuanying_probe, 'ensure_connected', new_callable=AsyncMock), \
                    patch.object(yuanying_probe, 'send_with_global_gate', side_effect=gate), \
                    patch.object(yuanying_probe, 'collect_recent_messages', new_callable=AsyncMock, return_value=[]) as recent, \
                    patch.object(yuanying_probe, 'collect_direct_replies_to_message', new_callable=AsyncMock, return_value=[]) as history, \
                    patch.object(yuanying_probe.asyncio, 'sleep', new_callable=AsyncMock), \
                    patch.object(yuanying_probe.time, 'time', side_effect=lambda: now):
                self.assertEqual(await yuanying_probe._main_async(), 11)
                self.assertLessEqual(history.await_count, 4)
                self.assertEqual(len(json.loads(pending.path.read_text())['attempts']), 101)
                now += 121
                recent.return_value = [SimpleNamespace(id=1100, sender_id=8917921351, fwd_from=None,
                    message='你的本命元婴 状态: 元神出窍 归来倒计时: 1小时',
                    date=datetime.fromtimestamp(now, timezone.utc), edit_date=None,
                    reply_to=SimpleNamespace(reply_to_msg_id=50, reply_to_top_id=7310786))]
                self.assertEqual(await yuanying_probe._main_async(), 10)
                client.send_message.assert_awaited_once()
            self.assertFalse(json.loads(pending.path.read_text())['pending'])
            self.assertEqual(json.loads((root / 'status_probe.json').read_text())['sent_message_id'], 50)


class ReceiptHealthTests(unittest.TestCase):
    def test_active_retreat_health_requires_a_current_owned_official_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state/accounts/tg9100000001/yuanyingchuqiao'
            state.mkdir(parents=True)
            (state / 'last_attempt.json').write_text(json.dumps({'ts': 900, 'status': 'settled'}))
            receipt = {'ts': 990, 'actor_id': 9100000001, 'flow_slug': 'yuanyingchuqiao',
                'message': '.元婴状态', 'status': 'active_without_countdown', 'acked': True,
                'target': '@example_game_group', 'topic_id': 7310786, 'sent_message_id': 30,
                'reply_message_id': 31, 'reply_sender_id': 8944702077}
            with patch.object(health, 'XIUXIAN_ROOT', root), patch.object(health, 'STATE_ROOT', root / 'state/accounts'), \
                    patch.object(health.time, 'time', return_value=1000), \
                    patch.object(health, 'cached_log_signal', return_value=(None, None)):
                for fields in ({}, {'actor_id': 42}, {'acked': False}, {'reply_sender_id': 123},
                               {'reply_message_id': None}, {'failed': True}, {'ts': 800}):
                    with self.subTest(fields=fields):
                        (state / 'status_probe.json').write_text(json.dumps({**receipt, **fields}))
                        result = health.classify_kind('tg9100000001', 'yuanyingchuqiao', 'send', '', True, {})
                        self.assertEqual(result.status == 'verified_status_waiting', not bool(fields))
                        self.assertIsNone(result.cooldown_until_ts)
                        if not fields:
                            self.assertEqual(result.severity, 'ok')

    def test_result_wait_requires_matching_official_receipt_not_a_local_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state/accounts/tg9100000001/wendao"
            state.mkdir(parents=True)
            receipt = {"ts": 1000, "status": "settled", "acked": True, "next_due_ts": 2000,
                       "actor_id": 9100000001, "flow_slug": "wendao", "message": ".问道",
                       "target": "@example_game_group", "topic_id": 7310786, "sent_message_id": 10,
                       "ack_reply_message_id": 11, "ack_reply_sender_id": 8917921351}
            with patch.object(health, "XIUXIAN_ROOT", root), patch.object(health, "STATE_ROOT", root / "state/accounts"), \
                    patch.object(health.time, "time", return_value=1010), \
                    patch.object(health, "cached_log_signal", return_value=(None, None)):
                for fields in ({}, {"actor_id": 9100000003}, {"ack_reply_sender_id": 123},
                               {"target": "-1009400000005"}, {"topic_id": 1}, {"failed": True}):
                    with self.subTest(fields=fields):
                        (state / "last_result.json").write_text(json.dumps({**receipt, **fields}))
                        result = health.classify_kind("tg9100000001", "wendao", "send", "", True, {})
                        self.assertEqual(result.severity == "ok", not bool(fields))

    def test_pending_retry_is_warn_even_with_recent_success_and_watchdog_green(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state/accounts/tg9100000001/wendao"
            state.mkdir(parents=True)
            pending = PendingCommand(state, flow="wendao", command=".问道", target="@example_game_group",
                topic_id=7310786, actor_id=9100000001, clock=lambda: 1000)
            pending.begin(120)
            (state / "last_success.json").write_text(json.dumps({"status": "acknowledged", "ts": 1000}))
            with patch.object(health, "XIUXIAN_ROOT", root), patch.object(health, "STATE_ROOT", root / "state/accounts"), \
                    patch.object(health.time, "time", return_value=1010), \
                    patch.object(health, "cached_log_signal", return_value=(None, None)), \
                    patch.object(health, "has_recent_watchdog_ok_signal", return_value=True):
                result = health.classify_kind("tg9100000001", "wendao", "send", "", True, {})
                self.assertEqual(result.status, "awaiting_official_reply")
                self.assertEqual(result.severity, "warn")
                with patch.object(health.time, "time", return_value=1500):
                    stalled = health.classify_kind("tg9100000001", "wendao", "send", "", True, {})
                    self.assertEqual(stalled.severity, "bad")

    def test_outbound_identity_rejects_secondary_group_and_wrong_actor(self):
        for target, account in (("-1009400000005", "tg9100000001"), ("@example_game_group", "tg9100000003")):
            with self.assertRaises(ValueError):
                validate_command_identity(target, 7310786, 9100000001, expected_account=account)


class DuelReceiptTests(unittest.TestCase):
    def test_late_receipt_counts_once_and_neither_transport_nor_rejection_counts(self):
        receipt = {"ts": 10000, "status": "settled", "acked": True, "sent_message_id": 10,
                   "ack_reply_message_id": 11, "ack_reply_sender_id": 8917921351,
                   "actor_id": 9100000003, "flow_slug": "doufa_demo_player_02",
                   "message": ".斗法 demo_player_01", "topic_id": 7310786, "target": "@example_game_group"}
        kwargs = {"account": "tg9100000003", "today": "1970-01-01"}
        first = apply_receipt({}, receipt, **kwargs)
        self.assertEqual(first["sent_count"], 1)
        self.assertEqual(apply_receipt(first, receipt, **kwargs)["sent_count"], 1)
        for fields in ({"acked": False}, {"actor_id": 9100000001}, {"duel_anti_repeat": True}, {"ack_reply_sender_id": 123},
                       {"target": "-1009400000005"}, {"ack_reply_message_id": 9}, {"topic_id": 1}):
            self.assertEqual(apply_receipt({}, {**receipt, **fields}, **kwargs)["sent_count"], 0)
