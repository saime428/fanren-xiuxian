import re

from tg_game.features.artifact.biz_artifact_targets import split_artifact_targets
from tg_game.web.biz_web_display_formatting import coerce_json_list

SMALL_WORLD_MIN_MAJOR_STAGE = "化神"
MAJOR_STAGE_ORDER = (
    "练气",
    "筑基",
    "结丹",
    "元婴",
    "化神",
    "半步炼虚",
    "炼虚",
    "合体",
    "大乘",
    "渡劫",
)


def major_stage_rank(stage_name: str) -> int:
    normalized = str(stage_name or "").strip()
    for index, major_stage in enumerate(MAJOR_STAGE_ORDER):
        if major_stage in normalized:
            return index
    return -1


def is_small_world_module_available(active_profile) -> bool:
    if not active_profile:
        return False
    return major_stage_rank(getattr(active_profile, "stage_name", "")) >= major_stage_rank(
        SMALL_WORLD_MIN_MAJOR_STAGE
    )


def is_yuanying_stage(active_profile) -> bool:
    if not active_profile:
        return False
    return major_stage_rank(getattr(active_profile, "stage_name", "")) >= major_stage_rank(
        "元婴"
    )


def payload_has_artifact_spirit(payload) -> bool:
    return bool(list_artifact_spirits(payload))


def list_artifact_spirits(payload) -> list[dict]:
    """天机阁资料里带器灵的法宝：[{name, spirit, level}]，按背包顺序、同名只留一件。"""
    if not isinstance(payload, dict):
        return []
    inventory = payload.get("inventory") or {}
    items = inventory.get("items") if isinstance(inventory, dict) else []
    spirits: dict[str, dict] = {}
    for item in coerce_json_list(items):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        spirit = item.get("spirit")
        if isinstance(spirit, dict) and spirit:
            spirit_name, level = str(spirit.get("name") or "").strip(), spirit.get("level")
        elif isinstance(spirit, str) and spirit.strip():
            spirit_name, level = spirit.strip(), None
        else:
            continue
        name = str(item["name"]).strip()
        spirits.setdefault(name, {"name": name, "spirit": spirit_name or "器灵", "level": level})
    return list(spirits.values())


def split_artifact_choice(text, option_names) -> tuple[list[str], str]:
    """配置里的法宝 → (勾选框里有的, 其余用「、」连起来给手填框)。

    抚摸存的是整条「.抚摸法宝 甲」，温养也可能是指令，先去掉指令前缀再比名字。
    """
    names = [
        re.sub(r"^\.(?:抚摸法宝|温养器灵)\s*", "", target)
        for target in split_artifact_targets(text)
    ]
    names = [name for name in names if name]
    picked = [name for name in names if name in option_names]
    return picked, "、".join(name for name in names if name not in option_names)
