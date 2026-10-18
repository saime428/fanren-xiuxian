import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tianji_trial.deadlines import collect_retry_labels, WRAPPER_ROOT

ACCOUNT = 'tg9100000001'
LABEL = 'hermes.cron.fixture'


class TianjiDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / 'state'
        self.now = datetime(2026, 10, 5, 3, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.directory = self.root / 'tianji_trial/state/accounts' / ACCOUNT
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'summary.json'
        self.row = {'account': ACCOUNT, 'status': 'retry', 'receipt_confirmed': False,
                    'attempt_date': '2026-10-05', 'updated_at_ts': self.now - 180,
                    'retry_not_before_ts': self.now - 60}
        self.job = {'Label': LABEL, 'Cron': '30,45 1,2 * * *',
                    'ProgramArguments': ['/bin/bash', f'{WRAPPER_ROOT}/xiuxian_tianji_trial_{ACCOUNT}.sh']}
        self.manifest = self.root / 'jobs.json'

    def collect(self, *, disabled=None, jobs=None):
        self.path.write_text(json.dumps(self.row))
        self.manifest.write_text(json.dumps({'jobs': [self.job] if jobs is None else jobs,
                                            'disabled_labels': disabled or {}}))
        return collect_retry_labels(self.root, self.state, now=self.now, manifest_path=self.manifest)

    def test_retry_after_last_daily_cron_slot_uses_existing_job_and_preserves_state(self):
        self.assertEqual(self.collect(), [LABEL])
        self.assertEqual(json.loads(self.path.read_text()), self.row)

    def test_future_and_yesterdays_deadlines_do_not_start(self):
        self.row['retry_not_before_ts'] = self.now + 1
        self.assertEqual(self.collect(), [])
        self.row.update(retry_not_before_ts=self.now - 60, attempt_date='2026-10-04')
        self.assertEqual(self.collect(), [])

    def test_completed_wrong_account_or_missing_records_do_not_start(self):
        original = dict(self.row)
        for change in ({'status': 'completed'}, {'receipt_confirmed': True}, {'account': 'tg1'}):
            self.row = {**original, **change}
            self.assertEqual(self.collect(), [])
        self.path.unlink()
        self.assertEqual(collect_retry_labels(self.root, self.state, now=self.now, manifest_path=self.manifest), [])

    def test_disabled_paused_missing_and_ambiguous_scheduler_are_respected(self):
        self.assertEqual(self.collect(disabled={LABEL: True}), [])
        self.assertEqual(self.collect(disabled=[LABEL]), [])
        self.assertEqual(self.collect(jobs=[]), [])
        self.assertEqual(self.collect(jobs=[self.job, dict(self.job)]), [])
        (self.directory / 'pause_all').touch()
        self.assertEqual(self.collect(), [])

    def test_old_retry_record_gets_a_bounded_fallback_and_recent_kicks_are_deduplicated(self):
        self.row.pop('attempt_date'); self.row.pop('retry_not_before_ts')
        self.assertEqual(self.collect(), [LABEL])
        self.state.mkdir()
        (self.state / 'return_deadline_dispatch.json').write_text(json.dumps({'last_kick_by_label': {LABEL: self.now - 30}}))
        self.assertEqual(self.collect(), [])

    def test_invalid_deadlines_do_not_trigger(self):
        for value in (True, None, 0, -1, float('nan'), float('inf'), 'bad'):
            with self.subTest(value=value):
                self.row['retry_not_before_ts'] = value
                self.assertEqual(self.collect(), [])
