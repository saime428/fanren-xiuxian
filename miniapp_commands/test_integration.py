import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# root and FIX are pytest fixtures resolved by name; ruff must not drop them.
from .test_commands import root, NOW, DEMO_01, YIN, FIX, FakeClient, reply, board, run  # noqa: F401
from .catalog import STATE_FILE, VERSION, state_dir
from miniapp_shared.state import read_json, write_json


@pytest.mark.parametrize("module_name,status", [
    ("send_topic_message", "request_failed"), ("send_topic_message", "active"),
    ("send_shendubiguan", "request_failed"), ("send_shendubiguan", "active"),
])
def test_legacy_entry_cannot_send_after_miniapp_routing(monkeypatch, module_name, status):
    import importlib
    from miniapp_commands import runner
    module = importlib.import_module(module_name)
    async def route(**kwargs): return {"status": status}
    def forbidden(*a, **kw): raise AssertionError("legacy Telegram path was reached")
    monkeypatch.setattr(runner, "route_from_environment", route)
    monkeypatch.setattr(module, "telegram_client", forbidden)
    if status == "request_failed":
        error = module.ExpectedFlowExit if module_name == "send_topic_message" else SystemExit
        with pytest.raises(error): asyncio.run(module.run_once())
    else:
        asyncio.run(module.run_once())


@pytest.mark.parametrize("status,code", [("ready",0),("active",10),("scheduled_wait",10),("unknown_outcome",11)])
def test_legacy_yuan_probe_uses_miniapp_only(monkeypatch, status, code):
    from miniapp_commands import runner
    import probe_yuanyingchuqiao_status as probe
    async def route(**kwargs):
        assert kwargs["probe_only"] is True
        return {"status":status}
    monkeypatch.setattr(runner,"route_from_environment",route)
    assert asyncio.run(probe._main_async()) == code


def test_cli_api_failure_is_not_the_legacy_transport_exit(monkeypatch):
    from miniapp_commands import run_cycle
    async def failed(*a,**kw): return {"status":"request_failed"}
    monkeypatch.setattr(run_cycle,"migrated",lambda *a:True)
    monkeypatch.setattr(run_cycle,"run_flow",failed)
    assert run_cycle.main(["--account",YIN,"--flow","xuexishanlin"]) == 1


@pytest.mark.parametrize("status",["lock_busy","transport_busy"])
def test_immediate_observer_retries_busy_miniapp_instead_of_consuming_the_return(status):
    from miniapp_commands.run_cycle import result_exit_code
    assert result_exit_code({"status":status},observer_immediate=True) == 75
    assert result_exit_code({"status":status}) == 0


def test_daily_runner_moves_only_checkin_and_preserves_the_other_commands(root, monkeypatch):
    from miniapp_commands import runner as api_runner
    from sect_daily.runner import run_account
    from sect_daily.test_sect_daily import FakeClient as GroupClient, FakeBridge, FakeClock, PANEL_DONE
    client=GroupClient({".边境军功":PANEL_DONE})
    calls=[]
    async def checkin(given_root,account,flow,**kwargs):
        calls.append((given_root,account,flow))
        return {"status":"already_checked_in","last_response":{"message":"今日已点卯"}}
    monkeypatch.setattr(api_runner,"run_flow",checkin)
    clock=FakeClock()
    result=asyncio.run(run_account(root,DEMO_01,day_key="2026-10-09",now=NOW,
        bridge_factory=lambda *_:FakeBridge(client),sleep=clock.sleep,monotonic=clock.now))
    assert result["status"] == "completed"
    assert calls == [(root,DEMO_01,"sect_checkin")]
    assert client.sent == [".边境军功"]
    assert result["steps"]["checkin"]["transport"] == "miniapp"
    assert "sent_message_id" not in result["steps"]["checkin"]


def test_daily_miniapp_error_does_not_send_checkin_to_group(root,monkeypatch):
    from miniapp_commands import runner as api_runner
    from sect_daily.runner import run_account
    from sect_daily.test_sect_daily import FakeClient as GroupClient, FakeBridge, FakeClock, PANEL_DONE
    client=GroupClient({".边境军功":PANEL_DONE})
    async def failed(*a,**kw): return {"status":"request_failed"}
    monkeypatch.setattr(api_runner,"run_flow",failed)
    clock=FakeClock()
    result=asyncio.run(run_account(root,DEMO_01,day_key="2026-10-09",now=NOW,
        bridge_factory=lambda *_:FakeBridge(client),sleep=clock.sleep,monotonic=clock.now))
    assert result["status"] == "partial" and result["steps"]["checkin"]["status"] == "unconfirmed"
    assert ".宗门点卯" not in client.sent


@pytest.mark.parametrize("api_status,expected",[("success","command_confirmed"),
    ("unknown_outcome","command_result_unconfirmed"),("cooldown","reply_retry_pending")])
def test_event_monitor_uses_miniapp_result_without_a_fake_telegram_receipt(root,monkeypatch,api_status,expected):
    from telethon import types
    from test_question_reply_monitor import FakeClient as EventClient
    from event_monitor_runtime import EventMonitorHealth
    from question_reply_monitor import QuestionReplyMonitor, CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID
    from miniapp_commands import runner as api_runner
    import question_reply_monitor as module
    calls=[]
    async def api(given_root,account,flow,**kwargs):
        calls.append((given_root,account,flow,kwargs))
        return {"status":api_status,"next_due_ts":NOW+300,"last_response":{"message":"fixture"}}
    async def gate(action,**kwargs): return await action()
    monkeypatch.setattr(api_runner,"run_flow",api)
    monkeypatch.setattr(module,"send_with_global_gate",gate)
    async def exercise():
        client=EventClient()
        primary=types.Channel(id=9400000004,title="fixture",photo=types.ChatPhotoEmpty(),
                              date=datetime.fromtimestamp(NOW,timezone.utc),megagroup=True)
        directory=state_dir(root,DEMO_01,"shenji_budao");directory.mkdir(parents=True)
        health=EventMonitorHealth(directory,DEMO_01,"shenji_budao","send",["@example_game_group"],60)
        try:
            monitor=QuestionReplyMonitor(client,health,[("@example_game_group",primary)],
                username="demo_player_01",trusted_sender_ids={8388633812},flow="shenji_budao",state_dir=directory,
                state_path=directory/"last_success.json",max_age=900,scan_limit=100,reconcile_seconds=60,
                reply_text=".神迹 布道",clock=lambda:NOW)
            assert health.data["command_transport"] == "miniapp"
            assert health.data["command_transport_version"] == VERSION
            message=SimpleNamespace(id=7319999,sender_id=8388633812,chat_id=CANONICAL_CHAT_ID,entities=[],
                fwd_from=None,date=datetime.fromtimestamp(NOW-1,timezone.utc),
                reply_to=SimpleNamespace(reply_to_msg_id=CANONICAL_TOPIC_ID,reply_to_top_id=None),
                message="【小世界·天降浩劫】 @demo_player_01 请速速查看 .小世界，安抚信徒。")
            client.messages[(CANONICAL_CHAT_ID,message.id)]=message
            monitor.observe(message,CANONICAL_CHAT_ID)
            key=monitor.key(CANONICAL_CHAT_ID,message.id)
            await monitor.dispatch(key)
            assert monitor.rows[key]["status"] == expected
            assert monitor.rows[key]["transport"] == "miniapp"
            assert client.sends == []
            assert "sent_message_id" not in monitor.rows[key]
            assert monitor.deadline(monitor.rows[key]) == float("inf")
        finally: health.close()
    asyncio.run(exercise())
    assert len(calls)==1 and calls[0][:3] == (root,DEMO_01,"shenji_budao")
    assert calls[0][3]["lock_held"] is True


def test_dispatcher_uses_miniapp_deadline_and_ignores_old_group_pending(root):
    from scripts.return_deadline_dispatch import collect_result_labels, collect_unconfirmed_labels, collect_due_labels
    directory=state_dir(root,YIN,"shendubiguan");directory.mkdir(parents=True)
    write_json(directory/STATE_FILE,{"account":YIN,"flow":"shendubiguan","runtime_version":VERSION,
                                    "status":"active","next_due_ts":NOW-1})
    labels=collect_result_labels(root/"state",registry_path=root/"config/active_flows.json",now_ts=NOW)
    assert labels==[f"ai.hermes.xiuxian.{YIN}.shendubiguan.send"]
    assert collect_unconfirmed_labels(root/"state",registry_path=root/"config/active_flows.json",now_ts=NOW)==[]
    assert collect_due_labels(root/"state",registry_path=root/"config/active_flows.json",now_ts=NOW)==[]


def test_health_prefers_new_api_failure_over_old_success(root, monkeypatch):
    from scripts import xiuxian_healthcheck as hc
    fake=FakeClient(overview=[reply(deep=board(remaining=1000))]);run(root,"shendubiguan",fake)
    monkeypatch.setattr(hc,"STATE_ROOT",root/"state/accounts")
    monkeypatch.setattr(hc,"XIUXIAN_ROOT",root)
    monkeypatch.setattr(hc.time,"time",lambda:NOW)
    kwargs=dict(account=YIN,flow="shendubiguan",kind="send",proc_text="",active=True,
                log_signal_cache={},command_transport="miniapp")
    assert hc.classify_kind(**kwargs).severity == "ok"
    path=state_dir(root,YIN,"shendubiguan")/STATE_FILE
    state=read_json(path);state.update(status="unknown_outcome",pending={"at":NOW});write_json(path,state)
    item=hc.classify_kind(**kwargs)
    assert item.severity == "warn" and item.status == "unknown_outcome"


def test_new_receipts_are_in_the_existing_dossier_collector(root):
    from automation_dossiers import evidence_paths,summarize_file
    fake=FakeClient(overview=[reply(deep=board())]);run(root,"shendubiguan",fake)
    files=evidence_paths(root,f"ai.hermes.xiuxian.{YIN}.shendubiguan.send",{"account":YIN,"flow":"shendubiguan"})
    paths={p.name:p for p in files}
    assert "miniapp_last_result.json" in paths and "miniapp_state.json" in paths
    assert "monitor_health.json" in paths
    result=summarize_file(paths["miniapp_last_result.json"],root)
    assert result["transport"] == "miniapp" and result["confirmed"] is True
    assert "message" not in result and "history" not in result


@pytest.mark.parametrize("issue,expected",[(None,"monitor_healthy"),
    ("failure","monitor_failed"),("unconfirmed","monitor_question_unresolved")])
def test_event_watchdog_uses_real_listener_evidence_before_first_api_event(root,monkeypatch,issue,expected):
    from scripts import xiuxian_healthcheck as hc
    from event_monitor_runtime import RUNTIME_VERSION,question_monitor_version
    directory=state_dir(root,DEMO_01,"shenji_budao")
    payload={"account":DEMO_01,"flow":"shenji_budao","kind":"send","pid":3210,
        "runtime_version":RUNTIME_VERSION,"question_monitor_version":question_monitor_version("shenji_budao"),
        "command_transport":"miniapp","command_transport_version":VERSION,
        "ts":NOW,"started_at_ts":NOW-100,"last_success_ts":NOW-1,"listener_verified_ts":NOW-1,
        "reconcile_seconds":60,"source_targets":["@example_game_group","-1009400000005"],
        "status":"healthy","listener_connected":True}
    if issue=="failure":payload.update(status="failed",error_type="RuntimeError",error_stage="history")
    if issue=="unconfirmed":payload["business_issue"]={"code":"command_result_unconfirmed","message_id":101}
    write_json(directory/"monitor_health.json",payload)
    monkeypatch.setattr(hc,"STATE_ROOT",root/"state/accounts")
    monkeypatch.setattr(hc,"XIUXIAN_ROOT",root)
    monkeypatch.setattr(hc.time,"time",lambda:NOW)
    item=hc.classify_kind(DEMO_01,"shenji_budao","watchdog","1000 3210 python auto_reply_topic_event.py",True,{},command_transport="miniapp")
    assert item.kind=="watchdog" and item.status==expected
    assert item.severity == ("ok" if issue is None else "bad")
    assert item.last_success_status is None
    assert not (directory/STATE_FILE).exists()
