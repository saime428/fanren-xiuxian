"""玄骨遗府自动出招自检：轮次面板用 10-16~10-19 群里的原文（名字换成假的），
认分工、查打法、两种第 4 轮提示、陌生提示不出招、开关/旧面板/重复面板、攻伐兜底收神、队长收尾、结算推送。
临时数据库，不连接游戏。

运行：uv run --with-requirements requirements.txt python -X utf8 -B tools/test_xuangu_dungeon.py
"""
import asyncio
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app" / "src"))
from tg_game.features import biz_xuangu_dungeon as xd
from tg_game.storage import Storage

OLD_GROUP = -100000000002
ROSTER = "探禁 @member_a｜攻伐 @member_b｜护队 @member_c｜镇魂 @member_d｜御傀 @member_e"
HINTS = {
    1: ("彩莲悬着七色霞光。池边白骨的右手乌黑，食指还缺了一截；池水乳白，深处却有黑影。先验骨、试池，留人掩护。",
        "验骨 / 试池 / 护队 / 采莲"),
    2: ("彩莲缩入水中，竟是妖冠蛇的肉冠！蛇首盯住人群，毒液将随扑击飞溅。需有人引蛇、有人护队，再集中破敌。",
        "引蛇 / 破敌 / 护队 / 镇魂"),
    3: ("池中残尸摇晃着站起，门边散骨却也悄悄挪动。玄骨诱言：‘不想死的，现在就走。’需侦察背后、护队并压制骨傀。",
        "侦察 / 护队 / 破敌"),
    4: ("正面魂影暴涨，傀躯血光却逆流，火鸟齐齐转向它身后。真杀机在傀躯：护傀、镇魂，另以侦察或画轴辨明分魂。",
        "侦察 / 护傀 / 护队 / 镇魂 / 画轴"),
    5: ("玄骨强摄煞丹傀躯！御傀者须收神；队友护队、镇魂并压制追魂。若御傀者失联，两人镇魂加一人收神也可接应。"
        "争取全收还需本轮至少一人有效亮招，压出搜宝窗口。", "收神 / 护队 / 镇魂 / 破敌"),
}
HINT_4_TEAMMATE = "傀躯气息稳定，队友识海却传来外力，魂影下的火鸟猛扑向人群。真杀机在队友：护队、镇魂，另以侦察或画轴辨明分魂。"


def panel(number, hint=None, roster=ROSTER):
    text, actions = HINTS[number]
    return (f"【玄骨遗府·第{number}轮：某关】\n上一轮的结果。\n本轮魂稳 -0，剩余 100。\n\n{hint or text}\n"
            f"魂稳 100｜傀躯：受控\n已识破：无\n{roster}\n尚未亮招：@member_a、@member_b\n玄骨当前防备：无\n"
            f"本轮动作：{actions}（均可防守）\n.玄骨行动 {number} 动作；或 .玄骨行动 {number} 亮招 动作\n"
            "剩余30秒，提交0/5：无；交齐立即结算。")


FINISH = ("【玄骨遗府·残图异动】\n分神及时回归，队友截住追魂；玄骨只夺走了空出的傀躯。\n魂稳 97｜傀躯：已夺舍\n"
          "立即撤离（可选）\n收拢遗宝（可选）\n争取全收（可选）\n队长 .玄骨收尾 立即撤离/收拢遗宝/争取全收；30秒内，超时撤离。")
SETTLE = ("【玄骨遗府·争取全收】\n分神及时回归，队友截住追魂；玄骨只夺走了空出的傀躯。\n"
          "@member_b：修为+9000、贡献+450、魂纹+3；养魂木×4、傀儡核心×2、养魂木×4\n"
          "@member_c：修为+6000、贡献+450、魂纹+3；阴凝之晶×2、养魂木×4、傀儡核心×2、首通徽章【老魔眼下留一手】")


class Client:
    def __init__(self):
        self.saved = []

    async def send_message(self, peer, text):
        assert peer == "me", peer
        self.saved.append(text)


def ctx(text, profile, client, *, age=1, chat=OLD_GROUP, bound=False, outgoing=False):
    event = SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(seconds=age))
    return SimpleNamespace(chat_id=chat, text=text, profile=profile, client=client, event=event,
                           chat_binding=object() if bound else None, is_outgoing=outgoing, thread_id=None)


def check_script():
    names = ["member_" + letter for letter in "abcde"]
    for number in range(1, 6):
        for role, name in zip(xd.SCRIPT, names):
            plan = xd.plan_round(panel(number), [name])
            assert plan == {"round": number, "role": role, "action": xd.SCRIPT[role].get(number, ""), "known": True}, plan
    # 第 4 轮另一种提示也照打（侦察/护队/镇魂/护傀 两种都覆盖）
    assert xd.plan_round(panel(4, HINT_4_TEAMMATE), ["member_e"]) == {"round": 4, "role": "御傀", "action": "护傀", "known": True}
    # 没见过的线索：不认
    assert not xd.plan_round(panel(4, "真杀机在正面：破敌、侦察。"), ["member_e"])["known"]
    # 本号不在这一队、不是轮次面板
    assert xd.plan_round(panel(1), ["someone_else"]) == {}
    assert xd.plan_round(FINISH, ["member_a"]) == {}
    # 名字不分大小写（游戏面板和 TG 用户名大小写可能不一致）
    assert xd.plan_round(panel(2, roster=ROSTER.replace("member_e", "Member_E")), ["member_e"])["action"] == "引蛇"


async def check_flow(storage):
    sent = []

    async def fake_send(client, chat_id, text, **kwargs):
        assert kwargs.get("allow_listen_only") is True, "老群是只听群，出招要点名放行"
        sent.append((chat_id, text, kwargs.get("thread_id")))

    xd.send_message_with_thread_fallback = fake_send
    xd.get_settings = lambda: SimpleNamespace(record_only_chat_ids=(OLD_GROUP,))
    xd.SHOUSHEN_FALLBACK_AT = 19.3  # 面板按 19 秒前算：各分工都立刻发，兜底只等零点几秒
    me = SimpleNamespace(id=7, telegram_username="member_b", account_name="")
    client = Client()
    switch = xd.AUTO_SWITCH_KEY.format(me.id)

    def reset():
        for table in (xd._seen, xd._leader_at, xd._shoushen_at):
            table.clear()
        sent.clear()
        client.saved.clear()

    async def settle():
        await asyncio.sleep(0.6)

    assert xd.observe(ctx(panel(1), me, client), storage) == "off"
    storage.set_runtime_state(switch, '{"enabled": true}')
    # 不在名单里的群不管
    assert xd.observe(ctx(panel(1), me, client, chat=-100000000009), storage) == ""
    # 第 1 轮攻伐采莲；同一张面板另一个 bot 再发一遍只认一次；重连补发的旧面板不动
    assert xd.observe(ctx(panel(1), me, client, age=19), storage) == "armed"
    assert xd.observe(ctx(panel(1), me, client, age=19), storage) == "dup"
    assert xd.observe(ctx(panel(2), me, client, age=45), storage) == "stale"
    await settle()
    assert sent == [(OLD_GROUP, ".玄骨行动 1 采莲", None)], sent

    # 第 4 轮攻伐没动作
    reset()
    assert xd.observe(ctx(panel(4), me, client), storage) == "idle" and not sent

    # 第 5 轮：亮招之后没人收神 → 改发收神
    reset()
    assert xd.observe(ctx(panel(5), me, client, age=19), storage) == "armed"
    await settle()
    assert [text for _, text, _ in sent] == [".玄骨行动 5 亮招 破敌", ".玄骨行动 5 收神"], sent
    # 御傀收神了（谁发的都算）→ 不兜底
    reset()
    assert xd.observe(ctx(panel(5), me, client, age=19), storage) == "armed"
    assert xd.observe(ctx(".玄骨行动 5 收神", None, None), storage) == "shoushen-seen"
    await settle()
    assert [text for _, text, _ in sent] == [".玄骨行动 5 亮招 破敌"], sent

    # 陌生线索：不出招，推收藏夹
    reset()
    assert xd.observe(ctx(panel(3, "需全员撤离。"), me, client), storage) == "unknown"
    await settle()
    assert not sent and "第 3 轮" in client.saved[0], client.saved

    # 收尾：不是队长不发；本号发过 .进入玄骨 才发争取全收；绑定群照样管
    reset()
    assert xd.observe(ctx(FINISH, me, client, bound=True, chat=-100000000009), storage) == ""
    assert xd.observe(ctx(".进入玄骨", me, client, bound=True, chat=-100000000009, outgoing=True), storage) == "leader"
    assert xd.observe(ctx(FINISH, me, client, bound=True, chat=-100000000009), storage) == "finish-armed"
    assert xd.observe(ctx(FINISH, me, client, bound=True, chat=-100000000009), storage) == "dup"
    await asyncio.sleep(4.2)  # 收尾等 2~4 秒
    assert sent == [(-100000000009, ".玄骨收尾 争取全收", None)], sent
    # 没标「争取全收（可选）」就不发，等超时撤离
    reset()
    xd._leader_at[(me.id, OLD_GROUP)] = time.time()
    assert xd.observe(ctx(FINISH.replace("争取全收（可选）", "争取全收（需终轮有效亮招）"), me, client), storage) == ""

    # 结算里有本号那一行 → 推收藏夹，两个 bot 各发一遍只推一次
    reset()
    assert xd.observe(ctx(SETTLE, me, client), storage) == "notified"
    assert xd.observe(ctx(SETTLE, me, client), storage) == "dup"
    await settle()
    assert client.saved == ["【玄骨遗府·争取全收】@member_b：修为+9000、贡献+450、魂纹+3；养魂木×4、傀儡核心×2、养魂木×4"], client.saved
    # 魂阵溃散没有逐人行：这场本号出过招才推
    reset()
    assert xd.observe(ctx("【玄骨遗府·魂阵溃散】\n玄骨声东击西。", me, client), storage) == ""
    xd.observe(ctx(panel(2), me, client), storage)
    assert xd.observe(ctx("【玄骨遗府·魂阵溃散】\n玄骨声东击西。", me, client), storage) == "notified"

    # 本号自己发的东西不当面板；开关关了不发
    reset()
    assert xd.observe(ctx(panel(1), me, client, outgoing=True), storage) == ""
    storage.set_runtime_state(switch, '{"enabled": false}')
    assert xd.observe(ctx(panel(1), me, client), storage) == "off"
    await settle()


def main():
    check_script()
    with tempfile.TemporaryDirectory() as folder:
        storage = Storage(Path(folder) / "xd.db")
        storage.init_schema()
        asyncio.run(check_flow(storage))
    print("xuangu dungeon: ok")


if __name__ == "__main__":
    main()
