import asyncio
from types import SimpleNamespace

import pytest

import yuanshen_automation as y
from miniapp_shared.state import exclusive_lock, read_json, write_json

NOW = 1790676000.0
IDLE = "【你的第二元神：金之元神】\n状态: 窍中温养\n魔染 40"
HIGH = "【你的第二元神：金之元神】\n状态: 修炼中 (剩余: 12小时59分钟)\n五子同心魔: 同心100 | 魔染91"
LOW = "【你的第二元神：金之元神】\n状态: 窍中温养\n五子同心魔: 同心100 | 魔染39"
PURGED = "【元神镇魔】\n你耗去 5000 点修为，强行镇压识海中翻腾的五魔。\n魔染度: 91 → 39\n同心度: 100 → 100"
STARTED = "你的第二元神已开始闭关修炼，本次修炼将持续24小时。"
TRAINING = "【你的第二元神：金之元神】\n状态: 修炼中 (剩余: 12小时59分钟)\n魔染 40"
BUSY = "你的第二元神正在(修炼中)，无法分心修炼。"


class FakeCommander:
    def __init__(self, texts, *, root=None, clock=lambda: NOW, fail_send=False):
        self.texts = list(texts)
        self.sent = []
        self.root, self.clock, self.fail_send = root, clock, fail_send
        self.recoveries = 0

    def guard(self):
        pass

    async def watermark(self):
        return 100

    async def send(self, command):
        if self.root:
            state = read_json(self.root / y.STATE_RELATIVE)
            assert state["pending"]["command"] == command
            assert state["pending"]["status"] == "unconfirmed"
        self.sent.append(command)
        if self.fail_send:
            raise TimeoutError("secret string never logged")
        return 100 + len(self.sent)

    async def recover_id(self, attempt):
        self.recoveries += 1
        return attempt.get("sent_id", 0)

    async def reply(self, sent_id, *, timeout=35):
        text = self.texts.pop(0)
        return None if text is None else {"id": sent_id + 10, "sender_id": 7228205882,
                                          "text": text, "at": self.clock()}


def run(tmp_path, texts, flow="train", state=None, now=NOW):
    commander = FakeCommander(texts, root=tmp_path, clock=lambda: now)
    runner = y.Runner(tmp_path, state or {}, commander, clock=lambda: now)
    result = asyncio.run(runner.run(flow))
    # Offline fixture for the independent return listener's business-health check.
    from yuanshen_return import INBOX_RELATIVE, OBSERVER_LABEL
    write_json(tmp_path / INBOX_RELATIVE, {"version": 1, "account": y.ACCOUNT, "actor_id": y.ACTOR_ID,
        "listener_account": y.ACCOUNT, "status": "healthy", "last_scan_at": now, "events": [],
        "listener_label": OBSERVER_LABEL, "mode": "standalone", "listener_connected": True})
    return result, commander, read_json(tmp_path / y.STATE_RELATIVE)


@pytest.mark.parametrize("text,expected", [("12小时59分钟", 46740), ("1天2小时3分4秒", 93784), ("", 0)])
def test_duration(text, expected):
    assert y.duration(text) == expected


@pytest.mark.parametrize("text,wait", [
    ("冷却中，还需2小时，本次修炼24小时", 7200),  # not 26h: only the remaining time counts
    ("請等待2小時", 7200),                       # traditional wording is a cooldown, not "unknown"
    ("元神修炼冷却中，请等待2小时", 7200),
])
def test_cooldown_wait_is_the_anchored_remaining_time(text, wait):
    for command in (y.STATUS_COMMAND, y.TRAIN_COMMAND):
        assert y.parse_reply(command, text) == {"status": "cooldown", "wait": wait}


def test_reference_real_samples():
    assert y.parse_reply(y.STATUS_COMMAND, TRAINING) == {"status": "training", "wait": 46740, "moran": 40}
    assert y.parse_reply(y.PURGE_COMMAND, PURGED) == {
        "status": "purged", "moran_before": 91, "moran": 39, "cultivation_cost": 5000}
    assert y.parse_reply(y.TRAIN_COMMAND, STARTED)["wait"] == 86400
    assert y.moran("魔染91→39") == 39
    assert y.moran("魔染101") is None
    assert y.parse_reply(y.TRAIN_COMMAND, "你的第二元神已开始闭关修炼，将在24小时后为你带来修为增长。") == {
        "status": "training_started", "wait": 86400}
    assert y.parse_reply(y.STATUS_COMMAND, "【你的第二元神：金之元神】\n\n等级: 1 级\n经验: 0 / 500\n"
        "战力: 1265695\n状态: 修炼中 (剩余: 15小时18分钟40秒)\n"
        "五子同心魔: 5/5 | 调度 修炼 | 同心 47 | 魔染 0") == {
        "status": "training", "wait": 55120, "moran": 0}


@pytest.mark.parametrize("text", ["【元神镇魔】修为不足", "【元神镇魔】魔染度: 91 → 39", "魔染度: 40",
                                  PURGED.replace("91 → 39", "39 → 91")])
def test_purge_refusal_not_success(text):
    assert y.parse_reply(y.PURGE_COMMAND, text)["status"] == "unknown"


def test_training_command_first_no_client_idle_requirement(tmp_path):
    result, commander, state = run(tmp_path, [STARTED])
    assert commander.sent == [y.TRAIN_COMMAND]
    assert result["status"] == "training_started"
    assert state["due"]["train"] == NOW + 86460
    assert "purge" not in state["due"]


def test_purge_runs_independently_during_training(tmp_path):
    result, commander, state = run(tmp_path, [HIGH, PURGED], flow="purge", state={"due": {"train": NOW + 86400}})
    assert commander.sent == [y.STATUS_COMMAND, y.PURGE_COMMAND]
    assert result["status"] == "purged"
    assert state["purge_ready_at"] == NOW + 21600
    assert state["due"]["train"] == NOW + 86400


def test_low_moran_does_not_spend(tmp_path):
    result, commander, state = run(tmp_path, [LOW], flow="purge")
    assert commander.sent == [y.STATUS_COMMAND]
    assert result["status"] == "moran_below_threshold"
    assert state["due"]["purge"] == NOW + 21600
    assert state["attempts"][-1]["flow"] == "purge"
    assert y.DEMON_COMMAND not in y.COMMANDS


def test_low_moran_waits_six_hours_without_opening_session(tmp_path):
    _, _, state = run(tmp_path, [LOW], flow="purge")
    def no_bridge(*args):
        pytest.fail("scheduled checks must not open Telegram")
    for elapsed in (300, 3600, 7200, 21599):
        result = asyncio.run(y.run_cycle(tmp_path, "purge", bridge_factory=no_bridge,
                                        clock=lambda: NOW + elapsed))
        assert result["status"] == "scheduled_wait"
    result, commander, _ = run(tmp_path, [LOW], flow="purge", state=state, now=NOW + 21600)
    assert result["status"] == "moran_below_threshold"
    assert commander.sent == [y.STATUS_COMMAND]


@pytest.mark.parametrize("phase", ["窍中温养", "修炼中", "受伤", "心魔试炼中"])
def test_purge_reads_corruption_independently_of_soul_phase(tmp_path, phase):
    panel = HIGH.replace("修炼中", phase)
    result, commander, _ = run(tmp_path, [panel, PURGED], flow="purge")
    assert result["status"] == "purged"
    assert commander.sent == [y.STATUS_COMMAND, y.PURGE_COMMAND]


def test_missing_corruption_does_not_spend_or_query_demons(tmp_path):
    result, commander, state = run(tmp_path, ["【你的第二元神：金之元神】\n状态: 窍中温养"], flow="purge")
    assert result["status"] == "unknown_moran"
    assert commander.sent == [y.STATUS_COMMAND]
    assert state["purge_spends"] == 0


def test_busy_refusal_queries_actual_remaining_time(tmp_path):
    result, commander, state = run(tmp_path, [BUSY, TRAINING])
    assert commander.sent == [y.TRAIN_COMMAND, y.STATUS_COMMAND]
    assert result["status"] == "training"
    assert state["due"]["train"] == NOW + 46740 + 60
    assert "purge" not in state["due"]


@pytest.mark.parametrize("reply,status", [("你尚未凝练第二元神", "not_unlocked"), ("陌生文本", "unknown"),
    ("元神修炼冷却中，请等待2小时", "cooldown")])
def test_rejected_training_is_not_success(tmp_path, reply, status):
    result, commander, _ = run(tmp_path, [reply])
    assert result["status"] == status
    assert commander.sent == [y.TRAIN_COMMAND]


def test_heart_demon_is_manual_and_purge_independent(tmp_path):
    result, commander, state = run(tmp_path, ["你的第二元神正在(心魔试炼中)，无法分心修炼。",
        IDLE.replace("窍中温养", "心魔试炼中")])
    assert result["status"] == "manual_heart_demon"
    assert commander.sent == [y.TRAIN_COMMAND, y.STATUS_COMMAND]
    assert "purge" not in state["due"]


def test_budget_limit_only_blocks_purge(tmp_path):
    result, commander, state = run(tmp_path, [], flow="purge",
                                  state={"budget_day": y.day_key(NOW), "purge_spends": 2})
    assert result["status"] == "purge_budget_exhausted"
    assert commander.sent == []
    assert "train" not in state["due"]
    result, commander, _ = run(tmp_path, [STARTED], state=state)
    assert result["status"] == "training_started"


def test_exhausted_budget_does_not_open_session(tmp_path):
    write_json(tmp_path / y.STATE_RELATIVE, {
        "version": 1, "account": y.ACCOUNT, "actor_id": y.ACTOR_ID,
        "budget_day": y.day_key(NOW), "purge_spends": 2,
    })
    def no_bridge(*args):
        pytest.fail("exhausted budget must not open Telegram")
    result = asyncio.run(y.run_cycle(tmp_path, "purge", bridge_factory=no_bridge, clock=lambda: NOW))
    assert result["status"] == "purge_budget_exhausted"
    assert result["due"]["purge"] == y.next_midnight_ts(NOW) + y.BUFFER_SECONDS


def test_no_hardcoded_reduction_or_immediate_second_purge(tmp_path):
    result, commander, state = run(tmp_path, [HIGH, PURGED.replace("91 → 39", "91 → 80")], flow="purge")
    assert result["status"] == "purged"
    assert commander.sent.count(y.PURGE_COMMAND) == 1
    assert state["moran"] == 80
    assert state["purge_ready_at"] == NOW + 21600


def test_daily_budget_rollover(tmp_path):
    _, _, state = run(tmp_path, [HIGH, PURGED], flow="purge", state={"budget_day": "2020-01-01", "purge_spends": 2})
    assert state["purge_spends"] == 1


def test_no_ack_keeps_durable_pending(tmp_path):
    commander = FakeCommander([None], root=tmp_path)
    runner = y.Runner(tmp_path, {}, commander, clock=lambda: NOW)
    with pytest.raises(y.Deferred):
        asyncio.run(runner.run("train"))
    state = read_json(tmp_path / y.STATE_RELATIVE)
    assert state["pending"]["sent_id"] == 101
    assert state["retry_until"] == NOW + y.TRAIN_RETRY_SECONDS
    assert state["training_needs_reconcile"]
    result, commander, state = run(tmp_path, [None, TRAINING], state=state, now=NOW + y.TRAIN_RETRY_SECONDS)
    assert result["status"] == "training"
    assert commander.sent == [y.STATUS_COMMAND]


def test_late_training_ack_does_not_restart_training(tmp_path):
    commander = FakeCommander([None], root=tmp_path)
    with pytest.raises(y.Deferred):
        asyncio.run(y.Runner(tmp_path, {}, commander, clock=lambda: NOW).run("train"))
    result, commander, state = run(tmp_path, [STARTED], state=read_json(tmp_path / y.STATE_RELATIVE), now=NOW + 1800)
    assert result["status"] == "scheduled_wait"
    assert commander.sent == []
    assert state["attempts"][0]["status"] == "confirmed"


def test_uncertain_purge_reserves_six_hours_and_budget(tmp_path):
    commander = FakeCommander([HIGH, None], root=tmp_path)
    with pytest.raises(y.Deferred):
        asyncio.run(y.Runner(tmp_path, {}, commander, clock=lambda: NOW).run("purge"))
    state = read_json(tmp_path / y.STATE_RELATIVE)
    assert state["purge_spends"] == 1
    assert state["purge_ready_at"] == NOW + 21600


def test_six_hour_restart_early_skip_and_exact_boundary(tmp_path):
    _, _, state = run(tmp_path, [HIGH, PURGED], flow="purge")
    state = read_json(tmp_path / y.STATE_RELATIVE)
    result = asyncio.run(y.run_cycle(tmp_path, "purge", clock=lambda: NOW + 21599))
    assert result["status"] == "scheduled_wait"
    result, commander, _ = run(tmp_path, [HIGH, PURGED], flow="purge", state=state, now=NOW + 21600)
    assert result["status"] == "purged"
    assert commander.sent == [y.STATUS_COMMAND, y.PURGE_COMMAND]


def test_server_cooldown_extends_but_never_shortens_success_floor(tmp_path):
    runner = y.Runner(tmp_path, {"last_purge_confirmed_at": NOW, "purge_ready_at": NOW + 21600},
                      FakeCommander([]), clock=lambda: NOW + 100)
    attempt = {"command": y.PURGE_COMMAND}
    def reply(text):
        return {"id": 200, "sender_id": 7228205882, "text": text, "at": NOW + 100}
    runner.record_reply(attempt, reply("元神镇魔冷却中，请等待1小时"))
    assert runner.state["purge_ready_at"] == NOW + 21600
    runner.record_reply(attempt, reply("元神镇魔冷却中，请等待8小时"))
    assert runner.state["purge_ready_at"] == NOW + 100 + 28800 + 60


class Bridge:
    def __init__(self, uid=y.ACTOR_ID, username=y.USERNAME):
        async def get_me():
            return SimpleNamespace(id=uid, username=username)
        self.client = SimpleNamespace(get_me=get_me)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_wrong_account_fails_before_command(tmp_path):
    result = asyncio.run(y.run_cycle(tmp_path, "train", bridge_factory=lambda *args: Bridge(uid=123), clock=lambda: NOW))
    assert result["status"] == "failed"
    state = read_json(tmp_path / y.STATE_RELATIVE)
    assert "attempts" not in state and "pending" not in state  # only the transport backoff
    assert state["transport_not_before"] == NOW + y.RETRY_SECONDS


@pytest.mark.parametrize("seconds,backoff", [(300, 300 + y.BUFFER_SECONDS), (0, y.RETRY_SECONDS)])
def test_failure_before_runner_exists_still_backs_off_the_kicks(tmp_path, seconds, backoff):
    from yuanshen_return import TRAIN_LABEL, training_dispatch_due
    write_json(tmp_path / "config/active_flows.json", {"helpers": [{"label": TRAIN_LABEL, "enabled": True}]})
    write_json(tmp_path / y.STATE_RELATIVE, {"version": 1, "account": y.ACCOUNT, "actor_id": y.ACTOR_ID,
                                            "due": {"train": NOW - 10}})
    assert training_dispatch_due(tmp_path, NOW)
    class FloodWait(Exception):
        pass
    error = FloodWait()
    error.seconds = seconds
    class FailingBridge(Bridge):
        async def __aenter__(self):
            raise error
    result = asyncio.run(y.run_cycle(tmp_path, "train", bridge_factory=lambda *args: FailingBridge(),
                                    clock=lambda: NOW))
    assert result["status"] == "failed" and result["next_due_at"] == NOW + backoff
    assert read_json(tmp_path / y.STATE_RELATIVE)["transport_not_before"] == NOW + backoff
    # The return listener no longer re-kicks this run on every group message.
    assert not training_dispatch_due(tmp_path, NOW + 1)


def test_corrupt_state_is_not_reset(tmp_path):
    path = tmp_path / y.STATE_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("invalid")
    result = asyncio.run(y.run_cycle(tmp_path, "train", clock=lambda: NOW))
    assert result["status"] == "invalid_state"
    assert path.read_text() == "invalid"


def test_shared_lock_prevents_parallel_wrappers(tmp_path):
    with exclusive_lock(tmp_path / y.STATE_RELATIVE.parent / "runner.lock"):
        result = asyncio.run(y.run_cycle(tmp_path, "train", clock=lambda: NOW))
    assert result["status"] == "lock_busy"


def test_qyz_guard_before_network(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 29, 13, 40, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    result = asyncio.run(y.run_cycle(tmp_path, "train", clock=lambda: now))
    assert result["status"] == "yielding_to_qyz"
    assert not (tmp_path / y.STATE_RELATIVE).exists()


def test_official_reply_exact_parent_only(tmp_path):
    messages = [SimpleNamespace(id=200 + n, sender_id=sender, reply_to_msg_id=parent,
        fwd_from=forward, message="text", date=SimpleNamespace(timestamp=lambda: NOW), chat_id=y.CANONICAL_CHAT_ID)
        for n, (sender, parent, forward) in enumerate([(123, 101, None), (7228205882, 999, None),
            (7228205882, 101, object()), (7228205882, 101, None)])]
    async def iterate(*args, **kwargs):
        for message in messages:
            yield message
    commander = y.Commander(SimpleNamespace(iter_messages=iterate), tmp_path, clock=lambda: NOW)
    assert asyncio.run(commander.reply(101, timeout=0))["id"] == 203


def test_transport_failure_is_sanitized_and_journal_survives(tmp_path):
    fake = FakeCommander([], root=tmp_path, fail_send=True)
    result = asyncio.run(y.run_cycle(tmp_path, "train", bridge_factory=lambda *args: Bridge(), clock=lambda: NOW,
                                    commander_factory=lambda *args, **kwargs: fake))
    assert result["status"] == "failed"
    assert result["error"] == "TimeoutError"
    state = read_json(tmp_path / y.STATE_RELATIVE)
    assert state["pending"]["sent_id"] == 0
    assert "secret" not in str(state)


def test_purge_failure_preserves_training_deadline(tmp_path):
    due = NOW + 86400
    write_json(tmp_path / y.STATE_RELATIVE, {
        "version": 1, "account": y.ACCOUNT, "actor_id": y.ACTOR_ID,
        "due": {"train": due}, "training_needs_reconcile": False,
    })
    fake = FakeCommander([], root=tmp_path, fail_send=True)
    result = asyncio.run(y.run_cycle(tmp_path, "purge", bridge_factory=lambda *args: Bridge(),
                                    clock=lambda: NOW, commander_factory=lambda *args, **kwargs: fake))
    assert result["status"] == "failed"
    assert read_json(tmp_path / y.STATE_RELATIVE)["due"]["train"] == due
    assert asyncio.run(y.run_cycle(tmp_path, "train", clock=lambda: NOW + 1801))["status"] == "scheduled_wait"


@pytest.mark.parametrize("fail_send", [False, True])
def test_purge_query_failure_does_not_change_training_outcome(tmp_path, fail_send):
    _, _, before = run(tmp_path, [STARTED])
    fake = FakeCommander([None], root=tmp_path, fail_send=fail_send)
    result = asyncio.run(y.run_cycle(tmp_path, "purge", bridge_factory=lambda *args: Bridge(),
                                    clock=lambda: NOW, commander_factory=lambda *args, **kwargs: fake))
    assert result["status"] == ("failed" if fail_send else "unconfirmed")
    after = read_json(tmp_path / y.STATE_RELATIVE)
    assert after["flow_status"]["train"] == before["flow_status"]["train"]
    assert after["due"]["train"] == before["due"]["train"]


def test_known_training_refusal_uses_status_fallback_when_no_push_arrives(tmp_path):
    _, _, state = run(tmp_path, [BUSY, TRAINING])
    assert state["training_needs_reconcile"] is False
    result, commander, _ = run(tmp_path, [IDLE, STARTED], state=state, now=state["due"]["train"])
    assert result["status"] == "training_started"
    assert commander.sent == [y.STATUS_COMMAND, y.TRAIN_COMMAND]


def test_unknown_training_reply_still_requires_reconciliation(tmp_path):
    _, _, state = run(tmp_path, ["陌生文本"])
    assert state["training_needs_reconcile"] is True
    result, commander, _ = run(tmp_path, [TRAINING], state=state, now=state["due"]["train"])
    assert result["status"] == "training"
    assert commander.sent == [y.STATUS_COMMAND]


def test_late_training_cooldown_prevents_immediate_retry(tmp_path):
    commander = FakeCommander([None], root=tmp_path)
    with pytest.raises(y.Deferred):
        asyncio.run(y.Runner(tmp_path, {}, commander, clock=lambda: NOW).run("train"))
    result, commander, state = run(tmp_path, ["元神修炼冷却中，请等待2小时"],
                                  state=read_json(tmp_path / y.STATE_RELATIVE), now=NOW + 1800)
    assert result["status"] == "scheduled_wait"
    assert commander.sent == []
    assert state["due"]["train"] == NOW + 1800 + 7200 + 60


def test_helpers_are_registered_without_generic_sender_state():
    import json
    from flow_registry import active_entries
    entries = active_entries(json.loads((y.ROOT / "config/active_flows.json").read_text()))
    for name in ("zhenmo", "xiulian"):
        label = f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-{name}"
        matching = [entry for entry in entries if entry.get("label") == label]
        assert len(matching) == 1
        assert matching[0]["kinds"] == ["helper"]


@pytest.mark.parametrize("flow,label,texts", [
    ("train", "xiulian", [BUSY, TRAINING]), ("purge", "zhenmo", [LOW]),
])
def test_helper_health_requires_business_evidence(tmp_path, monkeypatch, flow, label, texts):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    label = f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-{label}"
    assert health.yuanshen_helper_health(label)[1] == "bad"
    _, _, state = run(tmp_path, texts, flow=flow)
    assert health.yuanshen_helper_health(label)[1] == "ok"
    state["flow_status"][flow] = {"status": "failed", "at": NOW}
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(label)[:2] == ("failed", "bad")


@pytest.mark.parametrize("phase", ["窍中温养", "修炼中", "受伤", "心魔试炼中"])
def test_purge_panel_does_not_replace_training_health_evidence(tmp_path, monkeypatch, phase):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    _, _, state = run(tmp_path, [STARTED])
    _, _, state = run(tmp_path, [LOW.replace("窍中温养", phase)], flow="purge", state=state)
    train_health = health.yuanshen_helper_health(f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-xiulian")
    purge_health = health.yuanshen_helper_health(f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-zhenmo")
    assert train_health[:2] == ("training_started", "ok")
    assert purge_health[:2] == ("moran_below_threshold", "ok")
    state["pending"] = {"command": y.STATUS_COMMAND, "flow": "purge"}
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-xiulian")[1] == "ok"
    assert health.yuanshen_helper_health(f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-zhenmo")[1] == "bad"


def test_health_accepts_legacy_demon_panel_journal(tmp_path, monkeypatch):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    _, _, state = run(tmp_path, [LOW], flow="purge")
    attempt = state["attempts"][-1]
    attempt.pop("flow")
    attempt["command"] = y.DEMON_COMMAND
    attempt["reply"]["text"] = "【五子同心魔】\n魔染度: 39 / 100"
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-zhenmo")[1] == "ok"


def test_helper_health_rejects_wrong_reply_and_overdue(tmp_path, monkeypatch):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    label = f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-xiulian"
    _, _, state = run(tmp_path, [STARTED])
    state["due"]["train"] = NOW - 901
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(label)[:2] == ("overdue", "bad")
    state["due"]["train"] = NOW + 86400
    state["attempts"][-1]["reply"]["sender_id"] = 123
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(label)[:2] == ("unconfirmed", "bad")


def test_helper_scheduler_success_cannot_hide_failed_business(tmp_path, monkeypatch):
    from scripts import xiuxian_healthcheck as health
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    monkeypatch.setattr(health, "launchctl_snapshot", lambda label: "state = not running\nlast exit code = 0\n")
    _, _, state = run(tmp_path, [STARTED])
    state["flow_status"]["train"] = {"status": "failed", "at": NOW}
    write_json(tmp_path / y.STATE_RELATIVE, state)
    result = health.classify_helper({"label": f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-xiulian",
                                    "account": y.ACCOUNT, "flow": "yuanshen-xiulian"}, set())
    assert result.severity == "bad"
    assert result.status == "failed"


def test_helper_health_allows_bounded_battle_deferral(tmp_path, monkeypatch):
    from scripts import xiuxian_healthcheck as health
    from miniapp_shared import priority
    monkeypatch.setattr(health, "XIUXIAN_ROOT", tmp_path)
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    monkeypatch.setattr(priority, "qyz_battle_window", lambda *args: {"active": True})
    label = f"ai.hermes.xiuxian.{y.ACCOUNT}.yuanshen-xiulian"
    _, _, state = run(tmp_path, [STARTED])
    state["due"]["train"] = NOW - 1000
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(label)[:2] == ("yielding_to_qyz", "ok")
    state["due"]["train"] = NOW - 3601
    write_json(tmp_path / y.STATE_RELATIVE, state)
    assert health.yuanshen_helper_health(label)[:2] == ("overdue", "bad")


def test_check_is_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(y, "ROOT", tmp_path)
    assert y.main("train", ["--check"]) == 0
    assert '"network_used": false' in capsys.readouterr().out
    assert not (tmp_path / "state").exists()
