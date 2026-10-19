"""心劫等面板时不认指令中心的合成面板（10-17 17:02、22:05：.共历心劫 回复了群里不存在的消息，落成话题消息被 bot 拒）。

合成面板也是「回复本号 .我的侍妾」，原来的兜底会收下它；现在只认群里的真面板。
运行：PYTHONPATH=app/src python -B tools/test_heart_tribulation_synthetic_panel.py
全程临时数据库与假客户端，不连接 Telegram。
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))
from tg_game.features.estate import biz_command_center as cc  # noqa: E402
from tg_game.runtime import executors as ex  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.telegram import send_utils  # noqa: E402

CHAT, TOPIC, BOT = -100000000088, 42, 333
PANEL = (  # 10-17 探针拿到的面板格式（去掉 Markdown 后）
    "1. 你的红尘道侣: 【莎儿】 (状态: 随行中)\n\n【第二期机缘】\n- 天机代卜链: 无\n"
    "- 入梦寻图冷却: 207分钟\n- 共历心劫冷却: 可施展\n- 天机代卜冷却: 447分钟\n"
    "命令: .入梦寻图、.残图、.拼图、.共历心劫、.坠魔心劫、.天机代卜"
)
SYNTHETIC_COMMAND = cc.SYNTHETIC_MESSAGE_ID_BASE + 2 * 7


async def noop(*args, **kwargs):
    pass


async def main():
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "heart.db")
        storage.init_schema()
        profile = storage.create_profile("synthetic-panel")
        task = storage.upsert_companion_heart_tribulation_task(
            profile_id=profile.id, chat_id=CHAT, enabled=True, thread_id=TOPIC, run_id="run",
            workflow_state=ex.COMPANION_HEART_TRIBULATION_AWAIT_PANEL_STATE, next_run_at=0,
            step_deadline_at=time.time() + 300, anchor_command_msg_id=500,
        )
        # 别的流程排队查的 .我的侍妾 走了指令中心：本号的合成指令消息照常落库
        storage.upsert_bound_message(
            profile_id=profile.id, chat_id=CHAT, thread_id=TOPIC, message_id=SYNTHETIC_COMMAND,
            reply_to_msg_id=None, sender_id=0, sender_username="", direction="outgoing", is_bot=False,
            text=".我的侍妾",
        )
        sent = []

        async def send(chat_id, text, **kwargs):
            sent.append((text, kwargs.get("reply_to")))
            return SimpleNamespace(id=900 + len(sent))

        async def raw(request):  # 话题内回复走原始 SendMessageRequest
            return await send(request.peer, request.message, reply_to=request.reply_to.reply_to_msg_id)

        client = type("C", (), {"send_message": staticmethod(send), "get_input_entity": staticmethod(lambda c: _ret(c)),
                                "_get_response_message": staticmethod(lambda req, res, peer: res),
                                "__call__": lambda self, req: raw(req), "_tg_game_profile_id": profile.id})()

        def panel_event(message_id, reply_to):
            return SimpleNamespace(
                profile=profile, chat_id=CHAT, thread_id=TOPIC, message_id=message_id, reply_to_msg_id=reply_to,
                sender_id=BOT, text=PANEL, is_outgoing=False, chat_binding=SimpleNamespace(bot_ids=[BOT]),
                client=client, event=SimpleNamespace(sender=SimpleNamespace(username="hantianzun1_bot")),
            )

        def advance(context):
            return ex.GeneralGameExecutor._maybe_advance_companion_heart_tribulation(None, context, storage)

        with patch.object(ex.asyncio, "sleep", noop), patch.object(send_utils, "_throttle_outgoing_send", noop):
            # 合成面板（回复本号合成的 .我的侍妾）：不收、不发 .共历心劫
            assert await advance(panel_event(SYNTHETIC_COMMAND + 1, SYNTHETIC_COMMAND)) is False
            row = storage.get_companion_heart_tribulation_task(profile.id, CHAT, thread_id=TOPIC)
            assert not sent and row["workflow_state"] == task["workflow_state"], (sent, row["workflow_state"])
            # 群里的真面板（回复心劫自己发的 .我的侍妾 500）：照常回复它发 .共历心劫
            assert await advance(panel_event(501, 500)) is True
            assert sent == [(".共历心劫", 501)], sent
    print("heart tribulation synthetic panel: ok")


async def _ret(value):
    return value


if __name__ == "__main__":
    asyncio.run(main())
