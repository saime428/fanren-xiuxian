"""Read-only hunt health: official quota and attributed receipts, not exit codes."""
from __future__ import annotations

from pathlib import Path
from miniapp_shared.state import day_key, read_json, shanghai_now


def health(directory: Path, account: str, now: float, *, running=False):
    state = read_json(Path(directory)/'state.json')
    if state.get('status') == 'malformed_state':
        return 'bad', 'hunt_malformed_state', 'Hunt state cannot be parsed', None
    if not state or state.get('day') != day_key(now):
        if shanghai_now(now).hour < 4:
            return 'ok', 'hunt_waiting_admission', 'Daily hunts start at 04:00 Asia/Shanghai', None
        return 'warn', 'hunt_not_observed_today', 'No current-day hunt evidence', None
    if state.get('account') != account or state.get('player_id') != int(account[2:]):
        return 'bad', 'hunt_state_identity_mismatch', 'Hunt journal identity mismatch', None
    status = state.get('status')
    if status in ('daily_complete_with_unknown','unconfirmed_start','unconfirmed_settle'):
        return 'warn', status, 'Quota checked; at least one payout has no confirmed receipt', None
    if status == 'no_dwelling':
        return 'warn', status, 'Server reports no personal dwelling', None
    if state.get('pending'):
        age = now - float(state['pending'].get('at', 0))
        if running and age < 120:
            return 'ok', 'hunt_in_progress', 'Live operation; payout not claimed yet', None
        return 'bad', 'hunt_operation_unconfirmed', 'A mutation is pending reconciliation; rewards unconfirmed', None
    if status == 'daily_complete':
        quota = state.get('quota') or {}
        receipts = state.get('receipts') or []
        valid_quota = (type(quota.get('used')) is int and type(quota.get('limit')) is int
                       and quota['limit'] > 0 and quota['used'] >= quota['limit'] and quota.get('remaining') == 0
                       and 0 <= now-float(state.get('quota_confirmed_at', 0)) < 86400)
        valid_receipts = (all(r.get('confirmed') is True and r.get('player_id') == int(account[2:])
                            and r.get('endpoint') == '/api/miniapp/xianxia-dwelling/hunt/settle'
                            and isinstance(r.get('receipt_key'), str) for r in receipts)
                          and len({r['receipt_key'] for r in receipts}) == len(receipts))
        today_receipts = sum(r.get('started_day', state['day']) == state['day'] for r in receipts)
        if not valid_quota or not valid_receipts or today_receipts + state.get('preexisting_used', 0) < quota['used']:
            return 'bad', 'hunt_completion_unproven', 'Quota or settlement evidence is incomplete', None
        return 'ok', 'hunt_daily_verified', f'{len(receipts)} confirmed payouts; {state.get("preexisting_used",0)} pre-existing attempts', None
    if running:
        return 'ok', 'hunt_in_progress', 'Account-scoped hunt worker is running', None
    due = state.get('next_due_at')
    if due and now < due:
        return 'warn', 'hunt_retry_scheduled', str(state.get('error') or status), due
    age = now-float(state.get('updated_at',0))
    return ('warn' if age < 600 else 'bad'), 'hunt_incomplete', str(state.get('error') or status), due
