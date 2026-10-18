import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from miniapp_commands import client as client_module
from miniapp_commands.catalog import ACCOUNTS, VERSION, STATE_FILE, RECEIPT_FILE, migrated, state_dir
from miniapp_commands.client import CommandClient
from miniapp_commands.protocol import yuan_board
from miniapp_commands.receipts import health, valid_receipt
from miniapp_commands.runner import run_flow
from miniapp_shared.dwelling import DwellingSession, JsonTransport, MiniAppError
from miniapp_shared.state import read_json, write_json, exclusive_lock

NOW = 1791507600.0
YIN = "tg9100000002"
DEMO_01 = "tg9100000001"
FIX = "tg9100000006"


@pytest.fixture
def root(tmp_path):
    (tmp_path/"config").mkdir()
    write_json(tmp_path/"config/active_flows.json", {
        "flows": [{"account": a, "flow": f, "enabled": True, "kinds": ["send"], "transport": "miniapp"}
                  for f, accounts in ACCOUNTS.items() if f != "sect_checkin" for a in accounts],
        "helpers": [{"label": "ai.hermes.xiuxian.sect-daily", "enabled": True, "miniapp_commands": [".宗门点卯"]}],
    })
    return tmp_path


def reply(account=YIN, text="", *, ok=True, deep=None, world=None):
    value = {"ok": True, "account": {"playerId": int(account[2:]), "authUserId": int(account[2:])},
             "actionResult": {"ok": ok, "rawMessage": text, "completed": True}}
    if deep is not None:
        value["dwelling"] = {"meditation": {"deepSeclusion": deep}}
    if world is not None:
        value["account"]["smallWorld"] = world
    return value


def board(*, active=True, remaining=3600, completed=False):
    return {"active": active, "remainingSeconds": remaining, "completed": completed,
            "canStart": not active, "canSettle": active and completed, "startMs": 1000, "endMs": 28801000}


def yuan(text, account=YIN):
    return reply(account, "**你的本命元婴**\n**状态**: "+text+"\n使用 `.元婴出窍` 派遣元婴。")


class FakeClient:
    def __init__(self, **responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls = []
        self.before_mutation = None

    async def open(self):
        self.calls.append(("open", ""))
        return self

    async def step(self, method, command=""):
        self.calls.append((method, command))
        if method == "mutate" and self.before_mutation:
            self.before_mutation()
        result = self.responses[method].pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def overview(self):
        return await self.step("overview")

    async def details(self):
        return await self.step("details")

    async def read_command(self, command):
        return await self.step("read_command", command)

    async def mutate(self, flow, *, operation=""):
        return await self.step("mutate", f"{flow}:{operation}")


@asynccontextmanager
async def no_telegram(_root, account):
    async def forbidden(*args, **kwargs):
        raise AssertionError("A migrated command reached the Telegram sender")
    yield SimpleNamespace(account=account, client=SimpleNamespace(send_message=forbidden))


def run(root, flow, fake, account=YIN, now=NOW, **kwargs):
    return asyncio.run(run_flow(root, account, flow, bridge_factory=no_telegram,
        client_factory=lambda *_: fake, clock=lambda: now, **kwargs))


def test_current_scope_and_removed_flow(root):
    assert sum(len(v) for v in ACCOUNTS.values()) == 21
    assert migrated(root, FIX, "shendubiguan")
    assert not migrated(root, FIX, "yuanyingchuqiao")
    assert not migrated(root, YIN, "fumofabao_qingzhufengyunjian_shenlei")
    assert not migrated(root, YIN, "doufa_demo_player_02")


def test_disabled_route_never_authenticates(root):
    config = read_json(root/"config/active_flows.json")
    for row in config["flows"]:
        if row["account"] == YIN and row["flow"] == "shendubiguan": row["enabled"] = False
    write_json(root/"config/active_flows.json", config)
    fake = FakeClient()
    with pytest.raises(MiniAppError, match="not_enabled"):
        run(root, "shendubiguan", fake)
    assert fake.calls == []


def test_active_retreat_uses_server_timer_without_sending(root):
    fake = FakeClient(overview=[reply(deep=board(remaining=723))])
    result = run(root, "shendubiguan", fake)
    assert result["status"] == "active" and result["next_due_ts"] == NOW+726
    assert [c[0] for c in fake.calls] == ["open", "overview"]
    record = read_json(state_dir(root, YIN, "shendubiguan")/RECEIPT_FILE)
    assert valid_receipt(record, YIN, "shendubiguan") and not record["succeeded"]
    assert not {"acked", "sent_message_id", "reply_message_id"} & record.keys()


def test_completed_retreat_settles_then_restarts_and_journals_first(root):
    fake = FakeClient(overview=[reply(deep=board(remaining=0, completed=True))], mutate=[
        reply(text="【深度闭关总结】修为增加了 100 点", deep=board(active=False, remaining=0)),
        reply(text="你已进入深度闭关状态", deep=board(remaining=28800)),
    ])
    def journal_exists():
        state = read_json(state_dir(root, YIN, "shendubiguan")/STATE_FILE)
        assert state["status"] == "unconfirmed" and state["pending"]["request_id"]
    fake.before_mutation = journal_exists
    result = run(root, "shendubiguan", fake)
    assert result["status"] == "started"
    assert [c[1] for c in fake.calls if c[0] == "mutate"] == ["shendubiguan:settle", "shendubiguan:start"]
    state = read_json(state_dir(root, YIN, "shendubiguan")/STATE_FILE)
    assert state["pending"] is None and state["last_success_at"] == NOW


def test_lost_retreat_start_is_reconciled_without_repeating(root):
    fake = FakeClient(overview=[reply(deep=board(active=False, remaining=0)), reply(deep=board(remaining=28000))],
                      mutate=[MiniAppError("request_failed", uncertain=True)])
    assert run(root, "shendubiguan", fake)["status"] == "unknown_outcome"
    result = run(root, "shendubiguan", fake, now=NOW+121)
    assert result["status"] == "active"
    assert len([c for c in fake.calls if c[0] == "mutate"]) == 1
    assert read_json(state_dir(root, YIN, "shendubiguan")/STATE_FILE)["pending"] is None


@pytest.mark.parametrize("status,expected", [
    ("元神出窍\n归来倒计时: 1小时43分钟10秒", "active"),
    ("元婴闭关\n已积累修为: 13149 点", "manual_retreat"),
    ("空闲", "idle"), ("窍中温养", "idle"), ("元神出窍\n归来倒计时: 0秒", "return_due"),
])
def test_yuan_status_ignores_help_and_preserves_manual_retreat(status, expected):
    assert yuan_board(yuan(status))["status"] == expected


def test_yuan_active_or_manual_retreat_does_not_dispatch(root):
    fake = FakeClient(read_command=[yuan("元神出窍\n归来倒计时: 1小时")])
    result = run(root, "yuanyingchuqiao", fake)
    assert result["next_due_ts"] == NOW+3603
    assert all(c[0] != "mutate" for c in fake.calls)


def test_idle_yuan_dispatches_once_and_reads_actual_timer(root):
    fake = FakeClient(read_command=[yuan("空闲"), yuan("元神出窍\n归来倒计时: 7小时59分钟")],
                      mutate=[reply(text="你心念一动，元婴化作一道流光飞出")])
    result = run(root, "yuanyingchuqiao", fake)
    assert result["status"] == "started" and result["next_due_ts"] == NOW+28743
    assert len([c for c in fake.calls if c[0] == "mutate"]) == 1


def test_yuan_return_requires_elapsed_server_timer_and_idle_confirmation(root):
    fake = FakeClient(read_command=[yuan("元神出窍\n归来倒计时: 0秒"), yuan("空闲"),
                                   yuan("元神出窍\n归来倒计时: 8小时")],
                      mutate=[reply(text="元婴归来，已归窍"), reply(text="你心念一动，元婴化作一道流光飞出")])
    assert run(root, "yuanyingchuqiao", fake)["status"] == "started"
    assert [c[1] for c in fake.calls if c[0] == "mutate"] == ["yuanyingchuqiao:return", "yuanyingchuqiao:execute"]


@pytest.mark.parametrize("flow,account,text,status", [
    ("wendao", FIX, "【问道得宝】你获得了灵石x117", "success"),
    ("xuexishanlin", YIN, "【血洗功成】成功捕获 2 缕妖兽精魄", "success"),
    ("zhaohuanmoying", YIN, "召唤成功，镇压成功！魔影被你成功击溃", "success"),
    ("zhaohuanmoying", YIN, "召唤成功，镇压失败！修为暴跌", "settled_failure"),
    ("sect_checkin", YIN, "今日已点卯，道友明日再来吧。", "already_checked_in"),
    ("sect_checkin", YIN, "点卯成功！你获得了 50 点宗门贡献。", "checked_in"),
])
def test_command_business_results(root, flow, account, text, status):
    fake = FakeClient(mutate=[reply(account, text)])
    result = run(root, flow, fake, account)
    assert result["status"] == status
    record = read_json(state_dir(root, account, flow)/RECEIPT_FILE)
    assert record["succeeded"] is (status in {"success", "checked_in"})


def test_http_success_and_completed_do_not_prove_business_success(root):
    fake = FakeClient(mutate=[reply(FIX, "指令已处理")])
    result = run(root, "wendao", fake, FIX)
    assert result["status"] == "unknown_outcome"
    assert "last_success_at" not in result


def test_server_cooldown_takes_precedence_over_nominal_period(root):
    fake = FakeClient(mutate=[reply(FIX, "天机不可频繁窥探，请在 3分钟20秒 后再来问道。", ok=False)])
    result = run(root, "wendao", fake, FIX)
    assert result["status"] == "cooldown" and result["next_due_ts"] == NOW+203


def test_unknown_non_idempotent_action_waits_full_period(root):
    fake = FakeClient(mutate=[MiniAppError("request_failed", uncertain=True)])
    result = run(root, "xuexishanlin", fake)
    assert result["status"] == "unknown_outcome" and result["next_due_ts"] == NOW+14403
    calls = list(fake.calls)
    assert run(root, "xuexishanlin", fake, now=NOW+121)["status"] == "unknown_outcome"
    assert fake.calls == calls


def test_daily_checkin_recovers_lost_reply_with_same_idempotent_action(root):
    fake = FakeClient(mutate=[MiniAppError("request_failed", uncertain=True), reply(text="今日已点卯")])
    assert run(root, "sect_checkin", fake)["status"] == "unknown_outcome"
    assert run(root, "sect_checkin", fake, now=NOW+121)["status"] == "already_checked_in"
    calls = list(fake.calls)
    assert run(root, "sect_checkin", fake, now=NOW+300)["status"] == "already_checked_in"
    assert fake.calls == calls


def test_event_sermon_success_is_deduped_after_crash(root):
    event = {"key": "chat:1", "deadline": NOW+900}
    world = {"hasWorld": True, "actions": {"edictRemainingSeconds": 0}}
    fake = FakeClient(details=[reply(DEMO_01, world=world)],
                      mutate=[reply(DEMO_01, "【神音浩荡】信仰提升至 99，稳定提升至 100。")])
    assert run(root, "shenji_budao", fake, DEMO_01, event=event)["status"] == "success"
    calls = list(fake.calls)
    assert run(root, "shenji_budao", fake, DEMO_01, event=event, now=NOW+10)["status"] == "event_already_complete"
    assert fake.calls == calls


def test_uncertain_sermon_is_never_replayed_for_same_event(root):
    event = {"key": "chat:2", "deadline": NOW+20000}
    world = {"hasWorld": True, "actions": {"edictRemainingSeconds": 0}}
    fake = FakeClient(details=[reply(DEMO_01, world=world), reply(DEMO_01, world=world)],
                      mutate=[MiniAppError("request_failed", uncertain=True)])
    assert run(root, "shenji_budao", fake, DEMO_01, event=event)["status"] == "unknown_outcome"
    assert run(root, "shenji_budao", fake, DEMO_01, event=event, now=NOW+4000)["status"] == "unknown_outcome"
    assert len([c for c in fake.calls if c[0] == "mutate"]) == 1


def test_sermon_requires_live_event(root):
    fake = FakeClient()
    assert run(root, "shenji_budao", fake, DEMO_01)["error"] == "miniapp_event_required"
    assert all(c[0] != "mutate" for c in fake.calls)


def test_malformed_state_fails_closed_and_is_preserved(root):
    path = state_dir(root, YIN, "shendubiguan")/STATE_FILE
    path.parent.mkdir(parents=True);path.write_text("{partial")
    fake = FakeClient()
    with pytest.raises(MiniAppError, match="state_identity"):
        run(root, "shendubiguan", fake)
    assert fake.calls == [] and path.read_text() == "{partial"


@pytest.mark.parametrize("bad_due",[True,"invalid","nan",-5])
def test_invalid_deadline_cannot_bypass_cadence(root,bad_due):
    path=state_dir(root,YIN,"xuexishanlin")/STATE_FILE
    write_json(path,{"account":YIN,"flow":"xuexishanlin","runtime_version":VERSION,
                     "status":"success","next_due_ts":bad_due})
    fake=FakeClient()
    with pytest.raises(MiniAppError,match="state_invalid"):
        run(root,"xuexishanlin",fake)
    assert fake.calls==[]


def test_pause_and_cross_flow_api_lock_prevent_network(root):
    directory = state_dir(root, YIN, "xuexishanlin");directory.mkdir(parents=True)
    (directory/"pause_all").touch()
    fake = FakeClient()
    assert run(root, "xuexishanlin", fake)["status"] == "paused"
    (directory/"pause_all").unlink()
    with exclusive_lock(root/"state/miniapp_commands/api.lock"):
        assert run(root, "xuexishanlin", fake)["status"] == "transport_busy"
    assert fake.calls == []


def test_qyz_priority_skips_without_authentication(root):
    fake = FakeClient()
    assert run(root, "xuexishanlin", fake, now=NOW+4*3600+40*60)["status"] == "yielding_to_qyz"
    assert fake.calls == []


def test_read_only_does_not_create_business_journal_or_mutate(root):
    fake = FakeClient(overview=[reply(deep=board(active=False, remaining=0))])
    assert run(root, "shendubiguan", fake, read_only=True)["status"] == "read_only"
    assert not (state_dir(root, YIN, "shendubiguan")/STATE_FILE).exists()
    assert not (state_dir(root, YIN, "shendubiguan")/RECEIPT_FILE).exists()


def test_receipt_redaction_and_health_do_not_hide_uncertainty(root):
    fake = FakeClient(mutate=[reply(text="【血洗功成】 initData=SIGNED_SECRET https://example.invalid?token=AUTH_SECRET")])
    run(root, "xuexishanlin", fake)
    directory = state_dir(root, YIN, "xuexishanlin")
    data = (directory/STATE_FILE).read_text()+(directory/RECEIPT_FILE).read_text()
    assert "SIGNED_SECRET" not in data and "AUTH_SECRET" not in data
    assert health(directory, YIN, "xuexishanlin", NOW)[0] == "ok"
    state = read_json(directory/STATE_FILE);state["pending"] = {"at": NOW};state["status"] = "unknown_outcome"
    write_json(directory/STATE_FILE, state)
    assert health(directory, YIN, "xuexishanlin", NOW)[0] == "warn"


def test_wrong_player_receipt_is_rejected(root):
    fake = FakeClient(overview=[reply(deep=board())]);run(root, "shendubiguan", fake)
    row = read_json(state_dir(root, YIN, "shendubiguan")/RECEIPT_FILE)
    row["player_id"] = int(DEMO_01[2:])
    assert not valid_receipt(row, YIN, "shendubiguan")


def test_explicit_rate_limit_has_no_unknown_mutation_and_backs_off(root):
    fake = FakeClient(mutate=[MiniAppError("external_action_rate_limited")])
    result = run(root, "xuexishanlin", fake)
    assert result["status"] == "request_failed" and result["next_due_ts"] == NOW+300
    assert read_json(state_dir(root, YIN, "xuexishanlin")/STATE_FILE)["pending"] is None


def test_client_pins_personal_identity_and_never_accepts_unreviewed_command(root, monkeypatch):
    captured=[]
    async def verified(*_): return "demo_player_02"
    async def opened(*_):
        return DwellingSession(YIN, "SIGNED_TEST_INIT", "TEST_TOKEN", {},
                               (SimpleNamespace(player_id=int(YIN[2:])),))
    monkeypatch.setattr(client_module, "verify_username", verified)
    monkeypatch.setattr(client_module, "open_dwelling", opened)
    def post(path, payload, **kwargs):
        captured.append((path,payload,kwargs))
        return reply(text="【血洗功成】")
    api = CommandClient(root, YIN, object(), writer=JsonTransport({"/api/miniapp/xianxia-dwelling/command-center": True},post=post))
    async def exercise():
        await api.open()
        await api.mutate("xuexishanlin")
        with pytest.raises(MiniAppError): await api.read_command(".抚摸法宝 青竹蜂云剑")
        with pytest.raises(MiniAppError): await api.mutate("doufa_demo_player_02")
    asyncio.run(exercise())
    assert len(captured) == 1 and captured[0][1]["playerId"] == int(YIN[2:])
    assert captured[0][1]["command"] == ".血洗山林"


def test_client_rejects_wrong_actor_after_mutation_as_uncertain(root):
    api = CommandClient(root,YIN,object(),writer=lambda *a,**kw: None)
    api.session = DwellingSession(YIN,"TEST_INIT","TEST_TOKEN",{},())
    async def wrong(*args,**kw): return reply(DEMO_01,"【血洗功成】")
    api.writer = wrong
    with pytest.raises(MiniAppError) as exc:
        asyncio.run(api.mutate("xuexishanlin"))
    assert exc.value.code == "miniapp_response_identity_mismatch" and exc.value.uncertain


def test_business_rejection_in_an_error_body_is_certain_not_frozen(root, monkeypatch):
    # JsonTransport hands back an ``ok: false`` body that carries ``actionResult``
    # as a definite answer; it must not freeze the flow for a whole period.
    async def verified(*_): return "demo_player_02"
    async def opened(*_):
        return DwellingSession(YIN, "TEST_INIT", "TEST_TOKEN", {}, (SimpleNamespace(player_id=int(YIN[2:])),))
    monkeypatch.setattr(client_module, "verify_username", verified)
    monkeypatch.setattr(client_module, "open_dwelling", opened)
    def post(path, payload, **kwargs):
        return {"ok": False, "error": "command_failed", "account": {"playerId": int(YIN[2:])},
                "actionResult": {"ok": False, "rawMessage": "妖兽已被惊走，此地暂无可猎之物。"}}
    def factory(given_root, account, bridge):
        return CommandClient(given_root, account, bridge,
                             writer=JsonTransport({"/api/miniapp/xianxia-dwelling/command-center": True}, post=post))
    result = asyncio.run(run_flow(root, YIN, "xuexishanlin", bridge_factory=no_telegram, client_factory=factory,
                                  clock=lambda: NOW))
    assert result["status"] == "rejected" and result["next_due_ts"] == NOW+900
    assert read_json(state_dir(root, YIN, "xuexishanlin")/STATE_FILE)["pending"] is None


def test_repeated_request_failures_back_off_and_a_completed_cycle_resets(root):
    fail = MiniAppError("request_failed")
    fake = FakeClient(overview=[fail, fail, fail, fail, fail, reply(deep=board(remaining=60)), fail])
    now, gaps = NOW, []
    for _ in range(5):
        result = run(root, "shendubiguan", fake, now=now)
        assert result["status"] == "request_failed"
        gaps.append(result["next_due_ts"]-now)
        now = result["next_due_ts"]
    assert gaps == [120, 300, 900, 1800, 1800]
    assert run(root, "shendubiguan", fake, now=now)["status"] == "active"
    now += 63
    assert run(root, "shendubiguan", fake, now=now)["next_due_ts"] == now+120
