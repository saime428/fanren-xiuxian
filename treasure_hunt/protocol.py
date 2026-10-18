"""Strict projections of the official hunt replies; never retain authentication."""
from __future__ import annotations

import math
import re
from typing import Any

from miniapp_shared.dwelling import MiniAppError

PREFIX = '/api/miniapp/xianxia-dwelling/'
TYPES = frozenset({'empty', 'herb', 'mine', 'clue', 'trap', 'monster', 'chest', 'main'})


class HuntError(MiniAppError):
    """A hunt-flow MiniAppError: same ``code``/``uncertain`` contract, same error_code()."""


def number(value: Any, *, minimum=0, maximum=10**12) -> int:
    if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value:
        raise HuntError('hunt_invalid_number')
    if not minimum <= value <= maximum:
        raise HuntError('hunt_number_out_of_range')
    return int(value)


def text(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str) or len(value) > limit or re.search(r'[\x00-\x08\x0b-\x1f]', value):
        raise HuntError('hunt_invalid_text')
    if re.search(r'(?i)https?://|tgWebApp|initData|token=|signature=', value):
        raise HuntError('hunt_sensitive_text')
    return value


def successful(reply: dict) -> None:
    if not isinstance(reply, dict) or reply.get('ok') is not True:
        raise HuntError('hunt_response_not_successful')


def owner(reply: dict, player_id: int) -> None:
    successful(reply)
    account = reply.get('account')
    if not isinstance(account, dict) or type(account.get('playerId')) not in (int, str):
        raise HuntError('hunt_response_identity_missing')
    if str(account['playerId']) != str(player_id):
        raise HuntError('hunt_response_identity_mismatch')
    if account.get('authUserId') is not None and str(account['authUserId']) != str(player_id):
        raise HuntError('hunt_auth_identity_mismatch')


def quota(reply: dict, player_id: int, *, known_dwelling: bool | None = None) -> dict:
    owner(reply, player_id)
    dwelling = reply.get('dwelling')
    if not isinstance(dwelling, dict):
        raise HuntError('hunt_dwelling_missing')
    present = dwelling.get('hasDwelling', known_dwelling)
    # Mutation replies are partial snapshots. Only an identity-verified details
    # read may supply the missing flag; an unverified patch is not sufficient.
    if type(present) is not bool:
        raise HuntError('hunt_dwelling_missing')
    if not present:
        return {'has_dwelling': False, 'used': 0, 'limit': 0, 'remaining': 0, 'actionPoints': 0}
    value = dwelling.get('hunt')
    if not isinstance(value, dict):
        raise HuntError('hunt_quota_missing')
    result = {k: number(value.get(k), maximum=100) for k in ('used', 'limit', 'remaining', 'actionPoints')}
    if result['remaining'] != max(0, result['limit'] - result['used']):
        raise HuntError('hunt_quota_inconsistent')
    return {'has_dwelling': True, **result}


def loot(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise HuntError('hunt_loot_invalid')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise HuntError('hunt_loot_invalid')
        result.append({'name': text(item.get('name'), 100), 'quantity': number(item.get('quantity'))})
    return result


def hint(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HuntError('hunt_hint_invalid')
    result = {'text': text(value.get('text', ''), 1000)}
    if 'markers' in value:
        if not isinstance(value['markers'], list) or len(value['markers']) > 25:
            raise HuntError('hunt_markers_invalid')
        result['markers'] = []
        for marker in value['markers']:
            if not isinstance(marker, dict) or marker.get('kind') not in ('risk', 'treasure', 'resource'):
                raise HuntError('hunt_marker_invalid')
            result['markers'].append({'index': number(marker.get('index'), maximum=24), 'kind': marker['kind']})
    return result


def run(reply: dict, *, session_id: str | None = None) -> dict:
    successful(reply)
    value = reply.get('huntRun')
    if not isinstance(value, dict):
        raise HuntError('hunt_run_missing')
    sid = value.get('sessionId')
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', sid):
        raise HuntError('hunt_session_invalid')
    if session_id is not None and sid != session_id:
        raise HuntError('hunt_session_mismatch')
    result = {'sessionId': sid, 'size': number(value.get('size'), minimum=5, maximum=5),
              'ap': number(value.get('ap'), maximum=100), 'maxAp': number(value.get('maxAp'), minimum=1, maximum=100),
              'score': number(value.get('score'), maximum=100),
              'revealedCount': number(value.get('revealedCount'), maximum=25)}
    if result['ap'] > result['maxAp'] or type(value.get('foundMain')) is not bool:
        raise HuntError('hunt_run_invalid')
    if value.get('status') not in ('active', 'failed', 'completed', 'settled'):
        raise HuntError('hunt_status_unknown')
    result.update(status=value['status'], foundMain=value['foundMain'], loot=loot(value.get('loot')))
    cells = value.get('cells')
    if not isinstance(cells, list) or len(cells) != 25:
        raise HuntError('hunt_board_invalid')
    result['cells'] = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, dict) or cell.get('index') != i or type(cell.get('revealed')) is not bool:
            raise HuntError('hunt_cell_invalid')
        current = {'index': i, 'revealed': cell['revealed']}
        # Hidden fields are deliberately ignored. Only observations and hints count.
        if cell['revealed']:
            if cell.get('type') not in TYPES:
                raise HuntError('hunt_cell_type_unknown')
            current['type'] = cell['type']
            if cell.get('hint') is not None:
                current['hint'] = hint(cell['hint'])
            if cell.get('loot') is not None:
                current['loot'] = loot([cell['loot']])[0]
        result['cells'].append(current)
    if sum(c['revealed'] for c in result['cells']) != result['revealedCount']:
        raise HuntError('hunt_revealed_count_mismatch')
    result['latestHint'] = hint(value.get('latestHint'))
    # The current public UI has no challenge action. Preserve the flag and
    # settle that run instead of guessing an undocumented mutation.
    result['pendingChallenge'] = bool(value.get('pendingChallenge'))
    return result


def settlement(reply: dict, player_id: int) -> dict:
    owner(reply, player_id)
    value = reply.get('huntResult')
    if not isinstance(value, dict):
        raise HuntError('hunt_settlement_missing')
    if type(value.get('foundMain')) is not bool:
        raise HuntError('hunt_settlement_invalid')
    return {'score': number(value.get('score'), maximum=100), 'grade': text(value.get('grade'), 30),
            'contribution': number(value.get('contribution')), 'loot': loot(value.get('loot')),
            'foundMain': value['foundMain'], 'revealedCount': number(value.get('revealedCount'), maximum=25)}
