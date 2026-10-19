"""Account-scoped controls for existing runtime automations."""
import json
import math
import re

from tg_game.features import biz_ldc_red_packet as ldc
from tg_game.features import biz_xuangu_dungeon as xuangu_dungeon
from tg_game.features import biz_xuangu_quiz as xuangu
from tg_game.features.estate import biz_command_center as command_center
from tg_game.features.stock import biz_stock_miniapp as stock
from tg_game.features.stock import biz_stock_tick_trader as tick


FEATURE_PAGES = {"fate-cards": "other", "ldc-red-packet": "other", "wild-report": "other", "fish-open": "other",
                 "xuangu-quiz": "other", "xuangu-dungeon": "other", "wild-join": "other", "rebirth-roots": "other", "command-center": "other",
                 "stock-schedule": "stock", "stock-tick": "stock"}
CHOICES = {"hide": "藏锋避劫", "accept": "顺势承命", "defy": "逆势改命"}


def read_state(storage, key):
    try:
        value = json.loads(storage.get_runtime_state(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value, label, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须为有效数字。") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label}须在 {minimum:g}–{maximum:g} 之间。")
    return number


def question_options(state):
    current = str(state.get("question") or "cultivation")
    options = {"cultivation": "修行"}
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    for item in last.get("questions") or []:
        if isinstance(item, dict) and item.get("key"):
            options[str(item["key"])] = str(item.get("name") or item["key"])
    options.setdefault(current, f"当前主题（{current}）")
    return options


def save_settings(storage, profile_id, feature, form):
    enabled = form.get("enabled") == "1"
    if feature == "stock-schedule":
        minutes = _number(form.get("interval_minutes", "30"), "分析间隔（分钟）", 1, 1440)
        storage.set_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=profile_id), str(minutes * 60) if enabled else "0")
        return
    if feature == "wild-join":
        storage.set_runtime_state(f"wild_experience_disabled:{profile_id}", "0" if enabled else "1")
        return
    if feature == "rebirth-roots":
        # profile_rebirth 按字比：候选的灵根里含任一个字就优先，所以「灵」「根」会命中所有候选
        roots = "".join(dict.fromkeys(re.sub(r"[\s,，、/]+", "", str(form.get("roots") or ""))))
        if len(roots) > 10 or "灵" in roots or "根" in roots:
            raise ValueError("偏好灵根只填字，比如「冰风」或「金木」，最多 10 个字，不带「灵根」。")
        storage.set_runtime_state(f"rebirth_preferred_roots:{profile_id}", roots)
        return
    key = {"fate-cards": f"fate_cards:{profile_id}", "ldc-red-packet": ldc.SWITCH_KEY.format(profile_id),
           "wild-report": f"wild_experience_report:{profile_id}",
           "fish-open": f"fishing_auto_open:{profile_id}", "xuangu-quiz": xuangu.AUTO_SWITCH_KEY.format(profile_id),
           "xuangu-dungeon": xuangu_dungeon.AUTO_SWITCH_KEY.format(profile_id),
           "command-center": command_center.SWITCH_KEY.format(profile_id),
           "stock-tick": tick.CONFIG_STATE_KEY.format(profile_id=profile_id)}[feature]
    fields = {"enabled": enabled}
    expected = None
    if feature == "fate-cards":
        choice = str(form.get("choice") or "hide")
        question = str(form.get("question") or "cultivation")
        if choice not in CHOICES or question not in question_options(read_state(storage, key)):
            raise ValueError("请选择页面提供的命择和问天主题。")
        fields.update(choice=choice, question=question)
    elif feature == "command-center":
        fields["writes"] = form.get("writes") == "1"
    elif feature == "ldc-red-packet":
        mode = str(form.get("grab_mode") or read_state(storage, key).get("grab_mode") or "after_first")
        if mode not in ldc.GRAB_MODES:
            raise ValueError("请选择有效的红包领取方式。")
        minimum = _number(form.get("min_total", "200"), "红包总额门槛", 0, 1e12)
        low = _number(form.get("delay_low", "1"), "最短等待（秒）", 1, 300)
        high = _number(form.get("delay_high", "3"), "最长等待（秒）", 1, 300)
        if low > high:
            raise ValueError("最短等待不能大于最长等待。")
        fields.update(min_total=minimum, delay=[low, high], grab_mode=mode)
        if enabled:
            # A stale enabled checkbox must not undo a newer automatic brake.
            brake = form.get("braked_at")
            expected = {"braked_at": float(brake) if brake else None}
    elif feature == "stock-tick":
        amount = _number(form.get("amount", "10000"), "单笔金额（灵石）", 1, 1e9)
        signal = _number(form.get("signal_pct", tick.SIGNAL_PCT), "买入门槛（%）", tick.MIN_SIGNAL_PCT, 10)
        fields.update(amount=int(amount), signal_pct=signal)
        if enabled:
            fields["stopped"] = ""
            if form.get("was_enabled") == "1":
                # 页面打开时是开着的、提交前被连亏/日亏自动关了：旧勾选不能把它重新打开
                expected = {"enabled": True}
    storage.update_runtime_state_fields(key, fields, expected_fields=expected)


def build_view(storage, profile_id):
    fate = read_state(storage, f"fate_cards:{profile_id}")
    packet = read_state(storage, ldc.SWITCH_KEY.format(profile_id))
    wild = read_state(storage, f"wild_experience_report:{profile_id}")
    minimum, low, high = ldc._knobs(packet)
    try:
        seconds = float(storage.get_runtime_state(stock.SCHEDULE_STATE_KEY.format(profile_id=profile_id)) or 0)
    except ValueError:
        seconds = 0
    enabled = math.isfinite(seconds) and seconds > 0
    labels = {"settled": "今日已结算", "waiting": "等待验命", "expired": "今日已过期", "gave_up": "今日重试已停止", "failed": "等待重试"}
    return {
        "fate": {**fate, "choice": fate.get("choice") or "hide", "question": fate.get("question") or "cultivation",
                 "choices": CHOICES, "questions": question_options(fate), "status_label": labels.get(fate.get("status"), "尚无结果")},
        "ldc": {**packet, "min_total": minimum, "delay_low": low, "delay_high": high,
                "grab_mode": packet.get("grab_mode") or "after_first"},
        "wild": wild,
        "fish_open": read_state(storage, f"fishing_auto_open:{profile_id}"),
        "command_center": read_state(storage, command_center.SWITCH_KEY.format(profile_id)),
        "xuangu": read_state(storage, xuangu.AUTO_SWITCH_KEY.format(profile_id)),
        "xuangu_dungeon": read_state(storage, xuangu_dungeon.AUTO_SWITCH_KEY.format(profile_id)),
        "wild_join": storage.get_runtime_state(f"wild_experience_disabled:{profile_id}") != "1",
        "rebirth_roots": storage.get_runtime_state(f"rebirth_preferred_roots:{profile_id}") or "",
        "stock_tick": {**read_state(storage, tick.CONFIG_STATE_KEY.format(profile_id=profile_id)),
                       **tick.load_config(storage, profile_id)},
        "stock": {"enabled": enabled, "interval_minutes": seconds / 60 if enabled else 30,
                  "last_run": storage.get_runtime_state(stock.LAST_RUN_STATE_KEY.format(profile_id=profile_id)) or 0},
    }
