import unittest
import unittest.mock

from watch_send_health import format_attempt_summary, infer_send_launchd_label


class WatchdogFormatTests(unittest.TestCase):
    def test_format_attempt_summary_includes_core_fields(self):
        summary = format_attempt_summary(
            {
                'status': 'resent_pending_ack',
                'message_id': 123,
                'resent_message_id': 456,
                'completion_reason': 'received cooldown reply and resent after waiting; waiting for next confirmed non-cooldown reply',
            }
        )
        self.assertIn('status=resent_pending_ack', summary)
        self.assertIn('message_id=123', summary)
        self.assertIn('resent_message_id=456', summary)
        self.assertIn('reason=received cooldown reply and resent after waiting; waiting for next confirmed non-cooldown reply', summary)

    def test_format_attempt_summary_handles_empty(self):
        self.assertEqual(format_attempt_summary({}), 'status=unknown')


    def test_infer_send_launchd_label_for_secondary_account(self):
        with unittest.mock.patch.dict(
            'os.environ',
            {'XIU_ACCOUNT_SLUG': 'tg9100000007', 'TG_FLOW_SLUG': 'wendao', 'TG_SEND_LAUNCHD_LABEL': ''},
            clear=False,
        ):
            self.assertEqual(infer_send_launchd_label(), 'ai.hermes.xiuxian.tg9100000007.wendao.send')


if __name__ == '__main__':
    unittest.main()
