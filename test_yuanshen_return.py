import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import yuanshen_automation as y
import yuanshen_return as r
from miniapp_shared.state import read_json, write_json
from test_yuanshen_automation import Bridge, FakeCommander, IDLE, NOW, STARTED, TRAINING

RETURNED = ("【第二元神归位】\n道友 @demo_player_01 的第二元神已结束修炼，回归窍中温养。\n"
            "主魂获得了 6379 点修为，第二元神获得了 3558 点经验。\n"
            "灵智再开！ 第二元神已提升至 5 级，战力增长至 1466507。\n"
            "五子流转：同心 100→100，魔染 0→8。")


def message(**changes):
    fields = dict(id=90, sender_id=7965897083, chat_id=y.CANONICAL_CHAT_ID, message=RETURNED,
                  date=datetime.fromtimestamp(NOW, timezone.utc), fwd_from=None,
                  reply_to=SimpleNamespace(reply_to_msg_id=y.CANONICAL_TOPIC_ID, reply_to_top_id=None))
    fields.update(changes)
    return SimpleNamespace(**fields)


def seed(root):
    state = dict(version=1, account=y.ACCOUNT, actor_id=y.ACTOR_ID, username=y.USERNAME,
                 last_training_started_at=NOW - 86400, training_needs_status=True,
                 training_needs_reconcile=False, retry_until=0,
                 due={"train": NOW + 1800, "purge": NOW + 21600},
                 flow_status={"train": {"status": "training", "at": NOW - 300},
                              "purge": {"status": "moran_below_threshold", "at": NOW - 100}},
                 budget_day=y.day_key(NOW), purge_spends=1, purge_ready_at=NOW + 10000,
                 attempts=[dict(command=y.TRAIN_COMMAND, flow="train", at=NOW - 86401,
                                sent_id=50, watermark=49, status="confirmed",
                                reply=dict(id=60, at=NOW - 86400, sender_id=7965897083, text=STARTED))])
    write_json(root / y.STATE_RELATIVE, state)
    write_json(root / "config/active_flows.json", {"helpers": [{"label": r.TRAIN_LABEL, "enabled": True}]})
    return state


def inbox(root, events=None):
    data = dict(version=1, account=y.ACCOUNT, actor_id=y.ACTOR_ID,
                events=events if events is not None else [r.return_event(message(), NOW)],
                status="healthy", listener_account=y.ACCOUNT, last_scan_at=NOW,
                mode="standalone", listener_label=r.OBSERVER_LABEL, listener_connected=True)
    write_json(root / r.INBOX_RELATIVE, data)
    return data


def cycle(root, texts, *, now=NOW, fake=None):
    fake = fake or FakeCommander(texts, root=root, clock=lambda: now)
    result = asyncio.run(y.run_cycle(root, "train", bridge_factory=lambda *args: Bridge(),
                                    clock=lambda: now, commander_factory=lambda *args, **kwargs: fake))
    return result, fake, read_json(root / y.STATE_RELATIVE)


def test_real_return_sample():
    event = r.return_event(message(), NOW)
    assert event["id"] == 90 and event["topic_id"] == y.CANONICAL_TOPIC_ID
    assert y.moran(event["text"]) == 8
    assert r.return_event(message(message=RETURNED.replace("@demo_player_01", "@demo_player_01")), NOW)


@pytest.mark.parametrize("changes", [
    {"sender_id": 123}, {"chat_id": -1009400000005}, {"fwd_from": object()},
    {"reply_to": None}, {"reply_to": SimpleNamespace(reply_to_msg_id=123, reply_to_top_id=None)},
    {"message": RETURNED.replace("@demo_player_01", "@demo_player_01_other")},
    {"message": RETURNED.replace("@demo_player_01", "@demo_player_02")},
    {"message": RETURNED.replace("【第二元神归位】", "【元神归窍总结】")},
    {"message": RETURNED.replace("已结束修炼", "尚未结束修炼")},
    {"date": datetime.fromtimestamp(NOW + 1, timezone.utc)},
    {"date": datetime.fromtimestamp(NOW - r.MAX_EVENT_AGE - 1, timezone.utc)},
    {"date": None},
])
def test_unattributed_or_noncompletion_message_rejected(changes):
    assert r.return_event(message(**changes), NOW) is None


def test_push_persists_before_nonforced_kick_and_never_writes_training(tmp_path):
    before = seed(tmp_path)
    kicks = []
    async def kick():
        assert read_json(tmp_path / r.INBOX_RELATIVE)["events"][0]["id"] == 90
        assert read_json(tmp_path / y.STATE_RELATIVE) == before
        kicks.append(True)
        return True
    sidecar = r.ReturnObserver(tmp_path, clock=lambda: NOW, kick=kick)
    async def exercise():
        await sidecar.verify(Bridge().client)
        await sidecar.on_message(message())
        await sidecar.on_message(message())
        await sidecar.on_scan([message()])
    asyncio.run(exercise())
    data = read_json(tmp_path / r.INBOX_RELATIVE)
    assert len(kicks) == len(data["events"]) == 1
    assert data["last_scan_at"] == NOW
    assert data["last_kick_status"] == "scheduled"
    assert read_json(tmp_path / y.STATE_RELATIVE) == before


def test_history_recovers_missing_push_and_failed_kick(tmp_path):
    seed(tmp_path)
    async def failed_kick():
        return False
    sidecar = r.ReturnObserver(tmp_path, clock=lambda: NOW, kick=failed_kick)
    asyncio.run(sidecar.on_scan([message()]))
    data = read_json(tmp_path / r.INBOX_RELATIVE)
    assert data["events"][0]["id"] == 90
    assert data["last_kick_status"] == "failed"
    from scripts.return_deadline_dispatch import collect_yuanshen_labels
    assert collect_yuanshen_labels(tmp_path / "state", now_ts=NOW + 30) == [r.TRAIN_LABEL]


def test_wrong_listener_identity_has_no_inbox(tmp_path):
    sidecar = r.ReturnObserver(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        asyncio.run(sidecar.verify(Bridge(uid=123).client))
    assert not (tmp_path / r.INBOX_RELATIVE).exists()


def test_independent_observer_configuration_is_offline(capsys):
    assert r.main(["--check"]) == 0
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload["label"] == r.OBSERVER_LABEL
    assert payload["network_used"] is False
    assert "shendubiguan" not in payload["session_path"]
    assert "yuanyingchuqiao" not in payload["session_path"]


def test_return_preempts_old_timer_and_starts_without_status_probe(tmp_path):
    before = seed(tmp_path)
    inbox(tmp_path)
    result, fake, state = cycle(tmp_path, [STARTED])
    assert result["status"] == "training_started"
    assert fake.sent == [y.TRAIN_COMMAND]
    assert state["due"]["train"] == NOW + 86460
    assert state["last_training_return"]["continued_by_reply_id"] == 111
    for key in ("purge_spends", "purge_ready_at"):
        assert state[key] == before[key]
    assert state["due"]["purge"] == before["due"]["purge"]
    assert state["flow_status"]["purge"] == before["flow_status"]["purge"]
    assert not r.training_dispatch_due(tmp_path, NOW)
    result, fake, _ = cycle(tmp_path, [], now=NOW + 1)
    assert result["status"] == "scheduled_wait" and fake.sent == []


@pytest.mark.parametrize("pending", [False, True])
def test_old_completion_never_unlocks_a_newer_attempt(tmp_path, pending):
    state = seed(tmp_path)
    data = inbox(tmp_path)
    newer = dict(command=y.TRAIN_COMMAND, at=NOW + 1, watermark=95,
                 sent_id=0 if pending else 100, status="unconfirmed" if pending else "confirmed")
    state["attempts"].append(newer)
    if pending:
        state["pending"] = newer
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert r.current_return(state, data, NOW + 2) is None
    assert not r.training_dispatch_due(tmp_path, NOW + 2)


def test_return_does_not_claim_uncertain_next_training_succeeded(tmp_path):
    seed(tmp_path)
    inbox(tmp_path)
    result, fake, state = cycle(tmp_path, [None])
    assert result["status"] == "unconfirmed"
    assert fake.sent == [y.TRAIN_COMMAND]
    assert state["pending"]["sent_id"] == 101
    assert "continued_by_reply_id" not in state["last_training_return"]
    assert state["due"]["train"] == NOW + y.TRAIN_RETRY_SECONDS
    assert not r.training_dispatch_due(tmp_path, NOW + 1)
    result, fake, state = cycle(tmp_path, [None, TRAINING], now=NOW + y.TRAIN_RETRY_SECONDS)
    assert result["status"] == "training"
    assert fake.sent == [y.STATUS_COMMAND]


def test_late_start_ack_cannot_resurrect_completed_cycle_timer(tmp_path):
    state = seed(tmp_path)
    state["pending"] = copy.deepcopy(state["attempts"][-1])
    state["pending"].pop("reply")
    state["pending"]["status"] = "unconfirmed"
    state["training_needs_reconcile"] = True
    write_json(tmp_path / y.STATE_RELATIVE, state)
    inbox(tmp_path)
    class LateAck(FakeCommander):
        async def reply(self, sent_id, *, timeout=35):
            if timeout == 0:
                return {"id": 60, "sender_id": 7965897083, "text": STARTED, "at": NOW - 86400}
            return await super().reply(sent_id, timeout=timeout)
    result, fake, _ = cycle(tmp_path, [], fake=LateAck([STARTED], root=tmp_path))
    assert result["status"] == "training_started"
    assert fake.sent == [y.TRAIN_COMMAND]


def test_same_second_newer_id_return_is_not_lost(tmp_path):
    state = seed(tmp_path)
    state["attempts"].append(dict(command=y.TRAIN_COMMAND, at=NOW + 0.25, sent_id=80, watermark=79,
                                  status="confirmed"))
    assert r.current_return(state, inbox(tmp_path), NOW + 1)["id"] == 90


def test_newer_official_training_panel_supersedes_old_return(tmp_path):
    state = seed(tmp_path)
    data = inbox(tmp_path)
    state["attempts"].append(dict(command=y.STATUS_COMMAND, flow="train", at=NOW + 10, sent_id=101,
        status="confirmed", reply=dict(id=111, at=NOW + 11, sender_id=7965897083, text=TRAINING)))
    state["last_training_return"] = data["events"][0]
    state["due"]["train"] = NOW + 12 * 3600
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert r.current_return(state, data, NOW + 12) is None
    assert not r.training_dispatch_due(tmp_path, NOW + 12)
    result, fake, after = cycle(tmp_path, [], now=NOW + 12)
    assert result["status"] == "scheduled_wait" and fake.sent == []
    assert after["due"]["train"] == state["due"]["train"]


def test_purge_read_timeout_does_not_delay_return_continuation(tmp_path):
    state = seed(tmp_path)
    state.update(retry_flow="purge", retry_until=NOW + 1800)
    state["pending"] = dict(command=y.STATUS_COMMAND, flow="purge", at=NOW - 10, sent_id=70, watermark=69)
    write_json(tmp_path / y.STATE_RELATIVE, state)
    inbox(tmp_path)
    result, fake, after = cycle(tmp_path, [None, STARTED])
    assert result["status"] == "training_started"
    assert fake.sent == [y.TRAIN_COMMAND]
    assert after["purge_spends"] == state["purge_spends"]
    assert after["due"]["purge"] == state["due"]["purge"]


def test_without_push_expired_timer_queries_not_blindly_restarts(tmp_path):
    state = seed(tmp_path)
    state["due"]["train"] = NOW
    write_json(tmp_path / y.STATE_RELATIVE, state)
    result, fake, state = cycle(tmp_path, [TRAINING.replace(" (剩余: 12小时59分钟)", "")])
    assert fake.sent == [y.STATUS_COMMAND]
    assert result["status"] == "training"
    assert state["due"]["train"] == NOW + 60
    assert not r.training_dispatch_due(tmp_path, NOW + 59)
    assert r.training_dispatch_due(tmp_path, NOW + 60)
    result, fake, state = cycle(tmp_path, [IDLE, STARTED], now=NOW + 60)
    assert result["status"] == "training_started"
    assert fake.sent == [y.STATUS_COMMAND, y.TRAIN_COMMAND]


@pytest.mark.parametrize("field", ["train_not_before", "transport_not_before"])
def test_push_respects_explicit_server_waits_without_opening_session(tmp_path, field):
    state = seed(tmp_path)
    state[field] = NOW + 600
    write_json(tmp_path / y.STATE_RELATIVE, state)
    inbox(tmp_path)
    def no_bridge(*args):
        pytest.fail("server wait cannot be bypassed")
    result = asyncio.run(y.run_cycle(tmp_path, "train", clock=lambda: NOW, bridge_factory=no_bridge))
    assert result["status"] == "scheduled_wait"
    assert result["next_due_at"] == NOW + 600
    assert not r.training_dispatch_due(tmp_path, NOW)


@pytest.mark.parametrize("changes", [
    {"last_scan_at": NOW - r.LISTENER_STALE_SECONDS - 1}, {"last_scan_at": NOW + 1},
    {"status": "failed"}, {"status": "closed"}, {"actor_id": 123}, {"listener_account": "tg123"},
    {"mode": "sidecar"}, {"listener_label": "shendubiguan"}, {"listener_connected": False},
])
def test_training_health_requires_fresh_account_bound_listener(tmp_path, monkeypatch, changes):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    seed(tmp_path)
    assert health.yuanshen_helper_health(r.TRAIN_LABEL)[:2] == ("return_listener_missing", "bad")
    data = inbox(tmp_path)
    assert health.yuanshen_helper_health(r.TRAIN_LABEL)[1] == "ok"
    data.update(changes)
    write_json(tmp_path / r.INBOX_RELATIVE, data)
    assert health.yuanshen_helper_health(r.TRAIN_LABEL)[1] == "bad"


@pytest.mark.parametrize("elapsed,kick_status,expected", [
    (0, "failed", "return_dispatch_failed"), (121, "scheduled", "return_dispatch_overdue"),
])
def test_saved_completion_cannot_hide_dispatch_failure(tmp_path, monkeypatch, elapsed, kick_status, expected):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW + elapsed)
    seed(tmp_path)
    data = inbox(tmp_path)
    data["last_kick_status"] = kick_status
    write_json(tmp_path / r.INBOX_RELATIVE, data)
    assert health.yuanshen_helper_health(r.TRAIN_LABEL)[:2] == (expected, "bad")


def test_standalone_push_and_history_share_only_own_client(tmp_path, monkeypatch):
    import transport
    from unittest.mock import AsyncMock
    seed(tmp_path)
    kicks = []
    handlers = []
    async def kick():
        kicks.append(True)
        return True
    observer = r.ReturnObserver(tmp_path, clock=lambda: NOW, kick=kick)
    async def exercise():
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=y.ACTOR_ID, username=y.USERNAME)),
            get_entity=AsyncMock(return_value=SimpleNamespace(id=9400000004)),
            disconnected=asyncio.get_running_loop().create_future(), is_connected=lambda: True,
            add_event_handler=lambda callback, builder: handlers.append(callback))
        async def factory(root):
            assert root == tmp_path
            return client
        async def history(**kwargs):
            assert kwargs['client'] is client and kwargs['scan_limit'] == r.SCAN_LIMIT
            await handlers[0](SimpleNamespace(message=message()))
            await handlers[1](SimpleNamespace(message=message()))
            return [message()]
        async def sleep(seconds):
            assert seconds == 60
        monkeypatch.setattr(transport, "ensure_connected", AsyncMock())
        monkeypatch.setattr(transport, "collect_recent_messages", history)
        disconnect = AsyncMock()
        monkeypatch.setattr(transport, "disconnect_telegram_client", disconnect)
        await r.run_persistent(tmp_path, client_factory=factory, observer_factory=lambda root: observer,
                               max_cycles=2, sleep=sleep)
        disconnect.assert_awaited_once_with(client, 15)
    asyncio.run(exercise())
    data = read_json(tmp_path / r.INBOX_RELATIVE)
    assert data['listener_label'] == r.OBSERVER_LABEL and data['mode'] == 'standalone'
    assert len(data['events']) == len(kicks) == 1
    assert data['last_scan_at'] == NOW
    assert data['status'] == 'closed'


def test_standalone_routine_pass_reads_only_new_messages_without_reverifying(tmp_path, monkeypatch):
    import transport
    from unittest.mock import AsyncMock
    from event_monitor_runtime import HISTORY_OVERLAP_IDS
    seed(tmp_path)
    observer = r.ReturnObserver(tmp_path, clock=lambda: NOW, kick=AsyncMock(return_value=True))

    async def exercise():
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=y.ACTOR_ID, username=y.USERNAME)),
            get_entity=AsyncMock(return_value=SimpleNamespace(id=9400000004)),
            get_messages=AsyncMock(return_value=[]),
            disconnected=asyncio.get_running_loop().create_future(), is_connected=lambda: True,
            add_event_handler=lambda callback, builder: None)
        full_window = AsyncMock(return_value=[message(id=500)])
        monkeypatch.setattr(transport, "ensure_connected", AsyncMock())
        monkeypatch.setattr(transport, "collect_recent_messages", full_window)
        monkeypatch.setattr(transport, "disconnect_telegram_client", AsyncMock())
        await r.run_persistent(tmp_path, client_factory=AsyncMock(return_value=client),
                               observer_factory=lambda root: observer, max_cycles=3, sleep=AsyncMock())
        return client, full_window
    client, full_window = asyncio.run(exercise())
    assert full_window.await_count == 1
    assert client.get_me.await_count == 1
    assert [call.kwargs["min_id"] for call in client.get_messages.await_args_list] == [500 - HISTORY_OVERLAP_IDS] * 2
    assert read_json(tmp_path / r.INBOX_RELATIVE)["last_scan_at"] == NOW


def test_standalone_lock_precedes_session_authorization(tmp_path):
    from miniapp_shared.state import exclusive_lock, LockBusy
    async def no_client(root):
        pytest.fail('duplicate observer must not touch the session')
    with exclusive_lock(tmp_path / r.PROCESS_LOCK_RELATIVE):
        with pytest.raises(LockBusy):
            asyncio.run(r.run_persistent(tmp_path, client_factory=no_client))


def test_deep_retreat_and_yuanying_have_no_second_soul_hook():
    import inspect
    import observe_return
    assert 'return_sidecar' not in inspect.signature(observe_return.main_async).parameters
    assert 'yuanshen' not in inspect.getsource(observe_return.run_persistent_observer)
    for flow in ('shendubiguan', 'yuanyingchuqiao'):
        wrapper = y.ROOT / f'scripts/run_{flow}_return_observer_tg9100000001.sh'
        assert 'TG_SECOND_SOUL_RETURN_SIDECAR' not in wrapper.read_text()


def test_dispatch_obeys_registration_identity_and_kick_dedupe(tmp_path):
    from scripts.return_deadline_dispatch import collect_yuanshen_labels
    seed(tmp_path)
    inbox(tmp_path)
    assert collect_yuanshen_labels(tmp_path / "state", now_ts=NOW) == [r.TRAIN_LABEL]
    write_json(tmp_path / "state/return_deadline_dispatch.json", {"last_kick_by_label": {r.TRAIN_LABEL: NOW}})
    assert collect_yuanshen_labels(tmp_path / "state", now_ts=NOW + 29) == []
    write_json(tmp_path / "config/active_flows.json", {"helpers": [{"label": r.TRAIN_LABEL, "enabled": False}]})
    assert not r.training_dispatch_due(tmp_path, NOW + 30)
    seed(tmp_path)
    data = read_json(tmp_path / r.INBOX_RELATIVE)
    data["actor_id"] = 123
    write_json(tmp_path / r.INBOX_RELATIVE, data)
    assert not r.training_dispatch_due(tmp_path, NOW)
