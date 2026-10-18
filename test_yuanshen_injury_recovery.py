"""Replay a confirmed injury deadline with a still-injured official panel."""
import asyncio

import pytest

import yuanshen_automation as y
import yuanshen_return as returns
from miniapp_shared.state import read_json, write_json
from test_yuanshen_automation import Bridge, FakeCommander, STARTED

NOW = 1791889000.0
SENDER = 8788895566
INJURED = '【你的第二元神：金之元神】\n状态: 受伤\n魔染 56'


def panel(mid, at, text, flow='train'):
    return {'command': y.STATUS_COMMAND, 'flow': flow, 'at': at - 4, 'sent_id': mid - 1,
            'watermark': mid - 2, 'status': 'confirmed',
            'reply': {'id': mid, 'at': at, 'sender_id': SENDER, 'text': text},
            'parsed': y.parse_reply(y.STATUS_COMMAND, text)}


def state():
    at = NOW - 1000
    return {'version': 1, 'account': y.ACCOUNT, 'actor_id': y.ACTOR_ID,
            'last_training_started_at': NOW - 2 * 86400, 'training_needs_status': True,
            'due': {'train': at + 21600, 'purge': NOW + 500},
            'flow_status': {'train': {'status': 'injured', 'at': at}},
            'purge_spends': 1, 'budget_day': y.day_key(NOW), 'retry_until': 0,
            'attempts': [panel(100, NOW - 4000, INJURED.replace('受伤', '受伤 (剩余: 40分钟)'), 'purge'),
                         panel(200, at, INJURED)]}


def cycle(root, data, replies, *, now=NOW):
    write_json(root / y.STATE_RELATIVE, data)
    commander = FakeCommander(replies, root=root, clock=lambda: now)
    result = asyncio.run(y.run_cycle(root, 'train', clock=lambda: now,
        bridge_factory=lambda *a: Bridge(), commander_factory=lambda *a, **kw: commander))
    return result, commander, read_json(root / y.STATE_RELATIVE)


def test_expired_fallback_rechecks_and_confirms_one_resume(tmp_path):
    before = state()
    result, cmd, saved = cycle(tmp_path, before, [INJURED, STARTED])
    assert cmd.sent == [y.STATUS_COMMAND, y.TRAIN_COMMAND]
    assert result['status'] == 'training_started'
    assert saved['due']['train'] == NOW + 86460
    assert saved['due']['purge'] == before['due']['purge']
    assert saved['purge_spends'] == before['purge_spends']


def test_dispatcher_admits_expired_fallback_without_writing_state(tmp_path):
    before = state();write_json(tmp_path / y.STATE_RELATIVE, before)
    write_json(tmp_path / 'config/active_flows.json', {'helpers': [{'label': returns.TRAIN_LABEL, 'enabled': True}]})
    assert returns.training_dispatch_due(tmp_path, NOW)
    assert read_json(tmp_path / y.STATE_RELATIVE) == before


@pytest.mark.parametrize('field', ['train_not_before', 'transport_not_before', 'retry_until'])
def test_real_deadlines_are_never_shortened(tmp_path, field):
    data = state();data[field] = NOW + 1000;data['retry_flow'] = 'train'
    result, cmd, saved = cycle(tmp_path, data, [])
    assert result['status'] == 'scheduled_wait'
    assert cmd.sent == []
    assert saved[field] == data[field]


@pytest.mark.parametrize('change', ['missing', 'wrong_sender', 'wrong_account', 'old_generation', 'still_counting'])
def test_resume_needs_current_account_official_elapsed_countdown(tmp_path, change):
    data = state()
    if change == 'missing':data['attempts'] = data['attempts'][1:]
    if change == 'wrong_sender':data['attempts'][0]['reply']['sender_id'] = 123
    if change == 'wrong_account':data['account'] = 'tgOTHER'
    if change == 'old_generation':data['last_training_started_at'] = NOW - 2000
    if change == 'still_counting':data['attempts'][0] = panel(100, NOW - 4000, INJURED.replace('受伤', '受伤 (剩余: 3小时)'), 'purge')
    result, cmd, _ = cycle(tmp_path, data, [])
    assert cmd.sent == []
    assert result['status'] in {'scheduled_wait', 'invalid_state'}


def test_new_countdown_blocks_probe_after_old_injury_expired(tmp_path):
    result, cmd, saved = cycle(tmp_path, state(), [INJURED.replace('受伤', '受伤 (剩余: 30分钟)')])
    assert cmd.sent == [y.STATUS_COMMAND]
    assert result['status'] == 'injured'
    assert saved['due']['train'] == NOW + 1860


def test_uncertain_resume_is_not_replayed_against_same_injury(tmp_path):
    data = state();data['due']['train'] = NOW - 1
    data['training_needs_reconcile'] = True
    data['attempts'].append({'command': y.TRAIN_COMMAND, 'flow': 'train', 'at': NOW - 100,
                             'status': 'unconfirmed', 'sent_id': 201, 'watermark': 200})
    result, cmd, saved = cycle(tmp_path, data, [INJURED])
    assert cmd.sent == [y.STATUS_COMMAND]
    assert result['status'] == 'injured'
    assert saved['due']['train'] <= NOW + 300


def test_rejected_probe_is_not_sent_again_after_restart(tmp_path):
    rejected = '你的第二元神正在(受伤)，无法分心修炼。'
    result, cmd, saved = cycle(tmp_path, state(), [INJURED, rejected, INJURED])
    assert cmd.sent == [y.STATUS_COMMAND, y.TRAIN_COMMAND, y.STATUS_COMMAND]
    assert result['status'] == 'injured'
    result, cmd, saved = cycle(tmp_path, saved, [INJURED], now=NOW + 301)
    assert cmd.sent == [y.STATUS_COMMAND]
    assert result['status'] == 'injured'


@pytest.mark.parametrize('remaining', [0, 1, 59])
def test_recovery_includes_the_full_official_deadline_buffer(tmp_path, remaining):
    data = state();data['due']['train'] = NOW - 1
    data['attempts'][0] = panel(100, NOW - 60 + remaining,
                               INJURED.replace('受伤', '受伤 (剩余: 1秒)'), 'purge')
    result, cmd, _ = cycle(tmp_path, data, [INJURED])
    assert cmd.sent == [y.STATUS_COMMAND]
    assert result['status'] == 'injured'


def test_fallback_reconciliation_does_not_shorten_an_unrelated_future_due(tmp_path):
    data = state();data['due']['train'] = NOW + 8000
    result, cmd, saved = cycle(tmp_path, data, [])
    assert result['status'] == 'scheduled_wait'
    assert cmd.sent == []
    assert saved['due']['train'] == data['due']['train']
