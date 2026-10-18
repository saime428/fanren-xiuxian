import unittest

from send_outcome import SendState, classify_send_exception, recover_unknown_send


class SendOutcomeTests(unittest.TestCase):
    def test_timeout_is_unknown_not_failed(self):
        self.assertEqual(SendState.UNKNOWN, classify_send_exception(TimeoutError("timeout")))

    def test_validation_and_flood_are_definitely_not_sent(self):
        self.assertEqual(SendState.NOT_SENT, classify_send_exception(ValueError("bad input")))
        self.assertEqual(SendState.NOT_SENT, classify_send_exception(RuntimeError("FloodWait")))

    def test_unknown_recovery_requires_matching_message_and_identity(self):
        result = recover_unknown_send(
            state=SendState.UNKNOWN,
            command=".状态",
            identity_id=11,
            target_id=-100,
            attempt_started_at=100.0,
            messages=[
                {"message_id": 7, "text": ".状态", "sender_id": 11, "chat_id": -100, "ts": 101.0},
            ],
        )
        self.assertTrue(result["recovered"])
        self.assertEqual(7, result["message_id"])

    def test_unknown_recovery_does_not_infer_from_wrong_actor(self):
        result = recover_unknown_send(
            state=SendState.UNKNOWN,
            command=".状态",
            identity_id=11,
            target_id=-100,
            attempt_started_at=100.0,
            messages=[
                {"message_id": 7, "text": ".状态", "sender_id": 22, "chat_id": -100, "ts": 101.0},
            ],
        )
        self.assertFalse(result["recovered"])
        self.assertEqual("evidence_missing", result["reason"])


if __name__ == "__main__":
    unittest.main()
