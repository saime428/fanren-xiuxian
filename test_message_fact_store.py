import tempfile
import unittest
from pathlib import Path

from message_fact_store import MessageFactStore


class MessageFactStoreTests(unittest.TestCase):
    def test_append_is_redacted_deduplicated_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.jsonl"
            store = MessageFactStore(path, max_bytes=2000, max_lines=3)
            fact = {
                "event_type": "message",
                "identity_id": 11,
                "chat_id": -100,
                "message_id": 7,
                "sender_id": 11,
                "text": "cmd token=SUPERSECRET",
                "ts": 100.0,
            }
            self.assertTrue(store.append(fact))
            self.assertFalse(store.append(fact))
            rows = store.read()
            self.assertEqual(1, len(rows))
            self.assertNotIn("SUPERSECRET", path.read_text(encoding="utf-8"))
            self.assertIn("[REDACTED]", path.read_text(encoding="utf-8"))

    def test_different_revision_is_kept_for_edited_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageFactStore(Path(tmp) / "messages.jsonl")
            base = {"event_type": "message", "identity_id": 11, "chat_id": -100, "message_id": 7, "sender_id": 22, "text": "old", "ts": 100.0, "revision": 0}
            edited = dict(base, text="new", revision=1)
            self.assertTrue(store.append(base))
            self.assertTrue(store.append(edited))
            self.assertEqual(["old", "new"], [item["text"] for item in store.read()])

    def test_missing_identity_is_preserved_as_unattributed_not_inferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageFactStore(Path(tmp) / "messages.jsonl")
            store.append({"event_type": "message", "chat_id": -100, "message_id": 1, "sender_id": 22, "text": ".状态"})
            self.assertEqual(0, store.read()[0]["identity_id"])


if __name__ == "__main__":
    unittest.main()


# Keep the module intentionally simple and importable by unittest on Python 3.9.
