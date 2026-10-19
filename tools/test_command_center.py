"""群指令改走洞府指令中心（批 1 查询、批 2 侍妾写操作）：跑真实发送队列，接口用假的，网络全禁。

- 开着开关：`.我的侍妾` 不进群，回包包成合成消息走路由，指令按回包确认，两条消息都落 bound_messages；
- 接口拒绝：这条照常群发，该号 10 分钟内不再调接口；
- 开关关着、或不在查询名单里的指令：照常群发，不碰接口；
- 接口给的 Markdown 原文去掉标记后和群里 raw_text 一样（10-17 探针样本）；
- 写操作要再开 "writes"；结果不明（读超时、说不清的 5xx）不重发也不群发，留在「等回包」；
  服务器明确没执行（连不上、522、限流、白名单拒绝）才改群发；要回复某条消息的指令不走接口。

run: PYTHONPATH=app/src python tools/test_command_center.py
"""

import asyncio
import contextlib
import json
import sys
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.estate import biz_command_center as cc  # noqa: E402
from tg_game.runtime.executors import COMPANION_PANEL_COMMAND, _is_profile_command_reply  # noqa: E402
from tg_game.runtime.router import Router  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.telegram import runtime, send_utils  # noqa: E402

CHAT, TOPIC, BOT, USER = -100000000077, 42, 111, "555"
# 10-17 丙真人探针：接口原文 / 群里同一段（数字一致的前几行）
API_TEXT = (
    "**1. 你的道心侍妾: 【莎儿】** (状态: 随行中)\n\n**情缘值**: 1216\n\n**【掩月心契】**\n"
    "- 当前誓约: **守秘** (闭关奇遇率提高8%)\n命令: `.立誓 护道/守秘/共修`、`.毁誓`\n\n"
    "**远航状态**: 稳妥航线进行中，剩余约 186 分钟。"
)
GROUP_TEXT = (
    "1. 你的道心侍妾: 【莎儿】 (状态: 随行中)\n\n情缘值: 1216\n\n【掩月心契】\n"
    "- 当前誓约: 守秘 (闭关奇遇率提高8%)\n命令: .立誓 护道/守秘/共修、.毁誓\n\n"
    "远航状态: 稳妥航线进行中，剩余约 186 分钟。"
)
OK_BODY = {"ok": True, "actionResult": {"ok": True, "command": ".我的侍妾", "rawMessage": API_TEXT}}
# 远航归来：群里真实样本（tools/test_voyage_settle_parse.py），FanRenXiuXian 按「远航·归」认
RETURN_TEXT = "**【乱星海远航·归】**\n侍妾【莎儿】已自 稳妥 航线归来，向你呈上收获：\n- 修为 +200\n- 灵石 +20"
RETURN_BODY = {"ok": True, "actionResult": {"ok": True, "command": ".远航归来", "rawMessage": RETURN_TEXT}}


@contextlib.contextmanager
def case(enabled=True, writes=False):
    with tempfile.TemporaryDirectory(prefix="command-center-") as tmp:
        storage = Storage(Path(tmp) / "test.db")
        storage.init_schema()
        profile = storage.create_profile("offline-test")
        storage.bind_profile_telegram_account(profile.id, telegram_user_id=USER, telegram_username="offline_tester")
        storage.create_chat_binding(profile.id, CHAT, thread_id=TOPIC, bot_username="fanrenxiuxian_bot",
                                    bot_id=BOT, telegram_user_id=USER)
        storage.set_runtime_state(cc.SWITCH_KEY.format(profile.id), json.dumps({"enabled": enabled, "writes": writes}))
        storage.profile_has_companion = lambda profile_id: True  # 侍妾指令入队要有侍妾，正常靠天机阁同步
        cc._backoff_until.clear()
        cc._init_data.clear()
        yield storage, profile.id


class Observer:
    key = "observer"

    def __init__(self, storage):
        self.storage, self.seen = storage, []

    async def handle(self, context, storage):
        self.seen.append({
            "text": context.text, "thread_id": context.thread_id, "reply_to": context.reply_to_msg_id,
            "is_bot": context.is_bot_sender, "targets_me": await context.bot_message_targets_profile(),
            "own_command_reply": _is_profile_command_reply(
                storage, message={"reply_to_msg_id": context.reply_to_msg_id}, profile_id=context.profile.id,
                chat_id=context.chat_id, thread_id=context.thread_id, command_texts={COMPANION_PANEL_COMMAND}),
        })
        return True


class FakeClient:
    def __init__(self, storage, profile_id):
        self._tg_game_storage, self._tg_game_profile_id = storage, profile_id
        self.observer = Observer(storage)
        self._tg_game_router = Router(storage, [self.observer], runtime_profile_id=profile_id)
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(id=2000 + len(self.sent), sender_id=int(USER))

    async def get_input_entity(self, chat_id):
        return chat_id

    async def __call__(self, request):  # 话题内回复走原始请求
        return await self.send_message(request.peer, request.message)

    def _get_response_message(self, request, result, peer):
        return result


async def stop_at_idle(seconds):
    raise asyncio.CancelledError


async def run_queue(client, storage, profile_id, text, post, reply_to_msg_id=None):
    command_id = storage.enqueue_outgoing_command(profile_id, CHAT, text, thread_id=TOPIC,
                                                  reply_to_msg_id=reply_to_msg_id, bot_username="fanrenxiuxian_bot")
    send_utils._last_send_at = 0
    calls = []

    def fake_post(request):
        calls.append(request["payload"]["command"])
        if isinstance(post, Exception):
            raise post
        return post

    async def fake_init_data(client, storage, profile_id):
        return "token", "init-data"

    with patch.object(cc, "_post", fake_post), patch.object(cc, "_fresh_init_data", fake_init_data), \
            patch.object(runtime.asyncio, "sleep", stop_at_idle), contextlib.suppress(asyncio.CancelledError):
        await runtime._dispatch_outgoing_commands(client, storage, profile_id)
    return storage.get_outgoing_command(command_id), calls


async def api_reply_goes_through_router():
    with case() as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        row, calls = await run_queue(client, storage, profile_id, ".我的侍妾", (200, OK_BODY))
        assert calls == [".我的侍妾"] and not client.sent, (calls, client.sent)
        synthetic = cc.SYNTHETIC_MESSAGE_ID_BASE + 2 * row["id"]
        assert row["status"] == "confirmed" and row["sent_message_id"] == synthetic, row
        seen = client.observer.seen[-1]
        assert seen == {"text": GROUP_TEXT, "thread_id": TOPIC, "reply_to": synthetic, "is_bot": True,
                        "targets_me": True, "own_command_reply": True}, seen
        command_msg = storage.get_bound_message(CHAT, synthetic, profile_id)
        reply_msg = storage.get_bound_message(CHAT, synthetic + 1, profile_id)
        assert command_msg["direction"] == "outgoing" and command_msg["text"] == ".我的侍妾", command_msg
        assert reply_msg["is_bot"] and reply_msg["reply_to_msg_id"] == synthetic and reply_msg["text"] == GROUP_TEXT
        # 侍妾补位、网页面板等都靠「本号最新一条 .我的侍妾 的 bot 回包」取面板
        latest = storage.get_latest_bot_reply_for_command(CHAT, ".我的侍妾", profile_id=profile_id,
                                                          thread_id=TOPIC, sender_id=int(USER))
        assert latest and latest["message_id"] == synthetic + 1, latest


async def api_failure_falls_back_to_group():
    with case() as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        refused = (400, {"ok": False, "error": "command_not_allowed"})
        row, calls = await run_queue(client, storage, profile_id, ".远航状态", refused)
        assert calls == [".远航状态"] and client.sent == [".远航状态"], (calls, client.sent)
        assert row["status"] == "awaiting_confirm" and row["sent_message_id"] == 2001, row
        # 退避期内不再调接口
        row, calls = await run_queue(client, storage, profile_id, ".我的侍妾", (200, OK_BODY))
        assert calls == [] and client.sent[-1] == ".我的侍妾", (calls, client.sent)


async def switch_and_whitelist_gate_the_api():
    with case(enabled=False) as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        _, calls = await run_queue(client, storage, profile_id, ".我的侍妾", (200, OK_BODY))
        assert calls == [] and client.sent == [".我的侍妾"], (calls, client.sent)
    with case() as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        _, calls = await run_queue(client, storage, profile_id, ".召回侍妾", (200, OK_BODY))
        assert calls == [] and client.sent == [".召回侍妾"], (calls, client.sent)


async def writes_need_their_own_switch():
    with case(writes=True) as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        row, calls = await run_queue(client, storage, profile_id, ".远航归来", (200, RETURN_BODY))
        assert calls == [".远航归来"] and not client.sent, (calls, client.sent)
        synthetic = cc.SYNTHETIC_MESSAGE_ID_BASE + 2 * row["id"]
        assert row["status"] == "confirmed" and row["sent_message_id"] == synthetic, row
        seen = client.observer.seen[-1]
        assert seen["text"].startswith("【乱星海远航·归】") and seen["reply_to"] == synthetic and seen["targets_me"], seen
        # 宗门传功要在群里回复本号最近一条指令：合成消息不能被挑中
        assert storage.get_latest_outgoing_command_message(profile_id, CHAT, TOPIC) is None
        # 要回复群里某条消息的写指令接口替不了，照常群发
        _, calls = await run_queue(client, storage, profile_id, ".天机代卜", (200, RETURN_BODY), reply_to_msg_id=777)
        assert calls == [] and client.sent == [".天机代卜"], (calls, client.sent)


async def unknown_write_result_is_not_resent():
    for failure in (TimeoutError("read timed out"), (500, {"ok": False, "error": "internal_error"}),
                    (200, {"ok": True, "actionResult": {"ok": True}})):
        with case(writes=True) as (storage, profile_id):
            client = FakeClient(storage, profile_id)
            row, calls = await run_queue(client, storage, profile_id, ".侍妾远航 稳妥", failure)
            assert calls == [".侍妾远航 稳妥"] and not client.sent, (failure, calls, client.sent)
            # 照「发出去了、bot 没回」记账：不确认、不失败，到时被标成待人工确认
            synthetic = cc.SYNTHETIC_MESSAGE_ID_BASE + 2 * row["id"]
            assert row["status"] == "awaiting_confirm" and row["sent_message_id"] == synthetic, (failure, row)
            assert not client.observer.seen, client.observer.seen
            # 该号进退避：下一条查询直接群发
            _, calls = await run_queue(client, storage, profile_id, ".我的侍妾", (200, OK_BODY))
            assert calls == [] and client.sent == [".我的侍妾"], (calls, client.sent)


async def write_not_executed_falls_back_to_group():
    for failure in (urllib.error.URLError(ConnectionRefusedError()), (522, {"text": "<html>522</html>"}),
                    (200, {"ok": False, "error": "external_action_rate_limited"}),
                    (400, {"ok": False, "error": "command_not_allowed"})):
        with case(writes=True) as (storage, profile_id):
            client = FakeClient(storage, profile_id)
            row, calls = await run_queue(client, storage, profile_id, ".入梦寻图", failure)
            assert calls == [".入梦寻图"] and client.sent == [".入梦寻图"], (failure, calls, client.sent)
            assert row["status"] == "awaiting_confirm" and row["sent_message_id"] == 2001, (failure, row)


async def query_timeout_still_falls_back():
    with case() as (storage, profile_id):
        client = FakeClient(storage, profile_id)
        _, calls = await run_queue(client, storage, profile_id, ".远航状态", TimeoutError("read timed out"))
        assert calls == [".远航状态"] and client.sent == [".远航状态"], (calls, client.sent)


def write_command_rules():
    for text in (".远航归来", ".召回侍妾", ".召回侍妾 莎儿", ".侍妾远航 稳妥", ".侍妾远航 月殿寻痕",
                 ".入梦寻图", ".天机代卜", ".拼图", ".坠魔心劫", ".安置侍妾"):
        assert cc.is_write_command(text), text
    for text in (".侍妾远航", ".侍妾远航 乱跑", ".拼图 1", ".我的侍妾", ".共历心劫", ".稳"):
        assert not cc.is_write_command(text), text
    assert cc.not_executed(522, {"text": "x"}) and cc.not_executed(200, {"ok": False, "error": "auth_date_expired"})
    assert cc.not_executed(503, {"ok": False, "error": "busy", "retryable": True})
    assert not cc.not_executed(500, {"ok": False, "error": "internal_error"}) and not cc.not_executed(502, {"text": ""})


def reply_text_rules():
    assert cc.reply_text(200, OK_BODY) == GROUP_TEXT
    assert cc.reply_text(400, {"ok": False, "error": "command_not_allowed"}) == ""
    assert cc.reply_text(200, {"ok": False, "error": "rate_limited"}) == ""
    assert cc.reply_text(200, {"ok": True, "actionResult": {"ok": True}}) == ""
    # 业务拒绝也是游戏 bot 会在群里回的话，照样交给解析
    refusal = {"ok": True, "actionResult": {"ok": False, "message": "你当前没有侍妾在远航。"}}
    assert cc.reply_text(200, refusal) == "你当前没有侍妾在远航。"


async def main():
    def deny(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise AssertionError("network forbidden")
    sys.addaudithook(deny)
    reply_text_rules()
    write_command_rules()
    await api_reply_goes_through_router()
    await api_failure_falls_back_to_group()
    await switch_and_whitelist_gate_the_api()
    await writes_need_their_own_switch()
    await unknown_write_result_is_not_resent()
    await write_not_executed_falls_back_to_group()
    await query_timeout_still_falls_back()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
