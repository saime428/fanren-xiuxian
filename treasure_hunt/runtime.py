"""Durable hunt state machine. All game mutations stay on the production host."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from miniapp_shared.accounts import ACCOUNT_USERNAMES
from miniapp_shared.dwelling import JsonTransport, error_code, make_bridge, open_dwelling, verify_username
from miniapp_shared.priority import yield_to_qyz
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, read_json, write_json
from account_lifecycle import assert_account_allowed
from state_store import is_paused

from . import protocol as p
from .strategy import VERSION, choose

FLOW = 'treasure_hunt'
TERMINAL = frozenset({'daily_complete', 'daily_complete_with_unknown', 'no_dwelling',
                      'unconfirmed_start', 'unconfirmed_settle'})
RETRY_SECONDS = 15 * 60
ACTIVE_RETRY_SECONDS = 60  # Resume on the next 5-minute tick, before a run expires.
# The six account jobs start on the same dispatcher tick and queue on the fleet
# lock; wait for a turn without blocking the event loop, but not forever.
FLEET_WAIT_SECONDS = 10 * 60
FLEET_POLL_SECONDS = 5.0


def directory(root: Path, account: str) -> Path:
    if account not in ACCOUNT_USERNAMES:
        raise p.HuntError('hunt_account_not_allowed')
    return Path(root) / 'state/accounts' / account / FLOW


def guard(root: Path, account: str, now: float) -> None:
    target = directory(root, account)
    assert_account_allowed(account, path=Path(root)/'state/account_lifecycle.json')
    if is_paused(target, FLOW, (target.parent, Path(root)/'state'))[0]:
        raise p.HuntError('account_paused')
    if yield_to_qyz(FLOW, now, root):
        raise p.HuntError('yielding_to_qyz')


class Client:
    def __init__(self, root: Path, account: str, bridge: Any, *, transport=None, clock=time.time):
        self.root, self.account, self.bridge, self.clock = Path(root), account, bridge, clock
        self.player_id = int(account[2:])
        self.transport = transport or JsonTransport({p.PREFIX+k: mutation for k, mutation in
            (('start', False), ('details', False), ('hunt', True), ('hunt/reveal', True), ('hunt/settle', True))})
        self.session = None

    async def open(self):
        guard(self.root, self.account, self.clock())
        await verify_username(self.bridge, self.account)
        self.session = await open_dwelling(self.bridge, self.transport)
        if not any(i.player_id == self.player_id for i in self.session.identities):
            raise p.HuntError('hunt_personal_identity_missing')
        return self

    async def request(self, path, extra=None, *, mutation=False):
        if self.session is None:
            raise p.HuntError('hunt_not_authenticated')
        guard(self.root, self.account, self.clock())
        # Share the existing MiniApp request gate. A busy gate leaves the saved
        # hunt available for the next tick; it never creates a second sender.
        with exclusive_lock(self.root/'state/miniapp_commands/api.lock'):
            return await self.transport(p.PREFIX+path,
                {**(extra or {}), **self.session.base_payload(self.player_id)}, mutation=mutation)

    async def details(self):
        return await self.request('details')

    async def mutate(self, operation, *, session_id=None, index=None):
        if operation not in ('start', 'reveal', 'settle'):
            raise p.HuntError('hunt_operation_not_allowed')
        extra = {}
        if operation != 'start':
            if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
                raise p.HuntError('hunt_session_invalid')
            extra['sessionId'] = session_id
        if operation == 'reveal':
            extra['index'] = p.number(index, maximum=24)
        return await self.request('hunt' if operation == 'start' else 'hunt/'+operation, extra, mutation=True)


def fresh_state(account, now, prior=None):
    state = {'version': 1, 'account': account, 'player_id': int(account[2:]), 'day': day_key(now),
             'status': 'ready', 'run': None, 'pending': None, 'receipts': [], 'unconfirmed_runs': [], 'decisions': [],
             'policy': VERSION, 'updated_at': now}
    if prior and prior.get('run') and not (prior.get('pending') or {}).get('operation') in ('start', 'settle'):
        state.update(run=prior['run'], pending=prior.get('pending'), run_day=prior.get('run_day', prior['day']))
    return state


def load_state(root, account, now):
    path = directory(root, account)/'state.json'
    state = read_json(path)
    if state.get('status') == 'malformed_state':
        raise p.HuntError('hunt_malformed_state')
    if state and (state.get('account') != account or state.get('player_id') != int(account[2:])):
        raise p.HuntError('hunt_state_identity_mismatch')
    if state and state.get('day') != day_key(now):
        old_day = state.get('day', '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', old_day):
            raise p.HuntError('hunt_state_day_invalid')
        write_json(path.parent/'days'/f'{old_day}.json', state)
        state = fresh_state(account, now, state)
    return state or fresh_state(account, now)


def summarize(state: dict) -> dict:
    receipts = state.get('receipts', [])
    rewards = {}
    for receipt in receipts:
        for item in receipt['loot']:
            rewards[item['name']] = rewards.get(item['name'], 0) + item['quantity']
    q = state.get('quota') or {}
    return {'account': state['account'], 'player_id': state['player_id'], 'flow': FLOW,
            'day': state['day'], 'status': state['status'], 'updated_at': state.get('updated_at'),
            'policy': state.get('policy', VERSION), 'used': q.get('used'), 'limit': q.get('limit'),
            'remaining': q.get('remaining'), 'settled_runs': len(receipts),
            'preexisting_used': state.get('preexisting_used', 0), 'quota_confirmed_at': state.get('quota_confirmed_at'),
            'confirmed': bool(receipts) and all(r.get('confirmed') is True for r in receipts),
            'contribution': sum(r['contribution'] for r in receipts), 'rewards': rewards,
            'grades': [r['grade'] for r in receipts], 'scores': [r['score'] for r in receipts],
            'main_found': sum(r['foundMain'] for r in receipts),
            'receipt_keys': [r['receipt_key'] for r in receipts],
            'unconfirmed_runs': len(state.get('unconfirmed_runs', [])),
            'pending_operation': (state.get('pending') or {}).get('operation'),
            'error': state.get('error'), 'next_due_at': state.get('next_due_at', 0)}


def save(root, state, now):
    state['updated_at'] = now
    state['policy'] = VERSION
    target = directory(root, state['account'])
    write_json(target/'state.json', state)
    result = summarize(state)
    write_json(target/'last_result.json', result)
    return result


async def play(root, account, client, *, state=None, clock=time.time, sleep=asyncio.sleep,
               max_runs=3, policy='adaptive'):
    """Consume remaining official attempts, or stop at a durable uncertainty."""
    state = state if state is not None else load_state(root, account, clock())
    before_receipts = len(state['receipts'])
    state.pop('error', None)
    state['next_due_at'] = 0
    try:
        details = await client.details()
        q = p.quota(details, int(account[2:]))
        state.update(quota=q, quota_confirmed_at=clock())
        state.setdefault('preexisting_used', max(0, q['used'] - len(state['receipts']) - bool(state.get('run'))))
        if not q['has_dwelling']:
            state['status'] = 'no_dwelling'
            return save(root, state, clock())
        pending = state.get('pending')
        if pending and pending['operation'] in ('start', 'settle'):
            # The official UI exposes no read-only hunt-session/receipt endpoint.
            # A quota increment proves an attempt, not its contents or payout.
            state['status'] = 'unconfirmed_'+pending['operation']
            return save(root, state, clock())
        while len(state['receipts']) - before_receipts < max_runs:
            active = state.get('run')
            pending = state.get('pending')
            if active:
                active = p.run({'ok': True, 'huntRun': active})
                state['run'] = active
                if pending and pending['operation'] == 'reveal':
                    decision = {'action': 'settle', 'reason': 'uncertain_reveal_cash_out'}
                else:
                    try:
                        decision = choose(active, policy=policy)
                    except p.HuntError as exc:
                        # Contradictory clues fail the same way on every tick until
                        # the run expires and its payout is lost: bank it now.
                        decision = {'action': 'settle', 'reason': 'strategy_'+exc.code}
                operation = decision['action']
            elif q['remaining'] > 0:
                operation, decision = 'start', {'action': 'start', 'reason': 'official_attempt_available'}
            else:
                state['status'] = 'daily_complete'
                break
            state['pending'] = {'operation': operation, 'at': clock(), 'used_before': q['used']}
            if active:
                state['pending']['session_id'] = active['sessionId']
            if operation == 'reveal':
                state['pending']['index'] = decision['index']
                if active['cells'][decision['index']]['revealed']:
                    raise p.HuntError('hunt_reveal_already_known')
            state['decisions'] = (state['decisions'] + [{**decision, 'at': clock()}])[-120:]
            state['status'] = 'pending_'+operation
            save(root, state, clock())
            try:
                reply = await client.mutate(operation, session_id=active['sessionId'] if active else None,
                                            index=decision.get('index'))
            except Exception as exc:
                if (active and error_code(exc) in ('hunt_session_missing', 'hunt_session_closed')
                        and not getattr(exc, 'uncertain', False)):
                    # A terminal server reply is different from a timeout.
                    # Keep the missing payout explicit and spend only the
                    # remaining official attempts; never count the old bag.
                    state.setdefault('unconfirmed_runs', []).append({
                        'run_key': hashlib.sha256(active['sessionId'].encode()).hexdigest()[:24],
                        'reason': error_code(exc), 'at': clock(), 'started_day': state.get('run_day', state['day'])})
                    state.update(run=None, pending=None, status='run_unavailable')
                    save(root, state, clock())
                    q = p.quota(await client.details(), int(account[2:]))
                    state.update(quota=q, quota_confirmed_at=clock())
                    continue
                # Lock/pause/rejection before a request is safe to try later.
                # Transport failures remain pending: never replay a reveal.
                if not getattr(exc, 'uncertain', False):
                    state['pending'] = None
                raise
            # A successful mutation with changed response shape is uncertain too.
            # Keep the intent until both actor/session and payload are validated.
            if operation == 'settle':
                result = p.settlement(reply, int(account[2:]))
                receipt_key = hashlib.sha256(active['sessionId'].encode()).hexdigest()[:24]
                if any(r['receipt_key'] == receipt_key for r in state['receipts']):
                    raise p.HuntError('hunt_duplicate_settlement')
                state['receipts'].append({**result, 'confirmed': True, 'receipt_key': receipt_key,
                    'received_at': clock(), 'player_id': int(account[2:]), 'endpoint': p.PREFIX+'hunt/settle',
                    'final_ap': active['ap'], 'bag_rows': len(active['loot']), 'score_before_settle': active['score'],
                    'started_day': state.get('run_day', state['day'])})
                state.update(run=None, pending=None, status='settled')
                save(root, state, clock())
                q = p.quota(reply, int(account[2:]), known_dwelling=q['has_dwelling'])
                state.update(quota=q, quota_confirmed_at=clock())
            else:
                if operation == 'start':
                    p.owner(reply, int(account[2:]))
                elif 'account' in reply:
                    p.owner(reply, int(account[2:]))
                current = p.run(reply, session_id=active['sessionId'] if active else None)
                if operation == 'reveal' and not current['cells'][decision['index']]['revealed']:
                    raise p.HuntError('hunt_reveal_unconfirmed')
                if operation == 'reveal':
                    observation = {'index': decision['index'], 'type': current['cells'][decision['index']]['type'],
                        'ap_before': active['ap'], 'ap_after': current['ap'],
                        'score_before': active['score'], 'score_after': current['score'],
                        'bag_rows_before': len(active['loot']), 'bag_rows_after': len(current['loot']),
                        'main_before': active['foundMain'], 'main_after': current['foundMain'], 'at': clock()}
                    state['observations'] = (state.get('observations', []) + [observation])[-100:]
                state.update(run=current, pending=None, status='active')
                if operation == 'start':
                    state['run_day'] = state['day']
                    # Persist the usable session before parsing the new quota.
                    save(root, state, clock())
                    q = p.quota(reply, int(account[2:]), known_dwelling=q['has_dwelling'])
                    state.update(quota=q, quota_confirmed_at=clock())
            save(root, state, clock())
            await sleep(1.0)
        if not state.get('run') and not state.get('pending'):
            # Fresh server read, rather than a process exit, proves exhaustion.
            q = p.quota(await client.details(), int(account[2:]))
            state.update(quota=q, quota_confirmed_at=clock(),
                         status='daily_complete' if q['remaining'] == 0 else 'ready')
    except Exception as exc:
        state.update(status='waiting' if isinstance(exc, LockBusy) else 'needs_reconcile' if state.get('pending') else 'retry_wait',
                     error=error_code(exc), next_due_at=clock()+(ACTIVE_RETRY_SECONDS if state.get('run') else RETRY_SECONDS))
    if state['status'] == 'daily_complete' and state.get('unconfirmed_runs'):
        state['status'] = 'daily_complete_with_unknown'
    return save(root, state, clock())


async def _fleet_turn(root) -> ExitStack:
    """The held fleet lock; raises LockBusy after FLEET_WAIT_SECONDS."""
    deadline = time.monotonic() + FLEET_WAIT_SECONDS
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(exclusive_lock(Path(root)/'state/treasure_hunt/fleet.lock'))
            return stack
        except LockBusy:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(FLEET_POLL_SECONDS)


async def run_account(root, account, *, force=False, max_runs=3, clock=time.time,
                      bridge_factory=make_bridge, client_factory=Client):
    target = directory(root, account)
    try:
        guard(root, account, clock())
        with exclusive_lock(target/'run.lock'):
            state = load_state(root, account, clock())
            if not force and (state['status'] in TERMINAL or state.get('next_due_at', 0) > clock()):
                return summarize(state)
            with await _fleet_turn(root):
                async with bridge_factory(Path(root), account) as bridge:
                    try:
                        client = await client_factory(Path(root), account, bridge, clock=clock).open()
                        return await play(root, account, client, state=state, clock=clock, max_runs=max_runs)
                    except Exception as exc:
                        state.update(status='retry_wait', error=error_code(exc),
                                     next_due_at=clock()+(ACTIVE_RETRY_SECONDS if state.get('run') else RETRY_SECONDS))
                        return save(root, state, clock())
    except LockBusy:
        return {'account': account, 'status': 'lock_busy', 'day': day_key(clock())}
    except Exception as exc:
        # Don't overwrite another writer's state or a corrupt/foreign journal.
        result = {'account': account, 'player_id': int(account[2:]), 'flow': FLOW,
                  'status': 'account_failed', 'error': error_code(exc), 'day': day_key(clock()), 'updated_at': clock()}
        if not (target/'state.json').exists():
            write_json(target/'last_result.json', result)
        return result
