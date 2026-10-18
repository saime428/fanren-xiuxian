import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import yuanshen_automation as y
import yuanshen_heart_demon as h
import yuanshen_return as r
from miniapp_shared.state import read_json, write_json
from test_yuanshen_return import seed
from test_yuanshen_automation import NOW

WARNING = ('【天道警示·心魔试炼】\n道友 @demo_player_01 的第二元神在修炼中遭遇心魔，道心动摇！\n'
           '你必须立即为其做出抉择：\n\n'
           '1. 回复本消息 .抉择 强行突破 (高风险，高回报)\n'
           '2. 回复本消息 .抉择 稳固道心 (低风险，低回报)')
SUCCESS = '【破而后立·成功】\n你选择了最凶险的道路，并成功战胜了心魔！第二元神获得了 8646 点经验！'
FAILED = '【破而后立·失败】\n你的第二元神受伤，需要休养24小时。'


def message(text=WARNING, **changes):
    fields = dict(id=90, sender_id=7965897083, chat_id=y.CANONICAL_CHAT_ID, message=text,
                  date=datetime.fromtimestamp(NOW - 10, timezone.utc), edit_date=None, fwd_from=None,
                  reply_to=SimpleNamespace(reply_to_msg_id=y.CANONICAL_TOPIC_ID, reply_to_top_id=None))
    fields.update(changes)
    return SimpleNamespace(**fields)


async def gate(action, **kwargs):
    return await action()


class Client:
    def __init__(self, root):
        self.root = root
        self.current = {90: message()}
        self.sent = []
        self.error = None

    async def get_me(self):
        return SimpleNamespace(id=y.ACTOR_ID, username=y.USERNAME)

    async def get_messages(self, chat, *, ids):
        assert chat == y.CANONICAL_CHAT_ID
        return self.current.get(ids)

    async def send_message(self, chat, text, *, reply_to):
        assert read_json(self.root / h.STATE_RELATIVE)['events'][str(reply_to)]['status'] == 'send_intent'
        self.sent.append((chat, text, reply_to))
        if self.error:
            raise self.error
        return SimpleNamespace(id=100)


def setup(root):
    before = seed(root)
    write_json(root / 'config/active_flows.json', {'helpers': [
        {'label': r.TRAIN_LABEL, 'enabled': True},
        {'label': r.OBSERVER_LABEL, 'enabled': True, 'heart_demon_choice': h.CHOICE_COMMAND}]})
    handler = h.HeartDemonResponder(root, clock=lambda: NOW, send_gate=gate)
    client = Client(root)
    handler.client = client
    return handler, client, before


def row(root):
    return read_json(root / h.STATE_RELATIVE)['events']['90']


def test_exact_owned_warning_and_original_date():
    assert h.warning(message(), NOW)['id'] == 90
    assert h.warning(message(WARNING.replace('@demo_player_01', '@demo_player_01')), NOW)
    old = message(date=datetime.fromtimestamp(NOW - 3601, timezone.utc),
                  edit_date=datetime.fromtimestamp(NOW, timezone.utc))
    assert h.warning(old, NOW) is None


@pytest.mark.parametrize('changes', [
    {'sender_id': 1}, {'chat_id': -1009400000005}, {'fwd_from': object()}, {'reply_to': None},
    {'reply_to': SimpleNamespace(reply_to_msg_id=5, reply_to_top_id=None)},
    {'message': WARNING.replace('@demo_player_01', '@demo_player_01_other')},
    {'message': WARNING.replace('@demo_player_01', '@demo_player_02')},
    {'message': WARNING.replace('稳固道心', '强行突破')},
    {'message': WARNING.replace('稳固道心', '稳固道心诀')}, {'message': SUCCESS},
    {'date': None}, {'date': datetime.fromtimestamp(NOW + 1, timezone.utc)}, {'id': 0},
])
def test_untrusted_unattributed_or_incomplete_warning_does_not_authorize(changes):
    assert h.warning(message(**changes), NOW) is None


def test_push_history_and_restart_send_only_once_and_never_write_training(tmp_path):
    handler, client, before = setup(tmp_path)
    async def exercise():
        await handler.process([message()])
        await handler.process([message()], reconcile=True)
        restarted = h.HeartDemonResponder(tmp_path, clock=lambda: NOW, send_gate=gate)
        restarted.client = client
        await restarted.process([message()], reconcile=True)
    asyncio.run(exercise())
    assert client.sent == [(y.CANONICAL_CHAT_ID, '.抉择 稳固道心', 90)]
    assert row(tmp_path)['sent_id'] == 100
    assert row(tmp_path)['attempted_command'] == row(tmp_path)['sent_command'] == '.抉择 稳固道心'
    assert read_json(tmp_path / y.STATE_RELATIVE) == before


@pytest.mark.parametrize('text,status', [(SUCCESS, 'break_success'), (FAILED, 'break_failed'),
                                       ('【稳扎稳打·成功】\n成功渡过心魔试炼。', 'stable_success')])
def test_terminal_edit_is_bound_to_original_warning_without_username(tmp_path, text, status):
    handler, client, before = setup(tmp_path)
    async def exercise():
        await handler.process([message()])
        await handler.process([message(text, edit_date=datetime.fromtimestamp(NOW, timezone.utc))])
    asyncio.run(exercise())
    assert row(tmp_path)['status'] == status
    assert h.current_resolution(tmp_path, before, NOW)['id'] == 90
    assert r.training_dispatch_due(tmp_path, NOW)


@pytest.mark.parametrize('changes', [{'id': 777}, {'sender_id': 7228205882},
                                    {'chat_id': -1009400000005}, {'fwd_from': object()}])
def test_other_players_result_never_resolves_our_choice(tmp_path, changes):
    handler, client, before = setup(tmp_path)
    async def exercise():
        await handler.process([message()])
        await handler.process([message(SUCCESS, edit_date=datetime.fromtimestamp(NOW, timezone.utc), **changes)])
    asyncio.run(exercise())
    assert row(tmp_path)['status'] == 'sent'
    assert h.current_resolution(tmp_path, before, NOW) is None


def test_rechecks_current_anchor_before_claiming_a_send_slot(tmp_path):
    handler, client, before = setup(tmp_path)
    # The pushed warning was resolved manually before the choice was made.
    client.current[90] = message(SUCCESS, edit_date=datetime.fromtimestamp(NOW, timezone.utc))
    slots = []
    async def counting_gate(action, **kwargs):
        slots.append(1)
        return await action()
    handler.send_gate = counting_gate
    asyncio.run(handler.process([message()]))
    assert not client.sent and not slots
    assert row(tmp_path)['status'] == 'break_success'


def test_send_slot_covers_only_the_recheck_and_the_send(tmp_path):
    handler, client, before = setup(tmp_path)
    inside = []
    read = client.get_messages
    async def guarded_read(chat, *, ids):
        assert not inside, 'anchor read while holding a global send slot'
        return await read(chat, ids=ids)
    client.get_messages = guarded_read
    async def gate(action, **kwargs):
        inside.append(1)
        try:
            return await action()
        finally:
            inside.clear()
    handler.send_gate = gate
    asyncio.run(handler.process([message()]))
    assert client.sent == [(y.CANONICAL_CHAT_ID, '.抉择 稳固道心', 90)]


@pytest.mark.parametrize('current', [None, message(id=777), message(sender_id=7228205882),
                                   message(date=datetime.fromtimestamp(NOW - 20, timezone.utc))])
def test_fresh_anchor_identity_must_still_match(tmp_path, current):
    handler, client, before = setup(tmp_path)
    client.current[90] = current
    asyncio.run(handler.process([message()]))
    assert not client.sent


def test_ambiguous_send_survives_restart_without_blind_replay(tmp_path):
    handler, client, before = setup(tmp_path)
    client.error = TimeoutError('must not expose exception details')
    async def exercise():
        await handler.process([message()])
        restarted = h.HeartDemonResponder(tmp_path, clock=lambda: NOW, send_gate=gate)
        restarted.client = client
        await restarted.process([message()], reconcile=True)
    asyncio.run(exercise())
    assert len(client.sent) == 1
    assert row(tmp_path)['status'] == 'delivery_unconfirmed'
    assert 'must not expose' not in (tmp_path / h.STATE_RELATIVE).read_text()


def test_reconciles_own_reply_when_rpc_return_was_lost(tmp_path):
    handler, client, before = setup(tmp_path)
    client.error = TimeoutError()
    own = message(h.CHOICE_COMMAND, id=100, sender_id=y.ACTOR_ID,
                  reply_to=SimpleNamespace(reply_to_msg_id=90, reply_to_top_id=y.CANONICAL_TOPIC_ID))
    async def exercise():
        await handler.process([message()])
        await handler.process([own], reconcile=True)
    asyncio.run(exercise())
    assert len(client.sent) == 1
    assert row(tmp_path)['status'] == 'sent'
    assert row(tmp_path)['sent_id'] == 100


@pytest.mark.parametrize('command', ['.抉择 稳固道心', '.抉择 强行突破'])
def test_existing_manual_or_legacy_choice_is_not_overridden(tmp_path, command):
    handler, client, before = setup(tmp_path)
    own = message(command, id=99, sender_id=y.ACTOR_ID,
                  reply_to=SimpleNamespace(reply_to_msg_id=90, reply_to_top_id=y.CANONICAL_TOPIC_ID))
    asyncio.run(handler.process([message(), own], reconcile=True))
    assert not client.sent
    assert row(tmp_path)['sent_command'] == command


def test_saved_anchor_is_reconciled_after_it_leaves_recent_history(tmp_path):
    handler, client, before = setup(tmp_path)
    async def exercise():
        await handler.process([message()])
        client.current[90] = message(SUCCESS, edit_date=datetime.fromtimestamp(NOW, timezone.utc))
        await handler.process([], reconcile=True)
    asyncio.run(exercise())
    assert row(tmp_path)['status'] == 'break_success'


@pytest.mark.parametrize('blocked', ['policy', 'paused', 'qyz', 'generation', 'flood_wait'])
def test_live_gates_prevent_choice(tmp_path, monkeypatch, blocked):
    handler, client, before = setup(tmp_path)
    if blocked == 'policy':
        write_json(tmp_path / 'config/active_flows.json', {'helpers': []})
    elif blocked == 'paused':
        write_json(tmp_path / 'state/account_lifecycle.json', {'version': 1, 'outbox': {},
                   'accounts': {y.ACCOUNT: {'status': 'paused'}}})
    elif blocked == 'qyz':
        monkeypatch.setattr(h, 'yield_to_qyz', lambda *args: {'status': 'yielding'})
    elif blocked == 'flood_wait':
        before['transport_not_before'] = NOW + 100
        write_json(tmp_path / y.STATE_RELATIVE, before)
    else:
        before['last_training_started_at'] = NOW
        write_json(tmp_path / y.STATE_RELATIVE, before)
    asyncio.run(handler.process([message()]))
    assert not client.sent


@pytest.mark.parametrize('text', [SUCCESS, FAILED, '【稳扎稳打·成功】\n成功渡过心魔试炼。'])
def test_runner_alone_applies_result_and_preserves_purge_state(tmp_path, text):
    handler, client, before = setup(tmp_path)
    async def exercise():
        await handler.process([message()])
        await handler.process([message(text, edit_date=datetime.fromtimestamp(NOW, timezone.utc))])
    asyncio.run(exercise())
    state = copy.deepcopy(before)
    runner = y.Runner(tmp_path, state, None, clock=lambda: NOW)
    assert runner.apply_return()
    assert state['training_needs_status'] is True
    assert state['due']['purge'] == before['due']['purge']
    assert state['purge_spends'] == before['purge_spends']
    assert state['purge_ready_at'] == before['purge_ready_at']
    assert state['due']['train'] == (NOW + 86400 + y.BUFFER_SECONDS if text == FAILED else NOW)
    assert h.current_resolution(tmp_path, state, NOW) is None
    next_cycle = copy.deepcopy(before)
    next_cycle['last_training_started_at'] = NOW + 1
    assert h.current_resolution(tmp_path, next_cycle, NOW + 2) is None


def test_existing_observer_uses_its_verified_client(tmp_path):
    handler, client, before = setup(tmp_path)
    observer = r.ReturnObserver(tmp_path, clock=lambda: NOW)
    observer.heart_demon.send_gate = gate
    async def exercise():
        await observer.verify(client)
        await observer.on_message(message())
    asyncio.run(exercise())
    assert client.sent == [(y.CANONICAL_CHAT_ID, h.CHOICE_COMMAND, 90)]
    assert read_json(tmp_path / r.INBOX_RELATIVE)['heart_demon_policy'] == h.POLICY_VERSION


def test_registered_policy_only_enables_stable_choice():
    root = Path(__file__).resolve().parent
    assert h.policy_enabled(root)
    assert h.CHOICE_COMMAND == '.抉择 稳固道心'


def test_registry_is_parsed_once_until_it_changes(tmp_path, monkeypatch):
    path = tmp_path / 'config/active_flows.json'
    write_json(path, {'helpers': []})
    parsed = []
    monkeypatch.setattr(h, 'read_json', lambda target: parsed.append(target) or read_json(target))
    assert not h.policy_enabled(tmp_path) and not h.policy_enabled(tmp_path)
    assert len(parsed) == 1
    write_json(path, {'helpers': [{'label': h.OBSERVER_LABEL, 'enabled': True,
                                   'heart_demon_choice': h.CHOICE_COMMAND}]})
    assert h.policy_enabled(tmp_path)
    assert len(parsed) == 2


def test_old_unsafe_config_does_not_enable_sender(tmp_path):
    handler, client, before = setup(tmp_path)
    write_json(tmp_path / 'config/active_flows.json', {'helpers': [
        {'label': r.OBSERVER_LABEL, 'enabled': True, 'heart_demon_choice': '.抉择 强行突破'}]})
    asyncio.run(handler.process([message()]))
    assert not client.sent


def test_unsafe_only_warning_never_falls_back_to_breakthrough(tmp_path):
    handler, client, before = setup(tmp_path)
    unsafe_only = message(WARNING.split('2. 回复本消息')[0])
    client.current[90] = unsafe_only
    asyncio.run(handler.process([unsafe_only], reconcile=True))
    assert not client.sent


def test_stable_choice_does_not_require_unsafe_option(tmp_path):
    handler, client, before = setup(tmp_path)
    stable_only = message(WARNING.replace('1. 回复本消息 .抉择 强行突破 (高风险，高回报)\n', ''))
    client.current[90] = stable_only
    asyncio.run(handler.process([stable_only], reconcile=True))
    assert client.sent == [(y.CANONICAL_CHAT_ID, '.抉择 稳固道心', 90)]


def test_stable_option_is_rechecked_on_the_current_anchor(tmp_path):
    handler, client, before = setup(tmp_path)
    client.current[90] = message(WARNING.split('2. 回复本消息')[0])
    asyncio.run(handler.process([message()]))
    assert not client.sent


@pytest.mark.parametrize('status', ['observed', 'send_intent', 'delivery_unconfirmed', 'sent'])
def test_policy_upgrade_keeps_previous_delivery_state(tmp_path, status):
    handler, client, before = setup(tmp_path)
    data = handler.load()
    handler.observe(data, message())
    event = data['events']['90']
    event['status'] = status
    if status == 'sent':
        event['sent_id'] = 99
    data['policy_version'] = 'heart-demon-break-v1'
    write_json(tmp_path / h.STATE_RELATIVE, data)
    asyncio.run(handler.process([message()], reconcile=True))
    expected = [(y.CANONICAL_CHAT_ID, '.抉择 稳固道心', 90)] if status == 'observed' else []
    assert client.sent == expected
    assert row(tmp_path)['status'] == ('sent' if status == 'observed' else status)


def test_lost_legacy_reply_is_reconciled_without_new_choice(tmp_path):
    handler, client, before = setup(tmp_path)
    data = handler.load()
    handler.observe(data, message())
    data['events']['90']['status'] = 'delivery_unconfirmed'
    data['policy_version'] = 'heart-demon-break-v1'
    write_json(tmp_path / h.STATE_RELATIVE, data)
    own = message('.抉择 强行突破', id=99, sender_id=y.ACTOR_ID,
                  reply_to=SimpleNamespace(reply_to_msg_id=90, reply_to_top_id=y.CANONICAL_TOPIC_ID))
    asyncio.run(handler.process([own], reconcile=True))
    assert not client.sent
    assert row(tmp_path)['sent_id'] == 99
    assert row(tmp_path)['sent_command'] == '.抉择 强行突破'
