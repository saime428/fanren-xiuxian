"""群指令改走洞府「天机阁指令中心」（docs/dwelling-command-center-plan.md；批 1 查询、批 2 侍妾写操作）。

接口 POST /api/miniapp/xianxia-dwelling/command-center {token, initData, command} 把游戏 bot 本该在群里
回的那段文字放在 actionResult.rawMessage 里（Markdown 原文，去掉标记后和群里的 raw_text 逐行一致，
10-17 三个号实测）。拿到后包成一条「游戏 bot 回复本号指令」的合成消息交给现有路由，40 多处回包解析
一行不改。

结果分三种：
- 有回包文字：确定结果（成功或业务拒绝都算），注入路由；
- 服务器明确没执行（连不上、鉴权/入口/白名单/限流/忙）：这一次退回群发，该号 10 分钟内全走群发；
- 写操作请求发出去以后断了、或服务器报了说不清的错：结果不明。不重发也不群发，指令照「发了没等到回包」
  记账，等下一次 .我的侍妾 / .远航状态 对账（FanRenXiuXian 的垂钓、噬金虫都吃过重放的亏）。查询照旧退回群发。
"""

import asyncio
import json
import logging
import re
import time
import urllib.error
from types import SimpleNamespace
from typing import Optional

from telethon.extensions import markdown

from tg_game.features.estate import biz_estate_miniapp as estate

logger = logging.getLogger(__name__)

SWITCH_KEY = "command_center:{}"
QUERY_COMMANDS = frozenset({".我的侍妾", ".远航状态"})
# 批 2：侍妾写操作，开关是同一个键里的 "writes"。FanRenXiuXian 从 09-21 起 6 个号都这么发（pavilion_loop/）。
WRITE_COMMANDS = frozenset({".远航归来", ".召回侍妾", ".入梦寻图", ".天机代卜", ".拼图", ".坠魔心劫", ".安置侍妾"})
VOYAGE_ROUTES = frozenset({"稳妥", "均衡", "冒险", "月殿寻痕"})  # 官方白名单 /^\.侍妾远航\s+(…)$/
# 服务器拒收、没执行的错误码（官方洞府前端 errorText 表）：鉴权、入口 token、白名单、限流、忙
_NOT_EXECUTED_ERROR = re.compile(
    r"^(auth_|init_data_|dwelling_token_|token_|command_not_allowed$|command_invalid$|server_busy$)|rate_limited"
)
# 401/403 鉴权、429 限流；52x 是 Cloudflare 连不上源站（10-17 p2 碰到过 522），请求没到游戏服务器
_NOT_EXECUTED_HTTP = frozenset({401, 403, 429, 521, 522, 523, 525, 526})
# 合成消息号：真实群消息号 10-17 才到 1279 万，int32 上限内够用几十年；
# 用正数是因为好几处按 `<= 0` 当「没有消息」直接丢掉。指令 = 基数 + 2×排队 id，回包再 +1。
SYNTHETIC_MESSAGE_ID_BASE = 1_000_000_000
# ponytail: initData 有效期没实测，按 10 分钟复用；接口报错就丢掉缓存、该号 10 分钟内全走群发
INIT_DATA_TTL_SECONDS = 600
FAILURE_BACKOFF_SECONDS = 600

_init_data: dict[int, tuple[str, str, float]] = {}
_backoff_until: dict[int, float] = {}


def is_synthetic_message_id(message_id: object) -> bool:
    """合成消息只在本地库里有：要回复群消息的流程（比如 .共历心劫 回复面板）不能拿它当回复对象。"""
    try:
        return int(message_id or 0) >= SYNTHETIC_MESSAGE_ID_BASE
    except (TypeError, ValueError):
        return False


def _switch(storage, profile_id: int) -> dict:
    try:
        state = json.loads(storage.get_runtime_state(SWITCH_KEY.format(int(profile_id))) or "{}")
    except (TypeError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("enabled") else {}


def is_enabled(storage, profile_id: int) -> bool:
    return bool(_switch(storage, profile_id))


def is_write_command(text: str) -> bool:
    head, _, arg = str(text or "").strip().partition(" ")
    if head == ".侍妾远航":
        return arg.strip() in VOYAGE_ROUTES
    # .召回侍妾 <名字>：官方前端白名单只列了不带名字的，FanRenXiuXian 带名字发一直成功；真不收会回 command_not_allowed
    return head in WRITE_COMMANDS and (head == ".召回侍妾" or not arg)


def routes(storage, profile_id: int, text: str, reply_to_msg_id=None) -> bool:
    if reply_to_msg_id:
        return False  # 要回复群里某条消息的指令，接口替不了
    if text in QUERY_COMMANDS:
        return is_enabled(storage, profile_id)
    return is_write_command(text) and bool(_switch(storage, profile_id).get("writes"))


def not_executed(status_code: int, body: object) -> bool:
    """服务器明确没执行这条指令（可以放心改群发）。"""
    if int(status_code or 0) in _NOT_EXECUTED_HTTP:
        return True
    if not isinstance(body, dict):
        return False
    return bool(body.get("retryable")) or bool(_NOT_EXECUTED_ERROR.search(str(body.get("error") or "")))


def reply_text(status_code: int, body: object) -> str:
    """接口回包里 bot 原本要发到群里的文字；不是确定结果（拒绝、报错、空）就返回空串。"""
    if not 200 <= int(status_code or 0) < 300 or not isinstance(body, dict) or body.get("ok") is False:
        return ""
    action = body.get("actionResult") if isinstance(body.get("actionResult"), dict) else {}
    raw = str(action.get("rawMessage") or action.get("message") or "")
    # 群里存的是 Telegram 去掉格式实体后的 raw_text；bot 发群消息用的也是这套 Markdown
    return markdown.parse(raw)[0].strip() if raw else ""


def _post(request: dict) -> tuple[int, object]:
    return estate._coerce_response(estate._urllib_transport(request))


async def _fresh_init_data(client, storage, profile_id: int) -> tuple[str, str]:
    cached = _init_data.get(profile_id)
    if cached and time.time() - cached[2] < INIT_DATA_TTL_SECONDS:
        return cached[0], cached[1]
    discovery = await estate.resolve_estate_public_miniapp_launch(client, storage)
    if not discovery.get("ok"):
        raise RuntimeError(str(discovery.get("error") or "洞府公共入口未找到"))
    launch = dict(discovery.get("launch") or {})
    init_data = await estate.request_estate_miniapp_init_data(
        client,
        token=launch.get("token"),
        webview_url=launch.get("webview_url"),
        bot_username=launch.get("bot_username"),
        launch_context=launch,  # 公共入口失败时换成备用入口，token 跟着换
    )
    _init_data[profile_id] = (str(launch.get("token") or ""), init_data, time.time())
    return _init_data[profile_id][0], init_data


def _reply_bot(binding) -> tuple[int, str]:
    bot_ids = [int(value) for value in (getattr(binding, "bot_ids", None) or []) if value]
    if not bot_ids and getattr(binding, "bot_id", None):
        bot_ids = [int(binding.bot_id)]
    if not bot_ids:
        return 0, ""
    usernames = getattr(binding, "bot_usernames", None) or {}
    return bot_ids[0], str(usernames.get(bot_ids[0]) or getattr(binding, "bot_username", "") or "")


def _give_up(profile_id: int, text: str, reason: object, *, unknown: bool = False) -> None:
    _init_data.pop(profile_id, None)
    _backoff_until[profile_id] = time.time() + FAILURE_BACKOFF_SECONDS
    logger.warning(
        "指令中心%s profile=%s command=%s：%s",
        "结果不明，不重发也不群发" if unknown else "失败，改群发",
        profile_id, text, estate.sanitize_estate_miniapp_secret_text(reason),
    )


async def fetch_reply(client, storage, command: dict) -> Optional[SimpleNamespace]:
    """调指令中心。返回 None：没执行，调用方照常群发；返回带 text 的：确定回包（含合成的指令消息）；
    返回 text 为空的：写操作结果不明，调用方照「已发出、没等到回包」记账，不再群发。"""
    profile_id = int(command.get("profile_id") or 0)
    chat_id = int(command.get("chat_id") or 0)
    text = str(command.get("text") or "").strip()
    if _backoff_until.get(profile_id, 0) > time.time() or getattr(client, "_tg_game_router", None) is None:
        return None
    thread_id = command.get("thread_id")
    binding = storage.resolve_chat_binding_for_event(profile_id, chat_id, thread_id, None)
    bot_id, bot_username = _reply_bot(binding)
    if not bot_id:
        return None
    write = is_write_command(text)
    try:
        token, init_data = await _fresh_init_data(client, storage, profile_id)
        request = estate.build_estate_miniapp_request(
            "command_center", token=token, init_data=init_data, payload={"command": text}
        )
    except Exception as exc:
        _give_up(profile_id, text, exc)
        return None
    try:
        status_code, body = await asyncio.to_thread(_post, request)
    except urllib.error.URLError as exc:  # 连接、发送阶段就断了（HTTPError 在传输层已转成状态码）：服务器没收到整条请求
        _give_up(profile_id, text, exc)
        return None
    except Exception as exc:  # 请求已发出、等回包时断了（读超时、连接被重置）
        _give_up(profile_id, text, exc, unknown=write)
        return _reply(command, binding, thread_id, bot_id, bot_username, "") if write else None
    reply = reply_text(status_code, body)
    if not reply:
        error = body.get("error") or body.get("message") if isinstance(body, dict) else ""
        unknown = write and not not_executed(status_code, body)
        _give_up(profile_id, text, f"http {status_code} {error or '无回包文字'}", unknown=unknown)
        return _reply(command, binding, thread_id, bot_id, bot_username, "") if unknown else None
    logger.info("指令中心回包 profile=%s command=%s 合成消息=%s", profile_id, text, _command_message_id(command))
    return _reply(command, binding, thread_id, bot_id, bot_username, reply)


def _command_message_id(command: dict) -> int:
    return SYNTHETIC_MESSAGE_ID_BASE + 2 * int(command["id"])


def _reply(command: dict, binding, thread_id, bot_id: int, bot_username: str, text: str) -> SimpleNamespace:
    message_id = _command_message_id(command)
    return SimpleNamespace(
        # 队列拿它当「已发出的群消息」记账：标已发送、写 bound_messages
        command_message=SimpleNamespace(id=message_id, reply_to=None, sender_id=None),
        reply_id=message_id + 1, topic_id=getattr(binding, "thread_id", None) or (int(thread_id) if thread_id else None),
        text=text, bot_id=bot_id, bot_username=bot_username,
    )


class _ApiReplyEvent:
    """长得像 Telethon NewMessage 的游戏 bot 回包，路由和执行器只读这些属性。"""

    is_private = False
    out = False
    edit_date = None
    is_reply = True

    def __init__(self, *, chat_id: int, sender_id: Optional[int], command: str, reply) -> None:
        reply_to = SimpleNamespace(
            reply_to_msg_id=reply.command_message.id, reply_to_top_id=reply.topic_id, top_msg_id=reply.topic_id
        )
        self.chat_id = chat_id
        self.id = reply.reply_id
        self.sender_id = reply.bot_id
        self.sender = SimpleNamespace(id=reply.bot_id, username=reply.bot_username, bot=True, first_name="韩天尊")
        self.raw_text = self.text = reply.text
        self.message = SimpleNamespace(id=reply.reply_id, reply_to=reply_to, edit_date=None)
        self._command = SimpleNamespace(id=reply.command_message.id, sender_id=sender_id, raw_text=command, out=True)

    async def get_reply_message(self):
        return self._command

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return None

    async def reply(self, text: str) -> None:
        raise RuntimeError("指令中心回包在群里没有对应消息，不能回复")


async def dispatch_reply(client, storage, command: dict, reply) -> None:
    """队列标完「已发送」、写完指令消息之后调用：把回包当成群消息走一遍路由。"""
    profile_id = int(command.get("profile_id") or 0)
    chat_id = int(command.get("chat_id") or 0)
    if not reply.text:
        return  # 结果不明：指令留在「等回包」，到时被标成待人工确认，跟群里 bot 没回一样

    profile = storage.get_profile(profile_id)
    user_id = str(getattr(profile, "telegram_user_id", "") or "").strip()
    event = _ApiReplyEvent(
        chat_id=chat_id,
        sender_id=int(user_id) if user_id.isdigit() else None,
        command=str(command.get("text") or "").strip(),
        reply=reply,
    )
    try:
        await client._tg_game_router.dispatch(client, event)
    except Exception:  # 指令已经执行过了，这里再抛会被队列当成发送失败
        logger.exception("指令中心回包分发失败 profile=%s command=%s", profile_id, command.get("text"))
    # 路由没认领（比如绑定对不上）也别让它挂成「待人工确认」：回包是接口直接给的，确定执行过
    storage.confirm_outgoing_command_by_reply(profile_id, chat_id, reply.command_message.id, "天机阁指令中心回包")
