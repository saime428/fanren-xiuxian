"""Read-only wakeups for started, unfinished daily trials on the VPS scheduler."""
from __future__ import annotations

import math
from pathlib import Path

from miniapp_shared.accounts import ALL_ACCOUNTS
from miniapp_shared.state import day_key
from state_store import is_paused, load_json_dict

RUNTIME_MANIFEST = Path('/opt/runner/jobs.json')
WRAPPER_ROOT = '/Users/operator/.hermes/scripts'


def collect_retry_labels(project_root: Path, state_root: Path, *, now: float,
                         manifest_path: Path = RUNTIME_MANIFEST) -> list[str]:
    """Only retry today's recorded incomplete run; never open a new daily slot.

    Reuse the six actual cron labels from the running image, preserving disabled
    labels. The account coordinator retains its lock, admission, battle yield,
    server counter checks and uncertain-finish reconciliation.
    """
    manifest = load_json_dict(manifest_path)
    disabled = manifest.get('disabled_labels', {})
    if not isinstance(disabled, (dict, list)):
        return []
    jobs = manifest.get('jobs', [])
    if not isinstance(jobs, list):
        return []
    kicks = load_json_dict(state_root / 'return_deadline_dispatch.json').get('last_kick_by_label') or {}
    if not isinstance(kicks, dict):
        return []
    today = day_key(now)
    due = []
    for account in ALL_ACCOUNTS:
        expected = ['/bin/bash', f'{WRAPPER_ROOT}/xiuxian_tianji_trial_{account}.sh']
        matching = [job for job in jobs if isinstance(job, dict)
                    and job.get('ProgramArguments') == expected and job.get('Cron')]
        if len(matching) != 1:
            continue
        label = matching[0].get('Label')
        if not isinstance(label, str) or not label.startswith('hermes.cron.'):
            continue
        if (bool(disabled.get(label)) if isinstance(disabled, dict) else label in disabled):
            continue
        directory = project_root / 'tianji_trial/state/accounts' / account
        if (is_paused(directory, 'tianji_trial')[0]
                or is_paused(state_root / 'accounts' / account / 'tianji_trial', 'tianji_trial')[0]):
            continue
        row = load_json_dict(directory / 'summary.json')
        if row.get('account') != account or row.get('status') != 'retry' or row.get('receipt_confirmed') is True:
            continue
        try:
            updated = float(row['updated_at_ts'])
            recorded_day = row.get('attempt_date') or day_key(updated)
            raw_deadline = row.get('retry_not_before_ts', updated + 120)
            deadline = float(raw_deadline)
            last_kick = float(kicks.get(label) or 0)
            if (recorded_day == today and not isinstance(raw_deadline, bool)
                    and math.isfinite(deadline) and 0 < deadline <= now
                    and math.isfinite(last_kick) and now - last_kick >= 120):
                due.append(label)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    return sorted(set(due))
