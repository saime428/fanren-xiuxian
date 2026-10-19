"""只听群（TG_GAME_LISTEN_ONLY_CHAT_IDS）：消息交给监听，发送一律拦下。

- 启动时给号补一条停用绑定；选群发指令仍然只认主群（只听群没话题也不会被优先选中）；
- 只听群的消息能解析出那条停用绑定，监听照常拿到；只记录群的原始存档照写；
- 三个发送出口都拦：底层群发送、事件 reply、发送队列（含指令中心接口代发）。
run: PYTHONPATH=app/src python tools/test_listen_only_chat.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

MAIN, TOPIC, OLD = -1005550000777, 4242, -1005550000888
os.environ.update(TG_GAME_BOUND_CHAT_ID=str(MAIN), TG_GAME_BOUND_THREAD_ID=str(TOPIC), AUTHORIZED_USER_ID="123",
                  TG_GAME_LISTEN_ONLY_CHAT_IDS=str(OLD), TG_GAME_RECORD_ONLY_CHAT_IDS=str(OLD))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.config import LISTEN_ONLY_SEND_ERROR, ListenOnlyChatError, ensure_chat_sendable  # noqa: E402
from tg_game.runtime.context import EventContext  # noqa: E402
from tg_game.runtime.router import Router  # noqa: E402
from tg_game.storage import Storage  # noqa: E402
from tg_game.telegram import send_utils  # noqa: E402


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=1)


def event(chat_id, text="【星辰异象】某道友的星盘", top=None):
    reply_to = SimpleNamespace(reply_to_msg_id=top, reply_to_top_id=top, top_msg_id=top) if top else None
    message = SimpleNamespace(reply_to=reply_to, reply_to_top_id=top, top_msg_id=top)

    async def reply(_text):
        raise AssertionError("event.reply must not run for a listen-only chat")

    return SimpleNamespace(chat_id=chat_id, id=55, message=message, raw_text=text, text=text,
                           sender_id=777, sender=SimpleNamespace(username="other"), out=False,
                           reply=reply)


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "t.db")
        storage.init_schema()
        profile = storage.create_profile("p", activate=True)
        storage.bind_profile_telegram_account(profile.id, telegram_user_id="123")
        router = Router(storage, [], runtime_profile_id=profile.id)
        await router.startup(FakeClient())

        old = storage.get_chat_binding(profile.id, OLD)
        assert old is not None and not old.is_active, old
        primary = storage.get_primary_chat_binding(profile.id)
        assert (primary.chat_id, primary.thread_id) == (MAIN, TOPIC), primary
        await router.startup(FakeClient())  # 再启动不重复建
        assert len([b for b in storage.list_chat_bindings(profile.id) if b.chat_id == OLD]) == 1

        context = router._build_context(FakeClient(), event(OLD))
        assert context.chat_binding is not None and context.chat_binding.chat_id == OLD
        assert router._build_context(FakeClient(), event(MAIN, top=TOPIC)).chat_binding.chat_id == MAIN

        try:
            ensure_chat_sendable(OLD)
            raise AssertionError("listen-only chat accepted")
        except ListenOnlyChatError as exc:
            assert str(exc) == LISTEN_ONLY_SEND_ERROR
        ensure_chat_sendable(MAIN)

        client = FakeClient()
        for chat_id in (OLD,):
            try:
                await send_utils.send_message_with_thread_fallback(client, chat_id, ".闭关修炼", storage=storage,
                                                                   profile_id=profile.id)
                raise AssertionError("send to listen-only chat went through")
            except ListenOnlyChatError:
                pass
        assert client.sent == []
        # 调用方点名放行的（玄骨遗府房开在老群）照发
        await send_utils.send_message_with_thread_fallback(client, OLD, ".玄骨行动 1 侦察", storage=storage,
                                                           profile_id=profile.id, allow_listen_only=True)
        assert client.sent == [(OLD, ".玄骨行动 1 侦察")], client.sent
        try:
            await EventContext(client=client, event=event(OLD), profile=profile, chat_binding=old,
                               runtime_profile_id=profile.id).reply("hi")
            raise AssertionError("reply in listen-only chat went through")
        except ListenOnlyChatError:
            pass

        # 只记录群照常存档（赌石服务读这张表），即使现在能解析出停用绑定
        await router.dispatch(FakeClient(), event(OLD))
        with storage.connect() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM recorded_messages WHERE chat_id=?", (OLD,)).fetchone()[0]
        assert rows == 1, rows
    print("listen-only chat: ok")


if __name__ == "__main__":
    asyncio.run(main())
