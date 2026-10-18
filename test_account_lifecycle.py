from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlencode

import pytest

import account_lifecycle as lifecycle
from account_lifecycle import (AccountPausedError, CHAT_ID, TOPIC_ID, SUCCESS,
    assert_account_allowed, classify_reply, flush_notifications,
    load_state, observe_reply, queue_ready_reminders, record_evidence)

ACCOUNT = "tg9100000002"
ORIGINAL = "【元婴遁逃·虚弱】\n千钧一发之际，你的元婴带着你的三魂七魄，从破碎的肉身中遁出！\n但你的神魂遭受重创，已陷入 6小时 的【虚弱期】！\n\n在此期间，你的修为将会持续逸散，且无法进行夺舍。请静待神魂稳固！"
STARRED = "*肉身碎了……灵气在溃散。裂缝？好恨……谁在那？借躯壳夺舍！*"


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setenv("XIU_ACCOUNT_LIFECYCLE_PATH", str(path))
    return path


def pair(text=STARRED, command=".探寻裂缝", at=1000, message_id=20, account=9100000002):
    parent = NS(id=message_id - 1, sender_id=account, chat_id=CHAT_ID, raw_text=command,
                date=datetime.fromtimestamp(at-3, timezone.utc),
                reply_to=NS(reply_to_msg_id=TOPIC_ID, reply_to_top_id=None))
    reply = NS(id=message_id, sender_id=8980154525, chat_id=CHAT_ID, raw_text=text,
               reply_to_msg_id=parent.id, date=datetime.fromtimestamp(at, timezone.utc))
    return reply, parent


def break_account(text=ORIGINAL, **kwargs):
    evidence = classify_reply(*pair(text=text, **kwargs))
    assert evidence
    record_evidence(evidence)
    return evidence


ANNOUNCEMENT = ("【天机异闻·夺舍重生】\n"
    "先前肉身陨落的 @demo_player_02 (原道号：示例修士二)，其元婴已成功夺舍重生！\n"
    "从今日起，他将以【示例重生角色】为名，身负【伪灵根(金水土木)】的全新肉身，"
    "继续其漫漫仙途！过往因果，一并继承！")


def announcement_message(text=ANNOUNCEMENT, at=30000):
    return NS(id=1258466, sender_id=8788895566, chat_id=lifecycle.ANNOUNCEMENT_CHAT_ID,
              raw_text=text, date=datetime.fromtimestamp(at, timezone.utc), reply_to_msg_id=None)


def test_verified_public_announcement_recovers_without_direct_reply(state):
    break_account()
    message = announcement_message()
    evidence = lifecycle.classify_recovery_announcement(message)
    assert evidence["account"] == ACCOUNT
    assert evidence["recovery_format"] == "public_rebirth_announcement"
    assert asyncio.run(observe_reply(object(), message))
    assert_account_allowed(ACCOUNT)
    break_account(at=1000)
    assert_account_allowed(ACCOUNT)


@pytest.mark.parametrize("change", ["untrusted", "wrong_chat", "lookalike", "failure", "quoted", "extra_mention"])
def test_public_announcement_requires_exact_trusted_subject(state, change):
    break_account()
    message = announcement_message()
    if change == "untrusted": message.sender_id = 123
    elif change == "wrong_chat": message.chat_id = 123
    elif change == "lookalike": message.raw_text = ANNOUNCEMENT.replace("@demo_player_02", "@demo_player_02_fake")
    elif change == "failure": message.raw_text = ANNOUNCEMENT.replace("已成功夺舍重生", "未成功夺舍重生")
    elif change == "quoted": message.raw_text = "举例：" + ANNOUNCEMENT
    elif change == "extra_mention": message.raw_text = ANNOUNCEMENT.replace("@demo_player_02", "@stranger_001") + " @demo_player_02"
    assert lifecycle.classify_recovery_announcement(message) is None
    assert not asyncio.run(observe_reply(object(), message))
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)


def test_announcement_for_other_account_never_unpauses_this_one(state):
    break_account()
    asyncio.run(observe_reply(object(), announcement_message(ANNOUNCEMENT.replace("@demo_player_02", "@demo_player_03"))))
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)
    assert load_state()["accounts"]["tg9100000003"]["status"] == "active"


def test_verified_starred_reply_pauses_only_parent_account(state):
    break_account(STARRED)
    with pytest.raises(AccountPausedError):
        assert_account_allowed(ACCOUNT)
    assert_account_allowed("tg9100000001")
    row = load_state()["accounts"][ACCOUNT]
    assert row["ready_at"] is None


@pytest.mark.parametrize("change", ["stranger", "wrong_bot", "wrong_chat", "wrong_topic", "wrong_parent", "not_command"])
def test_requires_trusted_bot_exact_command_parent_and_topic(state, change):
    m, p = pair()
    if change == "stranger": p.sender_id = 123
    elif change == "wrong_bot": m.sender_id = 9100000001
    elif change == "wrong_chat": m.chat_id = -1009400000005
    elif change == "wrong_topic": p.reply_to.reply_to_msg_id = 1
    elif change == "wrong_parent": m.reply_to_msg_id = 2
    elif change == "not_command": p.raw_text = "@demo_player_02 肉身碎了"
    assert classify_reply(m, p) is None


@pytest.mark.parametrize("text", ["普通奖励 *材料*", "**夺舍成功**", "夺舍失败！", "尚未夺舍成功", "夺舍成功！", "请再温养 6小时。"])
def test_generic_markdown_or_failure_cannot_resume(state, text):
    break_account()
    assert classify_reply(*pair(text=text, command=".重生 1", at=30000)) is None
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)


def test_timer_does_not_resume_or_repeat_star_extend(state):
    break_account(at=1000)
    break_account(STARRED, at=22000, message_id=22)
    queue_ready_reminders(now=99999999)
    row = load_state()["accounts"][ACCOUNT]
    assert row["ready_at"] == 22600
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)
    assert len(load_state()["outbox"]) == 2
    queue_ready_reminders(now=999999999)
    assert len(load_state()["outbox"]) == 2


def test_original_can_be_recovered_after_starred_without_six_hour_reset(state):
    break_account(STARRED, at=10000)
    break_account(ORIGINAL, at=1000, message_id=10)
    assert load_state()["accounts"][ACCOUNT]["ready_at"] == 22600


def test_server_remaining_cooldown_is_authoritative(state):
    e = classify_reply(*pair(text="你的元婴尚在虚弱之中，无法承受夺舍带来的神魂冲击。\n请再温养 8分钟30秒。", command=".夺舍重生", at=1000))
    assert e["kind"] == "weak" and e["ready_at"] == 1510
    record_evidence(e)
    assert load_state()["accounts"][ACCOUNT]["ready_at"] == 1510


def test_only_full_success_matching_own_possession_command_resumes(state):
    break_account()
    assert classify_reply(*pair(text=SUCCESS, command=".探寻裂缝", at=30000)) is None
    e = classify_reply(*pair(text=SUCCESS, command=".重生 1", at=30000, message_id=30))
    record_evidence(e)
    assert_account_allowed(ACCOUNT)
    break_account(ORIGINAL, at=1000, message_id=10)
    assert_account_allowed(ACCOUNT)
    # A delayed bot response to an old command cannot reopen the old incident.
    m, p = pair(at=31000, message_id=32)
    p.date = datetime.fromtimestamp(1000, timezone.utc)
    record_evidence(classify_reply(m, p))
    assert_account_allowed(ACCOUNT)
    break_account(at=32000, message_id=34)
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)


def test_success_seen_before_old_break_blocks_replay(state):
    record_evidence(classify_reply(*pair(text=SUCCESS, command=".夺舍重生", at=30000)))
    break_account(at=1000)
    assert_account_allowed(ACCOUNT)


def test_notifications_injected_deduplicated_and_failed_delivery_retried(state):
    break_account()
    class Notify:
        def __init__(self): self.calls = []
        def send_once(self, key, text):
            self.calls.append((key, text))
            return {"status": "delivery_failed" if len(self.calls) == 1 else "delivered"}
    fake = Notify()
    flush_notifications(fake)
    flush_notifications(fake)
    flush_notifications(fake)
    assert len(fake.calls) == 2
    assert "尚未确认夺舍成功" in fake.calls[0][1]


def test_obsolete_history_pause_is_not_notified_after_success(state):
    break_account()
    record_evidence(classify_reply(*pair(text=SUCCESS, command=".重生 1", at=30000, message_id=30)))
    class Notify:
        def __init__(self): self.keys = []
        def send_once(self, key, text):
            self.keys.append(key)
            return {"status": "delivered"}
    fake = Notify()
    flush_notifications(fake)
    assert fake.keys == [f"{ACCOUNT}:resumed:30"]


def test_malformed_state_fails_closed(state):
    state.write_text('{"version":1,"accounts":{"tg1":{}},"outbox":{}}')
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)


def init_data(identity):
    return urlencode({"user": json.dumps({"id": identity}), "hash": "fixture"})


def test_async_observer_fetches_parent_not_reader_identity(state):
    m, p = pair()
    class Client:
        async def get_messages(self, chat, ids):
            assert chat == CHAT_ID and ids == p.id
            return p
    assert asyncio.run(observe_reply(Client(), m))
    with pytest.raises(AccountPausedError): assert_account_allowed(ACCOUNT)


@pytest.mark.parametrize("request_name", ["SendMessageRequest", "EditMessageRequest", "SendMediaRequest", "SendMultiMediaRequest"])
def test_rpc_guard_refuses_before_network_and_uses_get_me(state, monkeypatch, request_name):
    from account_lifecycle_telegram import LifecycleTelegramClient
    from telethon import TelegramClient
    break_account()
    calls = []
    async def network(*args, **kwargs):
        calls.append(True)
        return "sent"
    monkeypatch.setattr(TelegramClient, "_call", network)
    client = LifecycleTelegramClient(None, 12345, "fixture")
    async def me(): return NS(id=9100000002)
    monkeypatch.setattr(client, "get_me", me)
    request = type(request_name, (), {})()
    request.message = ".探寻裂缝"
    request.multi_media = [NS(message=".探寻裂缝")]
    with pytest.raises(AccountPausedError):
        asyncio.run(client._call(None, request))
    assert calls == []
    # Reads remain possible, otherwise no recovery observation could run.
    assert asyncio.run(client._call(None, NS())) == "sent"


@pytest.mark.parametrize("request_name,text", [
    ("SendMessageRequest", "普通聊天"), ("SendMessageRequest", "今天 v1.2 更新"),
    ("SendMessageRequest", "/start"), ("EditMessageRequest", "普通编辑"),
    ("GetBotCallbackAnswerRequest", ".not-a-message"),
    ("ForwardMessagesRequest", ""), ("SendWebViewDataRequest", ""),
    ("SendInlineBotResultRequest", ""), ("SendVoteRequest", ""),
])
def test_paused_account_allows_non_dot_telegram_requests(state, monkeypatch, request_name, text):
    from account_lifecycle_telegram import LifecycleTelegramClient
    from telethon import TelegramClient
    break_account()
    async def network(*args, **kwargs): return "allowed"
    monkeypatch.setattr(TelegramClient, "_call", network)
    client = LifecycleTelegramClient(None, 12345, "fixture")
    request = type(request_name, (), {})()
    request.message = text
    assert asyncio.run(client._call(None, request)) == "allowed"


def test_dot_detection_whitespace_and_batch():
    from account_lifecycle_telegram import _is_dot_command
    request = type("SendMessageRequest", (), {})()
    for text in (".探寻裂缝", "  .闭关", "。闭关"):
        request.message = text
        assert _is_dot_command(request)
    batch = type("SendMultiMediaRequest", (), {})()
    batch.multi_media = [NS(message="normal"), NS(message=".闭关")]
    assert _is_dot_command(batch)


def test_telethon_internal_retry_rechecks_pause_after_server_sleep(state, monkeypatch):
    from account_lifecycle_telegram import LifecycleTelegramClient
    from telethon import errors, types
    from telethon.tl.functions.messages import SendMessageRequest
    client = LifecycleTelegramClient(None, 12345, "fixture", request_retries=1)
    client._lifecycle_identity = 9100000002
    request = SendMessageRequest(types.InputPeerSelf(), ".探寻裂缝", random_id=1)
    async def resolve(*args): pass
    request.resolve = resolve
    calls = []
    class Sender:
        def send(self, *args, **kwargs):
            calls.append(True)
            future = asyncio.get_running_loop().create_future()
            future.set_exception(errors.ServerError(request, "fixture", code=500))
            return future
    async def sleep(_seconds):
        break_account()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(AccountPausedError):
        asyncio.run(client._call(Sender(), request))
    assert len(calls) == 1


def test_factories_are_guarded(state, monkeypatch):
    # Telethon 1.44 treats this string as a SQLite filename. Keep the dummy
    # session in the test directory so the source mount can remain read-only.
    monkeypatch.chdir(state.parent)
    from account_lifecycle_telegram import LifecycleTelegramClient
    import transport
    from fishing import fishing_accounts
    assert isinstance(transport.telegram_client(":memory:", 12345, "fixture"), LifecycleTelegramClient)
    assert isinstance(fishing_accounts.telegram_client(":memory:", 12345, "fixture"), LifecycleTelegramClient)
    # The QYZ host imports shared guard only after adding the project path.
    text = (Path(__file__).parent / "qingyuanzi-new-source/qingyuanzi_host.py").read_text()
    assert "from account_lifecycle_telegram import LifecycleTelegramClient as TelegramClient" in text


def test_dwelling_tianji_and_fishing_remain_allowed_while_dot_commands_paused(state):
    from miniapp_shared.dwelling import JsonTransport
    from tianji_trial.adapter import LiveAccountBridge
    from fishing.miniapp_fishing_integration import UpstreamFishingProtocol
    break_account()
    calls = []
    def response(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "data": {"url": "/miniapp/xianxia-fishing?startapp=fish_fixture"}}
    transport = JsonTransport({"/action": True}, post=response)
    assert asyncio.run(transport("/action", {"initData": init_data(9100000002)}, mutation=True))["ok"]
    bridge = LiveAccountBridge(Path("."), ACCOUNT, http_transport=response)
    assert asyncio.run(bridge.post_json("/api/miniapp/xianxia-trial/finish", {}))["ok"]
    fishing = UpstreamFishingProtocol(http_transport=response)
    fishing._request("start", token="fixture", init_data=init_data(9100000002))
    assert asyncio.run(fishing._fishing_token_from_dwelling(token="fixture", init_data=init_data(9100000002))) == "fish_fixture"
    assert len(calls) == 4


def test_qyz_http_remains_allowed_while_dot_commands_paused(state):
    import sys
    import httpx
    sys.path.insert(0, str(Path(__file__).parent / "qingyuanzi-new-source"))
    from qyz_http import HttpTransport
    break_account()
    async def run():
        transport = HttpTransport(ACCOUNT)
        await transport._client.aclose()
        transport._client = httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})))
        try:
            status, body = await transport._request("POST", "/any", phase="hit", payload={})
            assert status == 200 and json.loads(body) == {"ok": True}
        finally:
            await transport.close()
    asyncio.run(run())


def write_paused(path, *accounts):
    path.write_text(json.dumps({"version": 1, "outbox": {},
                                "accounts": {account: {"status": "paused"} for account in accounts}}))


def test_paused_account_status_probe_refuses_before_connecting(state, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch
    import probe_yuanyingchuqiao_status as probe
    write_paused(state, "tg9100000002")
    for name, value in {"XIU_ACCOUNT_SLUG": "tg9100000002", "TG_STATE_DIR": str(tmp_path / "probe"),
                        "TG_API_ID": "1", "TG_API_HASH": "unused-test-value", "TG_SESSION_NAME": "unused",
                        "TG_TARGET": "@example_game_group", "TG_TOPIC_ID": "7310786"}.items():
        monkeypatch.setenv(name, value)
    with patch("miniapp_commands.runner.route_from_environment", AsyncMock(return_value=None)), \
            patch.object(probe, "load_dotenv"), \
            patch.object(probe, "telegram_client", side_effect=AssertionError("must not build a Telegram client")):
        assert asyncio.run(probe.main_async()) == 12  # wrapper: blocked for this account, skip send
    assert not (tmp_path / "probe" / "status_probe.json").exists()


def test_paused_account_senders_refuse_locally_before_any_connect(state, tmp_path, monkeypatch):
    """A paused account is a definite local refusal, never an unknown send to retry."""
    from unittest.mock import AsyncMock, patch
    import observe_return
    import send_shendubiguan
    import shuangxiu_wenyang
    import yuanshen_automation
    write_paused(state, "tg9100000001", "tg9100000003")
    no_client = AssertionError("must not build a Telegram client")

    monkeypatch.setenv("TG_STATE_DIR", str(tmp_path / "shuangxiu"))
    with patch.object(shuangxiu_wenyang, "load_dotenv"), \
            patch.object(shuangxiu_wenyang, "telegram_client", side_effect=no_client):
        asyncio.run(shuangxiu_wenyang.run_once())
    assert not (tmp_path / "shuangxiu" / "last_attempt.json").exists()

    for name, value in {"XIU_ACCOUNT_SLUG": "tg9100000001", "TG_STATE_DIR": str(tmp_path / "biguan"),
                        "TG_OBSERVER_IMMEDIATE_KICK": "1", "TG_API_ID": "1", "TG_API_HASH": "unused-test-value",
                        "TG_SESSION_NAME": "unused", "TG_TARGET": "@example_game_group",
                        "TG_TOPIC_ID": "7310786"}.items():
        monkeypatch.setenv(name, value)
    with patch("miniapp_commands.runner.route_from_environment", AsyncMock(return_value=None)), \
            patch.object(send_shendubiguan, "telegram_client", side_effect=no_client), \
            pytest.raises(SystemExit) as kicked:
        asyncio.run(send_shendubiguan.run_once())
    assert kicked.value.code == 75  # the observer keeps its event pending

    (tmp_path / "state").mkdir()
    (tmp_path / "state/account_lifecycle.json").write_text(state.read_text())
    bridge = AssertionError("must not open a bridge")
    result = asyncio.run(yuanshen_automation.run_cycle(tmp_path, "train", bridge_factory=bridge))
    assert result["status"] == "account_paused"

    observer_dir = tmp_path / "observer"
    event = NS(id=600, message="深度闭关总结 @demo_player_01", sender_id=8918867302,
               date=datetime.now(timezone.utc), reply_to=NS(reply_to_msg_id=7310786, reply_to_top_id=7310786))
    for name, value in {"TG_FLOW_SLUG": "shendubiguan", "TG_STATE_DIR": str(observer_dir),
                        "TG_SEQUENCE_SEND_WRAPPER": "/unused-test-wrapper",
                        "TG_TRIGGER_TARGET_USERNAME": "demo_player_01"}.items():
        monkeypatch.setenv(name, value)
    client = AsyncMock()
    client.get_entity.return_value = NS(id=9400000004)
    with patch.object(observe_return, "load_dotenv"), \
            patch.object(observe_return, "ensure_connected", new_callable=AsyncMock), \
            patch.object(observe_return, "collect_recent_messages", AsyncMock(return_value=[event])), \
            patch.object(observe_return.subprocess, "Popen", side_effect=AssertionError("spawned sender")):
        assert asyncio.run(observe_return.main_async(client_override=client)) == 0
    seen = json.loads((observer_dir / "observer_last_seen.json").read_text())
    assert seen["status"] == "account_paused" and not seen.get("last_seen_event_ids_by_chat")
