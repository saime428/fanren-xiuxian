"""退宗识别自检：天机阁「散修」和机器人「散修无需点卯」都要关掉全部宗门自动任务。

运行：uv run --with-requirements requirements.txt python -X utf8 -B tools/test_sect_no_sect_guard.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
import biz_sect_game as g
from tg_game.features.sect.biz_sect_view_model import has_joined_sect

session = {"chat_id": -100, "profile_id": 3, "enabled": 1, "auto_sect_checkin_enabled": 1, "next_check_time": 0}

# 10-10 实况：天机阁 payload 的 sect_name 是「散修」，旧代码当成有宗门，点卯照发。
for name in ("散修", "【散修】", "未入宗门", ""):
    updates = g._build_sect_auto_guard_updates(session, name, now=1_000_000)
    assert updates["auto_sect_checkin_enabled"] == 0, name
    assert "人物已无宗门" in updates["next_check_source"], name
assert g._build_sect_auto_guard_updates(session, "落云宗", now=1_000_000).get("auto_sect_checkin_enabled") is None
assert not g._is_same_sect_name("散修", "散修")
assert not has_joined_sect(SimpleNamespace(sect_name="散修"))
assert has_joined_sect(SimpleNamespace(sect_name="【落云宗】"))

parsed = g.parse_message("散修无需点卯，速速寻一宗门拜入吧。")
assert parsed["event"] == "sect_no_sect", parsed
assert g.parse_message("点卯成功！你获得了 100 点宗门贡献。\n你已连续点卯 37 天。")["event"] == "sect_sign"

# 10-17 起星宫问安的成功回包；以前认不出，3 分钟后再问一次，每天多发一条（群里回「今日已经问安过了」）
assert g.parse_message("侍妾向你微微颔首【凌玉灵】，你们的情缘增加了 30 点。")["event"] == "companion_greet_success"
assert g.parse_message("今日已经问安过了，请勿过多打扰。你的心意她已收到。")["event"] == "companion_greet_already_done"
assert g.parse_message("【乱星海远航·归】\n此行顺遂，侍妾对你更添信重，情缘增加 9 点。")["event"] != "companion_greet_success"
print("ok")
