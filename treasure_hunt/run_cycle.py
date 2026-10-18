"""Daily hunt for all six personal accounts, using the existing VPS dispatcher.

--dry-run reads only. --force bypasses local timers, never quota/pause/priority.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for
from miniapp_shared.dwelling import error_code, make_bridge, missing_sessions
from miniapp_shared.state import shanghai_now, day_key
from .runtime import Client, run_account
from .protocol import quota
from .reports import report
from .strategy import VERSION

ALL_ACCOUNTS = accounts_for('treasure_hunt')
START_HOUR = 4


async def execute(root, accounts, args):
    if args.dry_run:
        rows = []
        for account in accounts:
            try:
                async with make_bridge(root, account) as bridge:
                    client = await Client(root, account, bridge).open()
                    rows.append({'account': account, 'player_id': int(account[2:]),
                                 'quota': quota(await client.details(), int(account[2:])), 'status': 'ready'})
            except Exception as exc:
                rows.append({'account': account, 'status': 'failed', 'error': error_code(exc)})
        return {'mode': 'read_only', 'accounts': rows}
    if not args.force and shanghai_now().hour < START_HOUR:
        return {'status': 'waiting_admission', 'day': day_key()}
    results = []
    for account in accounts:
        row = await run_account(root, account, force=args.force, max_runs=args.max_runs)
        results.append(row)
        print(json.dumps({'event': 'hunt_account', **row}, ensure_ascii=False), flush=True)
    return {'flow': 'treasure_hunt', 'policy_version': VERSION, 'accounts': results, 'report': report(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', choices=['all', *ALL_ACCOUNTS], default='all')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--report', action='store_true', help='report saved receipts; no game requests')
    parser.add_argument('--max-runs', type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args(argv)
    accounts = ALL_ACCOUNTS if args.account == 'all' else (args.account,)
    logging.basicConfig(level=logging.WARNING)
    if args.check:
        result = {'flow': 'treasure_hunt', 'accounts': list(ALL_ACCOUNTS), 'usernames': ACCOUNT_USERNAMES,
                  'policy_version': VERSION,
                  'missing_sessions': missing_sessions(ROOT, tuple(accounts)), 'starts': '04:00 Asia/Shanghai',
                  'scope': 'six_personal_characters', 'quota': 'official_server'}
    elif args.report:
        result = report(ROOT, force=args.force)
    else:
        result = asyncio.run(execute(ROOT, accounts, args))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if any(r.get('status') in ('failed', 'account_failed', 'needs_reconcile') for r in result.get('accounts', []) if isinstance(r, dict)) else 0


if __name__ == '__main__':
    raise SystemExit(main())
