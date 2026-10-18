import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from message_fact_store import MessageFactStore
from transport import send_with_global_gate


class MessageFactTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_send_records_account_attributed_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "messages.jsonl"

            async def action():
                return SimpleNamespace(id=77)

            with patch.dict(
                "os.environ",
                {
                    "TG_GLOBAL_SEND_GATE_STATE_PATH": str(root / "gate.json"),
                    "TG_GLOBAL_MIN_SEND_SPACING_SECONDS": "1",
                    "TG_MESSAGE_LOG_PATH": str(log_path),
                },
                clear=False,
            ):
                result = await send_with_global_gate(
                    action,
                    label="qyz:test",
                    state_dir=root,
                    fact={
                        "event_type": "message",
                        "identity_id": 11,
                        "chat_id": -100,
                        "sender_id": 11,
                        "text": ".状态",
                    },
                )

            self.assertEqual(77, result.id)
            rows = MessageFactStore(log_path).read()
            self.assertEqual(1, len(rows))
            self.assertEqual(77, rows[0]["message_id"])
            self.assertEqual(11, rows[0]["identity_id"])

    async def test_failed_send_does_not_claim_success_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            async def action():
                raise TimeoutError("unknown delivery")

            with patch.dict(
                "os.environ",
                {
                    "TG_GLOBAL_SEND_GATE_STATE_PATH": str(root / "gate.json"),
                    "TG_GLOBAL_MIN_SEND_SPACING_SECONDS": "1",
                    "TG_MESSAGE_LOG_PATH": str(root / "messages.jsonl"),
                },
                clear=False,
            ):
                with self.assertRaises(TimeoutError):
                    await send_with_global_gate(
                        action,
                        label="qyz:test-fail",
                        state_dir=root,
                        fact={
                            "event_type": "message",
                            "identity_id": 11,
                            "chat_id": -100,
                            "sender_id": 11,
                            "text": ".状态",
                        },
                    )

            self.assertEqual([], MessageFactStore(root / "messages.jsonl").read())


if __name__ == "__main__":
    unittest.main()
