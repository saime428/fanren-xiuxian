import tempfile
import unittest
from pathlib import Path

from miniapp_shared.state import LockBusy
from star_observatory.state import account_lock, read_account_state, write_account_state


class StarObservatoryStateTests(unittest.TestCase):
    def test_state_is_account_scoped_and_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_account_state(root, "tg9100000004", {"status": "wait"})
            self.assertEqual(root / "state/accounts/tg9100000004/star_observatory/state.json", path)
            self.assertEqual({"status": "wait"}, read_account_state(root, "tg9100000004"))
            self.assertEqual({}, read_account_state(root, "tg9100000005"))
            with self.assertRaises(ValueError):
                write_account_state(root, "tg9100000002", {})
            with account_lock(root, "tg9100000004"):
                with self.assertRaises(LockBusy):
                    with account_lock(root, "tg9100000004"):
                        pass


    def test_demo_player_01_acts_only_as_the_bound_character(self):
        from types import SimpleNamespace
        from miniapp_shared.dwelling import MiniAppError
        from star_observatory.state import scoped_identity
        main = SimpleNamespace(player_id=9100000001, label="示例修士一")
        bound = SimpleNamespace(player_id=9200000001, label="示例绑定角色")
        renamed = SimpleNamespace(player_id=9999, label="示例绑定角色")

        def dwelling(*identities, account_id=9100000001):
            return SimpleNamespace(identities=identities, account_id=account_id, personal_identity=lambda: identities[0])

        self.assertIs(bound, scoped_identity("tg9100000001", dwelling(main, bound)))
        self.assertIs(renamed, scoped_identity("tg9100000001", dwelling(main, renamed)))  # label fallback
        with self.assertRaises(MiniAppError):
            scoped_identity("tg9100000001", dwelling(main))
        own = SimpleNamespace(player_id=9100000004, label="")
        self.assertIs(own, scoped_identity("tg9100000004", dwelling(own, bound, account_id=9100000004)))

if __name__ == "__main__":
    unittest.main()
