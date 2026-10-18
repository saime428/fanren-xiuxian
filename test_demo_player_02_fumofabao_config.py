"""Template parity and account isolation for demo_player_02's artifact interaction."""
import json
import plistlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FLOW = 'fumofabao_qingzhufengyunjian_shenlei'
TEMPLATE = 'tg9100000006'
ACCOUNT = 'tg9100000002'

class demo_player_02ArtifactTests(unittest.TestCase):
    def test_behavior_matches_demo_player_06_with_only_account_credentials_changed(self):
        def executable(account):
            text = (ROOT / f'scripts/send_{FLOW}_{account}.sh').read_text()
            return '\n'.join(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#'))
        expected = executable(TEMPLATE).replace(TEMPLATE, ACCOUNT).replace('tg9100000007', ACCOUNT)
        actual = executable(ACCOUNT)
        self.assertEqual(actual, expected)
        self.assertIn(f"export XIU_ACCOUNT_SLUG='{ACCOUNT}'", actual)
        self.assertIn(f'source ./config/accounts/{ACCOUNT}.env', actual)
        self.assertNotIn(TEMPLATE, actual)
        self.assertNotIn('tg9100000007', actual)

    def test_scheduler_is_independent_and_has_identical_timing(self):
        def spec(account):
            return plistlib.loads((ROOT / f'scripts/ai.hermes.xiuxian.{account}.{FLOW}.send.plist').read_bytes())
        template = json.loads(json.dumps(spec(TEMPLATE)).replace(TEMPLATE, ACCOUNT))
        self.assertEqual(spec(ACCOUNT), template)
        self.assertFalse(template['RunAtLoad'])
        self.assertEqual(template['StartInterval'], 7200)
        self.assertIn(f'state/accounts/{ACCOUNT}/{FLOW}', template['ProgramArguments'][-1])

    def test_registry_has_one_enabled_sender_for_each_account(self):
        rows = json.loads((ROOT / 'config/active_flows.json').read_text())['flows']
        for account in (ACCOUNT, TEMPLATE):
            matches = [r for r in rows if r.get('account') == account and r.get('flow') == FLOW]
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]['enabled'])
            self.assertEqual(matches[0]['kinds'], ['send'])
