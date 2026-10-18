"""Regression for official recovery push 12736132, 2026-10-13 15:19:44 CST."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yuanshen_automation as y
import yuanshen_return as r
from miniapp_shared.state import read_json, write_json
from test_yuanshen_automation import NOW, STARTED, TRAINING
from test_yuanshen_return import message, seed, inbox, cycle

RECOVERED = '道友 @demo_player_01，你的第二元神已从【受伤】状态中恢复。'
INJURED = '【你的第二元神：金之元神】\n状态: 受伤\n五子同心魔: 5/5 | 调度 修炼 | 同心 100 | 魔染 56'


def recovered(**changes):
    return message(message=RECOVERED, sender_id=8944702077, **changes)


def injured(root):
    state = seed(root)
    state['due']['train'] = NOW + 21600
    state['flow_status']['train'] = {'status': 'injured', 'at': NOW - 94}
    state['attempts'].append(dict(command=y.STATUS_COMMAND, flow='train', at=NOW - 100,
        sent_id=70, status='confirmed', reply=dict(id=80, at=NOW - 94,
        sender_id=8788895566, text=INJURED)))
    write_json(root / y.STATE_RELATIVE, state)
    return state


def test_exact_official_injury_recovery_sample():
    event = r.return_event(recovered(), NOW)
    assert event is not None
    assert r.completion_kind(event['text']) == 'injury_recovered'
    assert r.completion_kind(RECOVERED.replace('@demo_player_01', '@demo_player_01')) == 'injury_recovered'


@pytest.mark.parametrize('changes', [
    {'sender_id': 123}, {'chat_id': -1009400000005}, {'fwd_from': object()},
    {'reply_to': None}, {'reply_to': SimpleNamespace(reply_to_msg_id=123, reply_to_top_id=None)},
    {'message': RECOVERED.replace('@demo_player_01', '@demo_player_01_other')},
    {'message': RECOVERED.replace('@demo_player_01', '@demo_player_02')},
    {'message': RECOVERED.replace('已从', '尚未从')},
    {'message': RECOVERED.replace('【受伤】', '【心魔】')},
    {'message': RECOVERED + ' 请重发'},
    {'date': datetime.fromtimestamp(NOW + 1, timezone.utc)},
    {'date': datetime.fromtimestamp(NOW - r.MAX_EVENT_AGE - 1, timezone.utc)},
])
def test_recovery_source_identity_wording_and_age_are_required(changes):
    fields = dict(message=RECOVERED, sender_id=8944702077)
    fields.update(changes)
    assert r.return_event(message(**fields), NOW) is None


@pytest.mark.parametrize('history', [False, True])
def test_recovery_push_and_history_release_injury_wait_once(tmp_path, history):
    before = injured(tmp_path)
    kicks = []
    async def kick():
        assert read_json(tmp_path / y.STATE_RELATIVE) == before
        assert read_json(tmp_path / r.INBOX_RELATIVE)['events'][0]['text'] == RECOVERED
        kicks.append(True)
        return True
    async def observe():
        observer = r.ReturnObserver(tmp_path, clock=lambda: NOW, kick=kick)
        for _ in range(2):
            if history:
                await observer.on_scan([recovered()])
            else:
                await observer.on_message(recovered())
    asyncio.run(observe())
    assert len(kicks) == 1
    assert r.training_dispatch_due(tmp_path, NOW)
    result, fake, after = cycle(tmp_path, [STARTED])
    assert result['status'] == 'training_started'
    assert fake.sent == [y.TRAIN_COMMAND]
    assert after['last_training_return']['continued_by_reply_id'] == 111
    assert after['due']['purge'] == before['due']['purge']
    assert after['purge_spends'] == before['purge_spends']
    result, fake, after = cycle(tmp_path, [], now=NOW + 30)
    assert fake.sent == [] and result['status'] == 'scheduled_wait'


def test_old_recovery_cannot_interrupt_newer_training_panel(tmp_path):
    state = injured(tmp_path)
    data = inbox(tmp_path, [r.return_event(recovered(), NOW)])
    state['attempts'].append(dict(command=y.STATUS_COMMAND, flow='train', at=NOW + 10,
        sent_id=101, status='confirmed', reply=dict(id=111, at=NOW + 11,
        sender_id=7965897083, text=TRAINING)))
    state['due']['train'] = NOW + 12 * 3600
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert r.current_return(state, data, NOW + 12) is None
    result, fake, _ = cycle(tmp_path, [], now=NOW + 12)
    assert fake.sent == [] and result['status'] == 'scheduled_wait'


@pytest.mark.parametrize('key', ['train_not_before', 'transport_not_before'])
def test_recovery_does_not_bypass_server_or_flood_wait(tmp_path, key):
    state = injured(tmp_path)
    state[key] = NOW + 3600
    write_json(tmp_path / y.STATE_RELATIVE, state)
    inbox(tmp_path, [r.return_event(recovered(), NOW)])
    assert not r.training_dispatch_due(tmp_path, NOW)
    result, fake, after = cycle(tmp_path, [])
    assert fake.sent == []
    assert after[key] == NOW + 3600


def test_recovery_is_not_a_receipt_for_next_training(tmp_path):
    injured(tmp_path)
    inbox(tmp_path, [r.return_event(recovered(), NOW)])
    result, fake, after = cycle(tmp_path, [None])
    assert result['status'] == 'unconfirmed' and fake.sent == [y.TRAIN_COMMAND]
    assert 'continued_by_reply_id' not in after['last_training_return']
    assert after['pending']['sent_id'] == 101
    result, fake, _ = cycle(tmp_path, [], now=NOW + 1)
    assert fake.sent == []
