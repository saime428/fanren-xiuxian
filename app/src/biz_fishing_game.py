import json
import re

FISHING_DEFAULT_POND = "青溪浅滩"
FISHING_DEFAULT_BAIT = "凡饵"
FISHING_POND_OPTIONS = [
    {"name": "青溪浅滩", "description": "门槛无，推荐凡饵"},
    {"name": "灵眼寒潭", "description": "钓术熟练度 800，推荐灵虫饵"},
    {"name": "乱星海礁", "description": "钓术熟练度 2400，推荐妖血饵"},
]
FISHING_BAIT_OPTIONS = [
    {"name": "凡饵", "description": "灵石 x12，入门鱼饵"},
    {"name": "灵米饵", "description": "灵石 x35，标准鱼饵"},
    {"name": "灵虫饵", "description": "灵石 x90，凝血草 x2"},
    {"name": "妖血饵", "description": "灵石 x220，一阶妖丹 x1"},
    {"name": "月华饵", "description": "灵石 x650，二级妖丹 x1"},
]
# 缺饵时从哪儿补：制饵走群命令 .制饵（花鱼材），买饵走小程序接口（花灵石）
FISHING_BAIT_SOURCE_OPTIONS = [
    {"name": "craft", "label": "制饵优先", "description": "先 .制饵，鱼材不够再买"},
    {"name": "craft_only", "label": "只制饵", "description": "只 .制饵，绝不花灵石"},
    {"name": "buy", "label": "只买饵", "description": "直接用灵石买"},
]
FISHING_DEFAULT_BAIT_SOURCE = "craft"


def normalize_bait_source(value: object) -> str:
    text = str(value or "").strip()
    return text if any(o["name"] == text for o in FISHING_BAIT_SOURCE_OPTIONS) else FISHING_DEFAULT_BAIT_SOURCE


FISHING_DAILY_LIMIT = 20
FISHING_RESULT_RECOVERY_SECONDS = 3


def fishing_rod_from_inventory(payload=None):
    inventory = payload.get('inventory') if isinstance(payload, dict) else {}
    if isinstance(inventory, str) and inventory.strip():
        try:
            parsed = json.loads(inventory)
        except Exception:
            parsed = {}
        inventory = parsed if isinstance(parsed, dict) else {}
    if not isinstance(inventory, dict):
        inventory = {}
    items = inventory.get('items')
    if not isinstance(items, list):
        items = []
    needle = chr(0x9493) + chr(0x7aff)
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        item_id = str(item.get('item_id') or item.get('id') or '')
        if needle not in name and not item_id.startswith('item_fishing_rod'):
            continue
        match = re.search(r'每日可垂钓\s*(\d+)\s*竿', str(item.get('description') or ''))
        daily_limit = int(match.group(1)) if match else None
        rod_text = ('%s（每日 %s 竿）' % (name, daily_limit)) if name and daily_limit else name
        return {'name': name, 'daily_limit': daily_limit, 'rod_text': rod_text, 'item_id': item_id}
    return {'name': '', 'daily_limit': None, 'rod_text': '', 'item_id': ''}
