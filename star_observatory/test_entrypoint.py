import tempfile
import unittest
from pathlib import Path

from star_observatory.run_cycle import check_configuration


class StarObservatoryEntrypointTests(unittest.TestCase):
    def test_check_configuration_requires_both_dedicated_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            (root / "state" / "tg9100000004.session").write_bytes(b"sqlite")
            self.assertEqual(["tg9100000001", "tg9100000005"], check_configuration(root)["missing_sessions"])
            (root / "state" / "tg9100000005.session").write_bytes(b"sqlite")
            vv = root / "state" / "accounts" / "tg9100000001" / "session.session"
            vv.parent.mkdir(parents=True)
            vv.write_bytes(b"sqlite")
            result = check_configuration(root)

        self.assertTrue(result["ok"])
        # Accounts run in accounts-config order.
        self.assertEqual(["tg9100000001", "tg9100000004", "tg9100000005"], result["accounts"])
        self.assertEqual({"tg9100000001": "demo_player_01", "tg9100000004": "demo_player_04", "tg9100000005": "demo_player_05"},
                         result["usernames"])
        self.assertEqual("示例绑定角色", result["identity_scope"]["tg9100000001"])


if __name__ == "__main__":
    unittest.main()
