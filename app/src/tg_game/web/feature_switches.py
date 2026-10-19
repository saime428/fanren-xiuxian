"""功能勾选页：一个号的全部自动功能列在一页，勾好保存。

保存时不重写任何开关逻辑：逐项读当前状态，跟勾选比，不一样的就以当前登录身份向
原来那个开关接口发同一份表单（排时间、查资源、清状态、境界/宗门/试跑这些前置条件
全由原接口处理），发完再读一次确认，有没开成都列给用户看。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

import biz_small_world_game
from tg_game.features.artifact.biz_artifact_nurture import unpack_artifact_nurture_strategy
from tg_game.features.artifact.biz_artifact_touch_auto import unpack_artifact_touch_strategy
from tg_game.features.artifact.biz_artifact_trial import unpack_artifact_trial_strategy
from tg_game.features.tianxing import biz_tianxing_runtime as tianxing
from tg_game.features.world_boss import world_boss_runtime
from tg_game.services import group_activity
from tg_game.web.admin_global_execution import is_schedule_enabled
from tg_game.web.module_availability import list_artifact_spirits
from tg_game.web.profile_automation_settings import read_state

BOT = "fanrenxiuxian_bot"
ON_OFF = {True: "1", False: "0"}


@dataclass
class Ctx:
    """一个号此刻的状态读取口：主群绑定、闭关/宗门会话行、天机阁缓存。每次读都是新的。"""

    storage: Any
    profile: Any
    chat: Any
    payload: dict

    @property
    def pid(self) -> int:
        return int(self.profile.id)

    @property
    def chat_id(self) -> int:
        return int(self.chat.chat_id) if self.chat else 0

    def base(self) -> dict:
        """各功能页表单里那几个隐藏字段，取值和页面一样。"""
        chat = self.chat
        return {
            "chat_id": str(self.chat_id),
            "thread_id": str(chat.thread_id or "") if chat else "",
            "chat_type": (chat.chat_type if chat else "") or "group",
            "bot_username": (chat.bot_username if chat else "") or BOT,
            "redirect_to": "/features",
        }

    def task(self, key: str) -> dict:
        return self.storage.get_companion_auto_task(self.pid, self.chat_id, key) or {}

    def row(self, table: str) -> dict:
        # 直接查：biz 层的 get_session 会顺手插行，读状态不该有副作用
        try:
            with self.storage.connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE profile_id=? AND chat_id=? AND bot_username=?",
                    (self.pid, self.chat_id, BOT),
                ).fetchone()
        except Exception:
            return {}
        return dict(row) if row else {}

    def state(self, name: str) -> dict:
        return read_state(self.storage, f"{name}:{self.pid}")


@dataclass(frozen=True)
class Switch:
    key: str
    label: str
    group: str
    enabled: Callable[[Ctx], bool]
    # (目标开关, 目标选项, 当前开关, 当前选项) → 依次要发的 (路径, 表单)
    requests: Callable[[Ctx, bool, str, bool, str], list]
    choices: tuple = ()
    choice: Optional[Callable[[Ctx], str]] = None
    blocked: Optional[Callable[[Ctx], str]] = None
    note: str = ""


def _task_on(key):
    return lambda c: bool(c.task(key).get("enabled"))


def _row_on(table, column):
    return lambda c: bool(c.row(table).get(column))


def _state_on(name):
    return lambda c: c.state(name).get("enabled") is True


def _strategy(c: Ctx, key: str) -> str:
    raw = str(c.task(key).get("strategy") or "").strip()
    return "" if raw.startswith("{") else raw


def _toggle(path, form=lambda c, choice: {}):
    """原接口是「点一下翻转」：状态不同点一次；开着只改选项就先关再开。"""
    def build(c, want, want_choice, now, now_choice):
        flips = 2 if (want and now) else 1
        return [(path, {**c.base(), **form(c, want_choice)})] * flips
    return build


def _set(path, form=lambda c, want, choice: {}):
    """原接口是「直接设定」：带上目标开关。"""
    def build(c, want, want_choice, now, now_choice):
        return [(path, form(c, want, want_choice))]
    return build


def _companion(key, form=lambda c, choice: {}):
    return _toggle("/runtime/commands/companion-auto", lambda c, choice: {"feature_key": key, **form(c, choice)})


def _run_time(key):
    return lambda c, choice: ({"run_time": _strategy(c, key)} if _strategy(c, key) else {})


def _admin_managed(kind):
    return lambda c: "管理员页面正在统一托管，这里改不了" if is_schedule_enabled(c.storage, kind) else ""


def _sect(path, column, label):
    return Switch(column, label, "宗门", _row_on("sect_sessions", column),
                  _set(path, lambda c, want, choice: {"chat_id": str(c.chat_id), "enabled": ON_OFF[want]}))


def _setting(feature, extra=lambda c: {}):
    return _set(f"/runtime/automation-settings/{feature}",
                lambda c, want, choice: {"enabled": "1" if want else "", **extra(c)})


def _divine_form(c, choice):
    target, _, rest = _strategy(c, "xinggong_divine").partition("|")
    slots, _, events = rest.partition("|")
    return {"divine_target": target, "divine_slots": slots, "divine_events": [e for e in events.split(",") if e]}


def _artifact_names(c) -> str:
    return "、".join(spirit["name"] for spirit in list_artifact_spirits(c.payload))


def _touch_form(c, choice):
    task = c.task("artifact_touch")
    if not task.get("strategy"):  # 第一次开：用识别到的器灵法宝，间隔用页面默认
        return {"command_text": _artifact_names(c)}
    commands, interval = unpack_artifact_touch_strategy(task["strategy"])
    return {"command_text": "、".join(commands), "interval_minutes": str(interval // 60)}


def _trial_form(c, choice):
    task = c.task("artifact_trial")
    if not task.get("strategy"):
        return {"artifact_name": _artifact_names(c)}
    names, route = unpack_artifact_trial_strategy(task["strategy"])
    return {"artifact_name": "、".join(names), "trial_route": route}


def _nurture_form(c, choice):
    task = c.task("artifact_nurture")
    names = unpack_artifact_nurture_strategy(task["strategy"]) if task.get("strategy") else []
    return {"target_name": "、".join(names) or _artifact_names(c)}


def _small_world_form(c, want, choice):
    config = biz_small_world_game.unpack_auto_strategy(c.task(biz_small_world_game.SMALL_WORLD_AUTO_FEATURE_KEY).get("strategy"))
    flags = ("collect_enabled", "quench_after_collect_enabled", "manifest_enabled", "relief_enabled", "preach_enabled")
    form = {**c.base(), "enabled": ON_OFF[want]}
    if c.task(biz_small_world_game.SMALL_WORLD_AUTO_FEATURE_KEY):
        form.update({flag: ON_OFF[bool(config.get(flag))] for flag in flags})
        form["refresh_interval_minutes"] = str(max(5, int(config.get("refresh_interval_seconds") or 0) // 60))
    return form


def _fishing_form(c, choice):
    session = c.storage.get_fishing_session(c.pid, c.chat_id) or {}
    form = _run_time("fishing_miniapp_daily")(c, choice)
    form.update({key: session[key] for key in ("pond", "bait") if session.get(key)})
    return form


def _heart_task(c) -> dict:
    return c.storage.get_companion_heart_tribulation_task(
        c.pid, c.chat_id, thread_id=c.chat.thread_id if c.chat else None) or {}


def _heart_form(c, choice):
    task = _heart_task(c)
    return {f"heart_round{n}": task.get(f"round{n}_reply") or "稳" for n in (1, 2, 3)}


_TIANXING_SAFE = ("auto_panel_enabled", "auto_observe_enabled", "auto_clear_calamity_enabled", "timeline_enabled")
_TIANXING_RISK = ("auto_set_star_enabled", "auto_predict_enabled", "auto_change_fate_enabled",
                  "retreat_farm_enabled", "deep_retreat_consume_enabled")


def _tianxing(flag, label):
    """天星宗配置按组整份提交：同组其他开关照当前值带上，只改这一个。"""
    def form(c, want, choice):
        config = tianxing.get_profile_record(c.storage, c.pid)["config"]
        group = _TIANXING_SAFE if flag in _TIANXING_SAFE else _TIANXING_RISK
        fields = {name: ON_OFF[bool(config.get(name))] for name in group}
        fields[flag] = ON_OFF[want]
        extra = {"scope": "safe"} if group is _TIANXING_SAFE else {
            "scope": "risk", "set_star_name": str(config.get("set_star_name") or "")}
        return {"profile_id": str(c.pid), "redirect_to": "/features", **extra, **fields}
    return Switch(flag, label, "天星宗",
                  lambda c: bool(tianxing.get_profile_record(c.storage, c.pid)["config"].get(flag)),
                  _set("/modules/tianxing/config", form))


SWITCHES: list[Switch] = [
    # 修炼与群事件
    Switch("retreat", "自动闭关", "修炼与群事件", _row_on("fanren_sessions", "enabled"),
           lambda c, want, choice, now, now_choice: (
               ([("/runtime/cultivation/mode", {"chat_id": str(c.chat_id), "mode": choice})]
                if choice and choice != now_choice else [])
               + ([("/runtime/cultivation/toggle", {"chat_id": str(c.chat_id), "enabled": ON_OFF[want]})]
                  if want != now else [])),
           choices=(("normal", "普通闭关"), ("deep", "深度闭关")),
           choice=lambda c: c.row("fanren_sessions").get("retreat_mode") or "normal"),
    Switch("yuanying", "元婴出窍", "修炼与群事件", _row_on("fanren_sessions", "auto_yuanying_enabled"),
           _set("/runtime/cultivation/yuanying-toggle", lambda c, want, choice: {"chat_id": str(c.chat_id), "enabled": ON_OFF[want]}),
           note="要元婴及以上"),
    Switch("rift", "探寻裂缝", "修炼与群事件", _row_on("fanren_sessions", "auto_rift_enabled"),
           _set("/runtime/cultivation/rift-toggle", lambda c, want, choice: {"chat_id": str(c.chat_id), "enabled": ON_OFF[want]}),
           note="要元婴及以上"),
    Switch("jiyin", "极阴祖师应对", "修炼与群事件", _row_on("fanren_sessions", "auto_jiyin_enabled"),
           _set("/runtime/cultivation/jiyin-toggle",
                lambda c, want, choice: {"chat_id": str(c.chat_id), "enabled": ON_OFF[want], "choice": choice}),
           choices=(("献上魂魄", "献上魂魄"), ("收敛气息", "收敛气息")),
           choice=lambda c: c.row("fanren_sessions").get("auto_jiyin_choice") or "献上魂魄"),
    Switch("nanlong", "南陇侯交易应对", "修炼与群事件", _row_on("fanren_sessions", "auto_nanlong_enabled"),
           _set("/runtime/cultivation/nanlong-toggle",
                lambda c, want, choice: {"chat_id": str(c.chat_id), "enabled": ON_OFF[want], "choice": choice}),
           choices=(("交换 法宝", "选法宝"), ("交换 功法", "选功法"), ("拒绝交易", "拒绝交易")),
           choice=lambda c: c.row("fanren_sessions").get("auto_nanlong_choice") or "交换 法宝"),
    Switch("xuangu", "玄骨考校自动作答", "修炼与群事件", _state_on("xuangu_quiz_auto"), _setting("xuangu-quiz")),
    Switch("xuangu_dungeon", "玄骨遗府自动出招", "修炼与群事件", _state_on("xuangu_dungeon_auto"),
           _setting("xuangu-dungeon"), note="只在本号已进队时出招，不会自己进房"),
    Switch("world_boss", "青元子世界 Boss", "修炼与群事件",
           lambda c: bool((world_boss_runtime.get_task(c.storage, c.pid) or {}).get("enabled")),
           _set("/runtime/world-boss/auto", lambda c, want, choice: {"enabled": ON_OFF[want]})),
    # 侍妾
    Switch("heart_tribulation", "共历心劫", "侍妾", lambda c: bool(_heart_task(c).get("enabled")),
           _companion("heart_tribulation", _heart_form),
           blocked=lambda c: "心劫正在进行，打完再改" if _heart_task(c).get("run_id") else ""),
    Switch("companion_voyage", "侍妾远航", "侍妾", _task_on("companion_voyage"),
           _companion("companion_voyage", lambda c, choice: {"voyage_strategy": choice}),
           choices=(("稳妥", "稳妥"), ("均衡", "均衡"), ("冒险", "冒险"), ("月殿寻痕", "月殿寻痕")),
           choice=lambda c: _strategy(c, "companion_voyage") or "均衡"),
    Switch("companion_replenish", "补齐双侍妾", "侍妾", _task_on("companion_replenish"), _companion("companion_replenish")),
    Switch("dream_seek", "入梦寻图（含拼图）", "侍妾", _task_on("dream_seek"), _companion("dream_seek"),
           note="冷却已过会立刻发一条"),
    Switch("divination_chain", "天机代卜", "侍妾", _task_on("divination_chain"), _companion("divination_chain"),
           note="冷却已过会立刻发一条"),
    Switch("dual_cultivation", "温养双修", "侍妾", _task_on("dual_cultivation"), _companion("dual_cultivation")),
    # 每日小程序
    Switch("fishing_miniapp_daily", "灵溪垂钓", "每日小程序", _task_on("fishing_miniapp_daily"),
           _toggle("/runtime/fishing/miniapp-daily-auto", _fishing_form), note="要先在垂钓页试钓通过"),
    Switch("fish_open", "钓完自动开鱼", "每日小程序", _state_on("fishing_auto_open"), _setting("fish-open")),
    Switch("estate_miniapp_hunt_daily", "洞府寻宝", "每日小程序", _task_on("estate_miniapp_hunt_daily"),
           _toggle("/runtime/estate/miniapp-hunt-daily-auto", _run_time("estate_miniapp_hunt_daily")),
           blocked=_admin_managed("estate")),
    Switch("pagoda_tower", "闯塔", "每日小程序", _task_on("pagoda_tower"),
           _toggle("/runtime/commands/pagoda-auto", _run_time("pagoda_tower")), blocked=_admin_managed("pagoda")),
    Switch("tianji_trial_daily", "天机试炼", "每日小程序", _task_on("tianji_trial_daily"),
           _toggle("/runtime/tianji-trial/daily-auto", _run_time("tianji_trial_daily")), blocked=_admin_managed("tianji")),
    Switch("beast_merge_daily", "灵兽合成", "每日小程序", _task_on("beast_merge_daily"),
           _toggle("/runtime/beast-merge/daily-auto", _run_time("beast_merge_daily")), blocked=_admin_managed("beast_merge")),
    Switch("fate_cards", "天机命脉 + 噬金虫", "每日小程序", _state_on("fate_cards"),
           _setting("fate-cards", lambda c: {"choice": c.state("fate_cards").get("choice") or "hide",
                                             "question": c.state("fate_cards").get("question") or "cultivation"})),
    Switch("wild_join", "参加野外历练", "每日小程序",
           lambda c: c.storage.get_runtime_state(f"wild_experience_disabled:{c.pid}") != "1", _setting("wild-join"),
           note="管理员页面统一安排场次，这里只管这个号参不参加"),
    Switch("wild_report", "野外历练日报", "每日小程序", _state_on("wild_experience_report"), _setting("wild-report")),
    Switch("luoyun_spirit_tree_daily", "落云灵眼赛", "每日小程序", _task_on("luoyun_spirit_tree_daily"),
           _toggle("/runtime/sect/luoyun-spirit-tree-daily-auto"), note="落云宗，且先试跑通过"),
    # 宗门
    Switch("mulan_support_plan", "慕兰支援", "宗门", _task_on("mulan_support_plan"),
           _set("/runtime/mulan/auto-support-plan", lambda c, want, choice: {
               **c.base(), "enabled": ON_OFF[want], "support_command": _strategy(c, "mulan_support_plan") or "auto"})),
    _sect("/runtime/sect/checkin-toggle", "auto_sect_checkin_enabled", "宗门点卯"),
    _sect("/runtime/sect/teach-toggle", "auto_sect_teach_enabled", "传功"),
    _sect("/runtime/sect/companion-greet-toggle", "auto_companion_greet_enabled", "问安"),
    _sect("/runtime/sect/companion-assist-toggle", "auto_companion_assist_enabled", "助阵"),
    _sect("/runtime/sect/lingxiao-toggle", "auto_lingxiao_enabled", "灵霄登阶"),
    _sect("/runtime/sect/lingxiao-gangfeng-toggle", "auto_lingxiao_gangfeng_enabled", "灵霄罡风"),
    _sect("/runtime/sect/lingxiao-borrow-toggle", "auto_lingxiao_borrow_enabled", "灵霄借宝"),
    _sect("/runtime/sect/lingxiao-question-toggle", "auto_lingxiao_question_enabled", "灵霄问答"),
    _sect("/runtime/sect/yuanying-wendao-toggle", "auto_yuanying_wendao_enabled", "元婴问道"),
    _sect("/runtime/sect/yuanying-retreat-toggle", "auto_yuanying_retreat_enabled", "元婴闭关"),
    _sect("/runtime/sect/yinluo-sacrifice-toggle", "auto_yinluo_sacrifice_enabled", "阴罗幡献祭"),
    _sect("/runtime/sect/yinluo-blood-wash-toggle", "auto_yinluo_blood_wash_enabled", "阴罗幡血洗"),
    _sect("/runtime/sect/yinluo-refine-toggle", "auto_yinluo_refine_enabled", "阴罗幡炼魂"),
    Switch("auto_huangfeng_enabled", "黄枫谷", "宗门", _row_on("sect_sessions", "auto_huangfeng_enabled"),
           _set("/runtime/sect/huangfeng-auto", lambda c, want, choice: {
               "chat_id": str(c.chat_id), "enabled": ON_OFF[want],
               "seed_name": str(c.row("sect_sessions").get("huangfeng_seed_name") or ""),
               "exchange_enabled": ON_OFF[bool(c.row("sect_sessions").get("auto_huangfeng_exchange_enabled"))]})),
    Switch("xinggong_divine", "星宫观星改命", "宗门", _task_on("xinggong_divine"),
           _companion("xinggong_divine", _divine_form), note="星宫；目标和时段沿用观星页的设置"),
    Switch("xinggong_starboard", "星辰采集", "宗门", _task_on("xinggong_starboard"),
           _toggle("/runtime/sect/xinggong-starboard-auto", lambda c, choice: {"target_star": choice}),
           choices=(("天雷星", "天雷星"), ("庚金星", "庚金星")),
           choice=lambda c: _strategy(c, "xinggong_starboard") or "庚金星", note="星宫"),
    Switch("wanling_roam", "万灵一键放养", "宗门", _task_on("wanling_roam"), _toggle("/runtime/sect/wanling-roam-auto")),
    # 法宝与元神
    Switch("artifact_touch", "抚摸法宝", "法宝与元神", _task_on("artifact_touch"),
           _toggle("/runtime/artifact/touch-auto", _touch_form), note="第一次开用识别到的器灵法宝"),
    Switch("artifact_trial", "器灵试炼", "法宝与元神", _task_on("artifact_trial"),
           _toggle("/runtime/artifact/trial-auto", _trial_form), note="要灵石和养魂木"),
    Switch("artifact_nurture", "温养器灵", "法宝与元神", _task_on("artifact_nurture"),
           _toggle("/runtime/artifact/nurture-auto", _nurture_form), note="要灵石和养魂木"),
    Switch("soul_cultivation", "第二元神修炼", "法宝与元神", _task_on("soul_cultivation"), _companion("soul_cultivation")),
    Switch("sword_formation", "大庚剑阵", "法宝与元神", _task_on("sword_formation"), _companion("sword_formation")),
    Switch("vase_condense_daily", "掌天瓶凝液", "法宝与元神", _task_on("vase_condense_daily"),
           _companion("vase_condense_daily", lambda c, choice: {"vase_followup": _strategy(c, "vase_condense_daily")})),
    Switch("small_world_auto", "小世界", "法宝与元神", _task_on(biz_small_world_game.SMALL_WORLD_AUTO_FEATURE_KEY),
           _set("/runtime/small-world/auto", _small_world_form), note="要化神"),
    Switch("small_world_preach_auto", "神迹布道", "法宝与元神", _task_on("small_world_preach_auto"),
           _toggle("/runtime/small-world/preach-auto")),
    Switch("taiyi_yindao", "太一引道", "法宝与元神", _task_on("taiyi_yindao"),
           _companion("taiyi_yindao", lambda c, choice: {"element": _strategy(c, "taiyi_yindao")})),
    Switch("xutian_cauldron", "自动虚天鼎", "法宝与元神", _task_on("xutian_cauldron"), _companion("xutian_cauldron")),
    # 天星宗
    _tianxing("auto_panel_enabled", "自动查盘"),
    _tianxing("auto_observe_enabled", "自动观命"),
    _tianxing("auto_clear_calamity_enabled", "自动消劫"),
    _tianxing("timeline_enabled", "时间线调度"),
    _tianxing("auto_set_star_enabled", "自动定命"),
    _tianxing("auto_predict_enabled", "自动推命"),
    _tianxing("auto_change_fate_enabled", "自动改命"),
    _tianxing("retreat_farm_enabled", "普通闭关前推命"),
    # 其他
    Switch("group_activity", "群聊活跃发言", "其他",
           lambda c: bool((group_activity.get_task(c.storage, c.pid) or {}).get("enabled")),
           lambda c, want, choice, now, now_choice: [("/runtime/group-activity/toggle", {})]),
    Switch("command_center", "指令走洞府接口", "其他", _state_on("command_center"),
           _setting("command-center", lambda c: {"writes": "1" if c.state("command_center").get("writes") else ""})),
]
GROUPS = list(dict.fromkeys(switch.group for switch in SWITCHES))


def view(ctx: Ctx) -> list[dict]:
    """勾选页每一行：当前开关、当前选项、改不了的原因。"""
    rows = []
    for switch in SWITCHES:
        rows.append({
            "key": switch.key, "label": switch.label, "group": switch.group, "note": switch.note,
            "enabled": switch.enabled(ctx), "choices": switch.choices,
            "choice": switch.choice(ctx) if switch.choice else "",
            "blocked": switch.blocked(ctx) if switch.blocked else "",
        })
    return rows


def _response_error(response) -> str:
    if response.status_code >= 400:
        try:
            return str(response.json().get("detail") or response.status_code)
        except ValueError:
            return f"HTTP {response.status_code}"
    query = parse_qs(urlsplit(response.headers.get("location", "")).query)
    return (query.get("error") or query.get("settings_error") or [""])[0]


def parse_form(form) -> dict:
    """页面提交 → {key: (开不开, 选项)}；只含页面上能改的（present）那些，改不了的不碰。"""
    on = set(form.getlist("on"))
    return {key: (key in on, str(form.get(f"choice_{key}") or "")) for key in form.getlist("present")}


async def apply(app, cookies: dict, make_ctx: Callable[[], Ctx], wanted: dict) -> list[dict]:
    """wanted = {key: (开不开, 选项)}，不在里面的功能不动。返回每个动过的功能的结果。"""
    results = []
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://feature-switches", cookies=cookies) as client:
        for switch in SWITCHES:
            if switch.key not in wanted:
                continue
            ctx = make_ctx()
            now = switch.enabled(ctx)
            now_choice = switch.choice(ctx) if switch.choice else ""
            want, want_choice = wanted[switch.key]
            want_choice = want_choice or now_choice
            if want == now and (not want or want_choice == now_choice):
                continue
            reason = switch.blocked(ctx) if switch.blocked else ""
            if not reason:
                for path, form in switch.requests(ctx, want, want_choice, now, now_choice):
                    try:
                        reason = _response_error(await client.post(path, data=form, follow_redirects=False))
                    except Exception as exc:  # noqa: BLE001 - 报给页面，不中断其余功能
                        reason = f"{type(exc).__name__}: {exc}"
                    if reason:
                        break
            after = make_ctx()
            done = switch.enabled(after) == want and (
                not want or not switch.choice or switch.choice(after) == want_choice)
            if not done and not reason:
                reason = str(after.task(switch.key).get("last_error") or "原接口没有改动，可能是前置条件不满足")
            results.append({"label": switch.label, "ok": done, "enabled": want,
                            "detail": ("已开启" if want else "已关闭") if done else reason})
    return results
