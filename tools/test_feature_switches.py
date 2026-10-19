"""功能勾选页：新号的页面能打开；保存时只切换和勾选不一样的功能，走的是原开关接口；托管中的不碰。

run: PYTHONPATH=app/src python tools/test_feature_switches.py
"""
import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.config import get_settings  # noqa: E402
from tg_game.storage import ASC_EXTERNAL_PROVIDER, CompatDb, Storage  # noqa: E402
from tg_game.web import feature_switches  # noqa: E402
import biz_fanren_game  # noqa: E402
import biz_sect_game  # noqa: E402

CHAT = -100000000042


def setup(folder: Path):
    storage = Storage(folder / "t.db")
    storage.init_schema()
    db = CompatDb(storage)
    try:
        biz_fanren_game.ensure_tables(db)
        biz_sect_game.ensure_tables(db)
    finally:
        db.close()
    profile = storage.create_profile("新号")
    storage.bind_profile_telegram_account(profile.id, telegram_user_id="123", telegram_username="newbie")
    storage.create_chat_binding(profile.id, CHAT, thread_id=42, bot_username="fanrenxiuxian_bot")
    storage.upsert_external_account(profile.id, ASC_EXTERNAL_PROVIDER, "123", "newbie", "connected",
                                    "session=offline", {}, "")
    return storage, storage.get_profile(profile.id)


def results(response):
    """只看「保存结果」那一段（整页本来就列着所有功能名）。"""
    assert response.status_code == 200, response.text[:500]
    return response.text.split("保存结果", 1)[1].split("<form", 1)[0]


def form(on, present, **choices):
    return {"present": list(present), "on": list(on), **{f"choice_{key}": value for key, value in choices.items()}}


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        storage, profile = setup(Path(tmp))
        web = importlib.import_module("tg_game.web.app")
        token = storage.create_app_session(profile.id)
        managed = {"estate"}
        with patch.multiple(get_settings(), database_path=storage.path, authorized_user_id="123", bound_chat_id=None), \
                patch.object(feature_switches, "is_schedule_enabled", lambda _storage, kind: kind in managed):
            app = web.create_app()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                         cookies={"tg_game_app_session": token}) as client:
                page = await client.get("/features?new=1")
                assert page.status_code == 200, page.status_code
                for text in ("功能勾选", "新号已添加", "共历心劫", "南陇侯交易应对", "自动改命", "管理员页面正在统一托管",
                             "玄骨遗府自动出招"):
                    assert text in page.text, text
                assert 'name="present" value="estate_miniapp_hunt_daily"' not in page.text  # 托管中的不提交

                saved = await client.post("/features", data=form(
                    ["xuangu", "companion_replenish", "companion_voyage", "jiyin", "estate_miniapp_hunt_daily"],
                    ["xuangu", "companion_replenish", "companion_voyage", "jiyin", "group_activity"],
                    companion_voyage="稳妥", jiyin="收敛气息"))
                assert json.loads(storage.get_runtime_state(f"xuangu_quiz_auto:{profile.id}"))["enabled"] is True
                replenish = storage.get_companion_auto_task(profile.id, CHAT, "companion_replenish")
                assert replenish and replenish["enabled"], replenish  # 翻转型只点了一次
                voyage = storage.get_companion_auto_task(profile.id, CHAT, "companion_voyage")
                assert voyage["enabled"] and voyage["strategy"] == "稳妥", voyage
                with storage.connect() as conn:
                    row = conn.execute("SELECT auto_jiyin_enabled, auto_jiyin_choice FROM fanren_sessions "
                                       "WHERE profile_id=? AND chat_id=?", (profile.id, CHAT)).fetchone()
                assert tuple(row) == (1, "收敛气息"), tuple(row)
                assert storage.get_companion_auto_task(profile.id, CHAT, "estate_miniapp_hunt_daily") is None
                assert "已开启" in results(saved) and "群聊活跃发言" not in results(saved)  # 没变的不列

                # 开着只改选项：先关再开，换成新路线
                changed = await client.post("/features", data=form(
                    ["xuangu", "companion_replenish", "companion_voyage"],
                    ["xuangu", "companion_replenish", "companion_voyage"], companion_voyage="冒险"))
                voyage = storage.get_companion_auto_task(profile.id, CHAT, "companion_voyage")
                assert voyage["enabled"] and voyage["strategy"] == "冒险", voyage
                assert results(changed).count("已开启") == 1, results(changed)  # 只有远航动了

                unchanged = await client.post("/features", data=form(
                    ["xuangu", "companion_replenish", "companion_voyage"],
                    ["xuangu", "companion_replenish", "companion_voyage"], companion_voyage="冒险"))
                assert "没有需要改的功能" in results(unchanged)

                dungeon = await client.post("/features", data=form(["xuangu", "companion_replenish", "xuangu_dungeon"],
                                                                   ["xuangu", "companion_replenish", "xuangu_dungeon"]))
                assert json.loads(storage.get_runtime_state(f"xuangu_dungeon_auto:{profile.id}"))["enabled"] is True
                assert results(dungeon).count("已开启") == 1, results(dungeon)

                off = await client.post("/features", data=form([], ["xuangu", "companion_replenish"]))
                assert json.loads(storage.get_runtime_state(f"xuangu_quiz_auto:{profile.id}"))["enabled"] is False
                assert not storage.get_companion_auto_task(profile.id, CHAT, "companion_replenish")["enabled"]
                assert results(off).count("已关闭") == 2, results(off)
                assert storage.get_companion_auto_task(profile.id, CHAT, "companion_voyage")["enabled"]  # 没提交的不动
    print("feature switches: ok")


if __name__ == "__main__":
    asyncio.run(main())
