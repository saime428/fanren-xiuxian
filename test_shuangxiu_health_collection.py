"""A business receipt, rather than a bare success string, clears this warning."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import xiuxian_healthcheck as h


class WenyangHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / 'accounts'
        self.directory = root / 'tg9100000001' / 'shuangxiu_wenyang'
        self.directory.mkdir(parents=True)
        state_patch = patch.object(h, 'STATE_ROOT', root)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        now = time.time() - 10
        self.receipt = {
            'status': 'success', 'ts': now, 'next_due_ts': now + 3600,
            'initiator_account': 'tg9100000001', 'initiator_username': 'demo_player_01',
            'partner_account': 'tg9100000003', 'partner_username': 'demo_player_03',
            'target': '@example_game_group', 'topic_id': 7310786,
            'anchor_message_id': 10, 'command_message_id': 11, 'reply_message_id': 12,
            'reply_to_msg_id': 11, 'reply_sender_id': 8917921351,
            'reply_text': '【温养双修·大成】@demo_player_03 修为增加了 61 点！@demo_player_01 修为增加了 61 点！',
        }

    def classify(self, attempt, success=None):
        for name, value in (('last_attempt', attempt), ('last_success', success or {})):
            (self.directory / f'{name}.json').write_text(json.dumps(value), encoding='utf-8')
        return h.classify_kind('tg9100000001', 'shuangxiu_wenyang', 'send', '', True, {})

    def test_confirmed_receipt_is_healthy_and_reports_the_business_cooldown(self):
        item = self.classify(self.receipt, self.receipt)
        self.assertEqual((item.status, item.severity), ('success', 'ok'))
        self.assertEqual(item.cooldown_until_ts, self.receipt['next_due_ts'])

    def test_receipt_validation_works_without_telegram_or_site_packages(self):
        # Cron's interpreter lacked Telethon. Exercise the real lazy receipt
        # validation in a fresh interpreter with all site packages disabled.
        result = subprocess.run(
            [sys.executable, '-S', '-c',
             'import json, sys\n'
             'from scripts import xiuxian_healthcheck as h\n'
             'receipt = json.load(sys.stdin)\n'
             'assert h.confirmed_wenyang_receipt("tg9100000001", receipt, receipt)\n'
             'assert not h.valid_wenyang_business_reply("tg9100000001", '
             '{**receipt, "reply_sender_id": 999}, "success")\n'
             'assert "transport" not in sys.modules\n'
             'assert "telethon" not in sys.modules\n'],
            input=json.dumps(self.receipt), cwd=Path(__file__).parent,
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bare_missing_mismatched_or_wrong_account_receipts_do_not_clear_warning(self):
        cases = [
            ({'status': 'success', 'ts': time.time()}, {}),
            (self.receipt, {}),
            (self.receipt, {**self.receipt, 'reply_message_id': 99}),
        ]
        for extra in ({'initiator_account': 'tg999'}, {'reply_to_msg_id': 9}, {'reply_sender_id': 999},
                      {'partner_account': 'tg9100000005'}, {'topic_id': 1}, {'target': '-1009400000005'},
                      {'reply_message_id': None}, {'next_due_ts': None}, {'reply_text': ''}):
            bad = {**self.receipt, **extra}
            cases.append((bad, bad))
        for attempt, success in cases:
            with self.subTest(attempt=attempt):
                self.assertNotEqual(self.classify(attempt, success).severity, 'ok')

    def test_newer_failure_stale_receipt_and_current_log_error_are_not_hidden(self):
        self.assertEqual(self.classify({**self.receipt, 'status': 'sent_unconfirmed_assume_consumed'}, self.receipt).severity, 'bad')
        stale = {**self.receipt, 'ts': time.time() - 86400}
        self.assertNotEqual(self.classify(stale, stale).severity, 'ok')
        (self.directory / 'send.launchd.log').write_text('Traceback: current connection failed\n', encoding='utf-8')
        self.assertEqual(self.classify(self.receipt, self.receipt).severity, 'bad')

    def test_server_confirmed_cooldown_is_waiting_and_not_a_fake_success(self):
        receipt = {**self.receipt, 'status': 'cooldown',
                   'reply_text': '道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中）。'}
        item = self.classify(receipt)
        self.assertEqual((item.status, item.severity), ('cooldown', 'ok'))
        self.assertEqual(item.cooldown_until_ts, receipt['next_due_ts'])
        self.assertNotEqual(self.classify({**receipt, 'reply_sender_id': 999}).severity, 'ok')

    def test_abandoned_submitted_round_is_bad_even_if_previous_round_succeeded(self):
        for status in ['anchor_send_submitted', 'command_send_submitted', 'command_send_unknown']:
            self.assertEqual(self.classify({**self.receipt, 'status': status}, self.receipt).severity, 'bad')


if __name__ == '__main__':
    unittest.main()
