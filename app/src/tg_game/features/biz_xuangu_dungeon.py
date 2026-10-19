"""玄骨遗府自动出招：本号在队里时，每轮看到面板就照自己的分工发 `.玄骨行动 <轮次> <动作>`。

10-19 用户定：只出招，不自动进房——房由自己或队友开、自己进；那支固定队不去抢位。
打法照群里 10-16 15:09 ~ 10-19 的 17 场（17 场全部争取全收，0 失败）：

    分工   1     2     3     4     5
    探禁   验骨  —     侦察  侦察  —
    攻伐   采莲  破敌  破敌  —     亮招 破敌
    护队   护队  护队  护队  护队  护队
    镇魂   —     镇魂  —     镇魂  镇魂
    御傀   —     引蛇  —     护傀  收神

- 分工从面板「探禁 @甲｜攻伐 @乙…」里认本号（出发时游戏自动补齐，所以不用自己选）；
  绑定群和只记录的老群都管，房在哪个群、哪个话题就在哪儿发
- 面板「需……」那句不在 KNOWN_HINTS 里（玄骨换了线索），这轮不出招、推收藏夹，人来判断
- 第 5 轮攻伐：发完亮招，到第 23 秒群里还没人发 `.玄骨行动 5 收神`，改发收神
  （截止前可改；没收神扣 37 魂稳、选不了全收，比丢亮招亏）
- 本号发过 `.进入玄骨`（当队长）、收尾面板标了「争取全收（可选）」，发 `.玄骨收尾 争取全收`；
  其他情况不发，超时游戏自己按立即撤离结算（收拢遗宝没人试过）
- 结算里有本号那一行，推一条到收藏夹
开关 runtime_state `xuangu_dungeon_auto:<pid>` = {"enabled": true}，没配就是关。
"""
import asyncio
import json
import logging
import random
import re
import time

from tg_game.config import get_settings
from tg_game.services.automation_switch import is_automation_paused
from tg_game.services.runtime_drain import tracked_flow
from tg_game.telegram.send_utils import send_message_with_thread_fallback

logger = logging.getLogger(__name__)

AUTO_SWITCH_KEY = "xuangu_dungeon_auto:{}"
SCRIPT = {
    "探禁": {1: "验骨", 3: "侦察", 4: "侦察"},
    "攻伐": {1: "采莲", 2: "破敌", 3: "破敌", 5: "亮招 破敌"},
    "护队": {1: "护队", 2: "护队", 3: "护队", 4: "护队", 5: "护队"},
    "镇魂": {2: "镇魂", 4: "镇魂", 5: "镇魂"},
    "御傀": {2: "引蛇", 4: "护傀", 5: "收神"},
}
# 面板出来后第几秒发：每轮 30 秒，回包慢 2~3 秒，全部在前 16 秒发完；几个号同队时还要过全局 4 秒发送间隔
ROLE_DELAY = {"探禁": 2, "攻伐": 5, "护队": 8, "镇魂": 11, "御傀": 14}
DELAY_JITTER = 1.5
# 面板里「需……」那句，见过的原文；第 4 轮有两种（10-16~10-19：队友 11 次、傀躯 6 次），上面的动作两种都覆盖
KNOWN_HINTS = {
    1: ("先验骨、试池，留人掩护",),
    2: ("需有人引蛇、有人护队，再集中破敌",),
    3: ("需侦察背后、护队并压制骨傀",),
    4: ("真杀机在傀躯：护傀、镇魂，另以侦察或画轴辨明分魂", "真杀机在队友：护队、镇魂，另以侦察或画轴辨明分魂"),
    5: ("御傀者须收神；队友护队、镇魂并压制追魂",),
}
SHOUSHEN_COMMAND = ".玄骨行动 5 收神"
SHOUSHEN_FALLBACK_AT = 23
FINISH_COMMAND = ".玄骨收尾 争取全收"
# 超过这么久的面板不动：重连补发的旧消息，轮次早结束了
PANEL_MAX_AGE = 20
# 几个 bot 会各发一遍同一张面板，同一号同一群同一轮这么久内只认一次；下一场同一轮至少隔 3 分钟
DEDUPE_SECONDS = 120
LEADER_SECONDS = 15 * 60

_ROUND_TITLE = re.compile(r"【玄骨遗府·第(\d)轮")
_ROLE = re.compile(r"(探禁|攻伐|护队|镇魂|御傀) @([^｜\s]+)")
_ACTIONS = re.compile(r"本轮动作：([^（\n]+)")
_SETTLE = re.compile(r"【玄骨遗府·(争取全收|收拢遗宝|立即撤离|魂阵溃散)】")

# ponytail: 进程内存；服务重启赶上一场打到一半，最多丢这场的队长收尾和结算推送，面板本身按时间判旧
_seen: dict = {}
_leader_at: dict = {}
_shoushen_at: dict = {}
_tasks: set = set()


def plan_round(text: str, names) -> dict:
    """轮次面板 → {round, role, action, known}；不是面板或本号不在这一队返回空 dict。"""
    match = _ROUND_TITLE.search(str(text or ""))
    if not match:
        return {}
    roles = {name.lower(): role for role, name in _ROLE.findall(text)}
    role = next((roles[name] for name in names if name in roles), "")
    if not role:
        return {}
    number = int(match.group(1))
    action = SCRIPT[role].get(number, "")
    offered = _ACTIONS.search(text)
    offered = {item.strip() for item in offered.group(1).split("/")} if offered else set()
    known = any(hint in text for hint in KNOWN_HINTS.get(number, ())) and (
        not action or action.replace("亮招 ", "") in offered
    )
    return {"round": number, "role": role, "action": action, "known": known}


def _names(profile) -> list:
    raw = (getattr(profile, "telegram_username", ""), getattr(profile, "account_name", ""))
    return [name for name in dict.fromkeys(str(item or "").strip().lstrip("@").lower() for item in raw) if name]


def _switch_on(storage, profile_id) -> bool:
    try:
        return bool(json.loads(storage.get_runtime_state(AUTO_SWITCH_KEY.format(profile_id)) or "{}").get("enabled"))
    except (TypeError, ValueError, AttributeError):
        return False


def _once(key, now) -> bool:
    if now - _seen.get(key, 0) < DEDUPE_SECONDS:
        return False
    _seen[key] = now
    return True


def _spawn(coroutine) -> None:
    task = asyncio.get_running_loop().create_task(coroutine)
    _tasks.add(task)  # 留个引用，免得还没跑完就被回收
    task.add_done_callback(_tasks.discard)


def observe(context, storage) -> str:
    """路由对每条群消息都调一次；返回做了什么（自检用）。"""
    chat_id = int(context.chat_id or 0)
    if not chat_id or not (context.chat_binding or chat_id in get_settings().record_only_chat_ids):
        return ""
    text = context.text
    now = time.time()
    if text.startswith(SHOUSHEN_COMMAND):
        _shoushen_at[chat_id] = now  # 谁发的都算，攻伐兜底只看群里有没有人收神
        return "shoushen-seen"
    profile = getattr(context, "profile", None)
    if profile is None or getattr(context, "client", None) is None:
        return ""
    if text.startswith(".进入玄骨"):
        if not context.is_outgoing:
            return ""
        _leader_at[(profile.id, chat_id)] = now
        return "leader"
    # 不看 is_bot_sender：发送者实体没加载时它是假的，存档里有过这样的轮次面板；有人贴面板骗我们出招也只是白发一条
    if not text.startswith("【玄骨遗府·") or context.is_outgoing:
        return ""
    if _ROUND_TITLE.match(text):
        return _on_round(context, storage, profile, chat_id, now)
    if not _switch_on(storage, profile.id):
        return ""
    if text.startswith("【玄骨遗府·残图异动】"):
        if "争取全收（可选）" not in text or now - _leader_at.get((profile.id, chat_id), 0) > LEADER_SECONDS:
            return ""
        if not _once((profile.id, chat_id, "finish"), now):
            return "dup"
        _spawn(xuangu_dungeon_send(context.client, storage, profile.id, chat_id, context.thread_id,
                                   FINISH_COMMAND, random.uniform(2, 4)))
        return "finish-armed"
    settle = _SETTLE.match(text)
    if not settle:
        return ""
    names = _names(profile)
    mine = next((line for line in text.split("\n") if any(line.lower().startswith(f"@{name}：") for name in names)), "")
    if not mine and not (settle.group(1) == "魂阵溃散" and now - _seen.get((profile.id, chat_id, "played"), 0) < 600):
        return ""
    if not _once((profile.id, chat_id, "settle"), now):
        return "dup"
    _spawn(_notify(context.client, f"【玄骨遗府·{settle.group(1)}】{mine or '魂稳归零，这场失败了'}"))
    return "notified"


def _on_round(context, storage, profile, chat_id, now) -> str:
    plan = plan_round(context.text, _names(profile))
    if not plan:
        return ""
    if not _switch_on(storage, profile.id):
        logger.info("玄骨遗府第 %s 轮 profile=%s 是%s，自动出招没开", plan["round"], profile.id, plan["role"])
        return "off"
    date = getattr(context.event, "date", None)
    panel_at = date.timestamp() if hasattr(date, "timestamp") else now
    if now - panel_at > PANEL_MAX_AGE:
        return "stale"
    if not _once((profile.id, chat_id, plan["round"]), now):
        return "dup"
    _seen[(profile.id, chat_id, "played")] = now
    if not plan["known"]:
        logger.warning("玄骨遗府第 %s 轮 profile=%s 面板和已知的对不上，不出招", plan["round"], profile.id)
        _spawn(_notify(context.client, f"玄骨遗府第 {plan['round']} 轮面板和已知的对不上，这轮没自动出招"
                                       f"（本号是{plan['role']}），快去看一眼：\n\n{context.text[:800]}"))
        return "unknown"
    if not plan["action"]:
        return "idle"
    command = f".玄骨行动 {plan['round']} {plan['action']}"
    delay = panel_at + ROLE_DELAY[plan["role"]] + random.uniform(0, DELAY_JITTER) - now
    fallback_at = panel_at + SHOUSHEN_FALLBACK_AT if plan["role"] == "攻伐" and plan["round"] == 5 else 0
    logger.info("玄骨遗府第 %s 轮 profile=%s 是%s，%.0f 秒后发 %s", plan["round"], profile.id, plan["role"], delay, command)
    _spawn(xuangu_dungeon_send(context.client, storage, profile.id, chat_id, context.thread_id, command, delay, fallback_at))
    return "armed"


@tracked_flow
async def xuangu_dungeon_send(client, storage, profile_id, chat_id, thread_id, command, delay, fallback_at=0):
    await asyncio.sleep(max(0.0, delay))
    if not await _send(client, storage, profile_id, chat_id, thread_id, command) or not fallback_at:
        return
    await asyncio.sleep(max(0.0, fallback_at - time.time()))
    if _shoushen_at.get(chat_id, 0) >= fallback_at - SHOUSHEN_FALLBACK_AT:
        return
    logger.warning("玄骨遗府 profile=%s 第 5 轮到第 %s 秒还没人收神，攻伐改发收神", profile_id, SHOUSHEN_FALLBACK_AT)
    await _send(client, storage, profile_id, chat_id, thread_id, SHOUSHEN_COMMAND)


async def _send(client, storage, profile_id, chat_id, thread_id, command) -> bool:
    if not _switch_on(storage, profile_id) or is_automation_paused(storage):
        logger.info("玄骨遗府 profile=%s 等的时候开关关了或全局暂停，不发 %s", profile_id, command)
        return False
    try:
        # 不回复面板：房在老群就直接发，在绑定话题里 thread_id 是话题根
        await send_message_with_thread_fallback(
            client, chat_id, command, thread_id=thread_id, storage=storage, profile_id=profile_id,
            bot_username="fanrenxiuxian_bot", log_prefix="玄骨遗府", guard_network_pause=True,
            allow_listen_only=True,  # 老群是只听群，但房开在那里时照样出招
        )
    except Exception:  # 每轮只发这一次：过了截止就没用了，不重试
        logger.exception("玄骨遗府 profile=%s %s 没发出去", profile_id, command)
        return False
    logger.info("玄骨遗府 profile=%s 已发 %s", profile_id, command)
    return True


async def _notify(client, text) -> None:
    try:
        await client.send_message("me", text)  # 发自己收藏夹，不进群
    except Exception:
        logger.exception("玄骨遗府收藏夹通知没发出去")
