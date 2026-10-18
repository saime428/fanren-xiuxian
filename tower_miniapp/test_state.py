import tempfile
import unittest
from pathlib import Path

from miniapp_shared.state import LockBusy
from tower_miniapp.state import account_lock, read_identity, read_state, write_identity, write_state


class TowerStateTests(unittest.TestCase):
    def test_state_paths_are_account_and_identity_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_state(root, "tg9100000001", {"status": "completed"})
            write_identity(root, "tg9100000001", 9200000001, {"status": "settled_confirmed"})
            self.assertEqual({"status": "completed"}, read_state(root, "tg9100000001"))
            self.assertEqual({"status": "settled_confirmed"}, read_identity(root, "tg9100000001", 9200000001))
            self.assertTrue((root / "state/accounts/tg9100000001/tower_miniapp/identities/9200000001.json").is_file())
            with self.assertRaises(ValueError):
                write_state(root, "tg9100000007", {})
            with account_lock(root, "tg9100000001"):
                with self.assertRaises(LockBusy):
                    with account_lock(root, "tg9100000001"):
                        pass


if __name__ == "__main__":
    unittest.main()
