"""The production host is the sole account mapping, not the retired Dashboard config."""
import unittest
from pathlib import Path
from types import SimpleNamespace

import qingyuanzi_host as host


EXPECTED_ACCOUNTS = (
    "tg9100000001",
    "tg9100000002",
    "tg9100000003",
    "tg9100000004",
    "tg9100000005",
    "tg9100000006",
)
EXPECTED_USERNAMES = (
    "demo_player_01", "demo_player_02", "demo_player_03", "demo_player_04", "demo_player_05", "demo_player_06",
)


class SixAccountSettingsTests(unittest.TestCase):
    def test_only_the_six_verified_accounts_are_defined_by_the_host(self):
        self.assertEqual(EXPECTED_ACCOUNTS, tuple(host.ACCOUNTS))
        self.assertEqual(EXPECTED_USERNAMES, tuple(row["username"] for row in host.ACCOUNTS.values()))
        sources = tuple(host.session_source_for(account) for account in EXPECTED_ACCOUNTS)
        self.assertEqual(EXPECTED_ACCOUNTS[:-1] + ("tg9100000007",), sources)
        self.assertEqual(len(sources), len(set(sources)))

    def test_retired_dashboard_settings_cannot_override_the_host(self):
        root = Path(host.__file__).parent
        stale = sorted(
            str(path.relative_to(root))
            for pattern in ("automation_settings.*", "__pycache__/automation_settings.*.pyc")
            for path in root.glob(pattern)
        )
        self.assertEqual(stale, [], "An unused second account/settings authority must not remain")

    def test_world_boss_uses_protocol_execution_owner(self):
        self.assertEqual(EXPECTED_ACCOUNTS, tuple(host.ACCOUNTS))
        actor = SimpleNamespace(account_key=EXPECTED_ACCOUNTS[0], client=SimpleNamespace(), state={},
                                save_state=lambda: None)
        host.OfficialWorldBossMonitor(actor, EXPECTED_ACCOUNTS[0])._health("idle_waiting_event")
        self.assertEqual("official_protocol", actor.state["world_boss_health"]["execution_owner"])


if __name__ == "__main__":
    unittest.main()
