import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flow_registry import active_entries, launchagent_labels
from scripts import xiuxian_healthcheck as h

DISPATCH = 'ai.hermes.xiuxian.return-deadline-dispatch'


class FlowRegistryTests(unittest.TestCase):
    def test_removed_demo_player_06_yuanying_cannot_be_restored_by_stale_registry(self):
        entry = {'account': 'tg9100000006', 'flow': 'yuanyingchuqiao', 'kinds': ['send']}
        with self.assertRaisesRegex(ValueError, 'removed account flow'):
            active_entries({'flows': [entry]})
        for kind in ('send', 'watchdog', 'return_observer', 'return_observer.watchdog'):
            label = f'ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.{kind}'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'removed account scheduler'):
                active_entries({'flows': [], 'helpers': [{'label': label}]})
        # The deletion is account- and flow-scoped.
        others = [{**entry, 'account': 'tg9100000001'}, {**entry, 'flow': 'shendubiguan'}]
        self.assertEqual(active_entries({'flows': others}), others)

    def test_monitor_evidence_requirement_is_preserved_and_must_be_boolean(self):
        entry = {'account': 'tg123', 'flow': 'jiyin_choice', 'kinds': ['send'],
                 'monitor_health_required': True}
        self.assertTrue(active_entries({'flows': [entry]})[0]['monitor_health_required'])
        for invalid in ('true', 'false', 1, None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                active_entries({'flows': [{**entry, 'monitor_health_required': invalid}]})

    def test_six_account_checks_share_one_restore_label_and_include_enabled_helper(self):
        payload = {'flows': [{'account': f'tg{i}', 'flow': 'qyz_miniapp', 'kinds': ['qyz_monitor']}
                            for i in range(6)],
                   'helpers': [{'label': DISPATCH, 'enabled': True},
                               {'label': 'ai.hermes.xiuxian.disabled', 'enabled': False}]}
        entries = active_entries(payload)
        self.assertEqual(len(entries), 7)
        self.assertEqual(launchagent_labels(entries),
                         ['ai.hermes.xiuxian.qingyuanzi-six.monitor', DISPATCH])

    def test_an_enabled_helper_alone_is_collected_and_missing_service_is_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'active.json'
            config.write_text(json.dumps({'flows': [], 'helpers': [{'label': DISPATCH, 'enabled': True}]}))
            with patch.object(h, 'CONFIG_PATH', config):
                entries, error = h.load_active_config()
            self.assertIsNone(error)
            with patch.object(h, 'launchctl_snapshot', return_value=''), \
                 patch.object(h, 'disabled_launchagent_labels', return_value=set()):
                items = h.collect_active_items('', entries, {})
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].status, 'helper_not_loaded')
            self.assertEqual(h.summarize(items, [])['counts']['bad'], 1)

    def test_invalid_or_disabled_malformed_helpers_are_rejected(self):
        for helpers in (None, {}, ['invalid'], [{'label': DISPATCH, 'enabled': 'false'}],
                        [{'label': '../outside', 'enabled': False}],
                        [{'label': DISPATCH}, {'label': DISPATCH}]):
            with self.subTest(helpers=helpers), self.assertRaises(ValueError):
                active_entries({'flows': [], 'helpers': helpers})

    def test_helper_liveness_exit_and_disable_evidence(self):
        entry = active_entries({'flows': [], 'helpers': [{'label': DISPATCH}]})[0]
        cases = [
            ('state = not running\nlast exit code = 0\nrun interval = 30 seconds', set(), 'ok'),
            ('state = running\nlast exit code = 1', set(), 'ok'),
            ('state = spawn scheduled\nlast exit code = 0\nproperties = keepalive | runatload', set(), 'ok'),
            ('state = spawn scheduled\nlast exit code = 1\nproperties = keepalive | runatload', set(), 'bad'),
            ('state = not running\nlast exit code = 1', set(), 'bad'),
            ('state = not running\nproperties = keepalive | runatload', set(), 'bad'),
            ('state = not running\nlast exit code = 0', {DISPATCH}, 'bad'),
            ('state = running', None, 'bad'),
        ]
        for launchd, disabled, severity in cases:
            with self.subTest(launchd=launchd, disabled=disabled), \
                 patch.object(h, 'launchctl_snapshot', return_value=launchd):
                self.assertEqual(h.classify_helper(entry, disabled).severity, severity)

    def test_launchctl_word_and_boolean_disabled_formats_are_recognized(self):
        output = '\n'.join(['"ai.hermes.xiuxian.one" => disabled',
                            '"ai.hermes.xiuxian.two" => true',
                            '"ai.hermes.xiuxian.three" => enabled',
                            '"ai.hermes.xiuxian.four" => false'])
        with patch.object(h.subprocess, 'check_output', return_value=output):
            self.assertEqual(h.disabled_launchagent_labels(),
                             {'ai.hermes.xiuxian.one', 'ai.hermes.xiuxian.two'})

    def test_restore_list_is_the_same_deduplicated_registry_without_service_actions(self):
        root = Path(__file__).resolve().parent
        expected = launchagent_labels(active_entries(json.loads((root / 'config/active_flows.json').read_text())))
        output = subprocess.check_output(['bash', str(root / 'scripts/restore_xiuxian_launchagents.sh'), '--list'],
                                         text=True, env={**os.environ, 'PYTHON': sys.executable})
        self.assertEqual(output.splitlines(), expected)
        self.assertEqual(output.count('ai.hermes.xiuxian.qingyuanzi-six.monitor\n'), 1)
        self.assertIn(DISPATCH, expected)
        self.assertFalse(any('tg9100000007' in label or 'yewaililian' in label for label in expected))


if __name__ == '__main__':
    unittest.main()
