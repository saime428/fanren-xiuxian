"""MiniApp 垂钓流程自检（洞府入口 → 老垂钓接口 /api/miniapp/xianxia-fishing/*）。

假服务器的形状按 10-14 的老页面源码和当晚实测回包；遛鱼题目取自前端 dwelling-fishing-preview.js。
洞府钓位回退 10-16 删了，文件名是历史遗留。
"""
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))

from tg_game.features.fishing import biz_fishing_miniapp as fishing

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _run(transport, *, max_rounds=3, auto_buy_bait=True, bait="凡饵"):
    sleeps = []
    result = fishing.run_fishing_miniapp_public_flow(
        estate_token="df_public_test",
        init_data="offline-init",
        pond="青溪浅滩",
        bait=bait,
        max_rounds=max_rounds,
        auto_buy_bait=auto_buy_bait,
        transport=transport,
        sleeper=sleeps.append,
    )
    return result, sleeps


def test_urllib_transport_keeps_http_error_body():
    # 根因自检：urlopen 对 4xx 直接抛异常，正文（错误码）会丢，只剩「HTTP Error 409: Conflict」
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            # 先读完请求体再回包，否则 Windows 上会在客户端还在发正文时 RST（WinError 10053）
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"ok": False, "error": "fishing_companion_required"}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = {"method": "POST", "url": f"http://127.0.0.1:{server.server_port}/x", "payload": {}}
        result = fishing.execute_fishing_miniapp_request(request, fishing._urllib_transport)
    finally:
        server.shutdown()
    assert result["status_code"] == 409 and result["error"] == "fishing_companion_required", result


def test_bait_source_switch():
    import biz_fishing_game

    assert biz_fishing_game.normalize_bait_source("craft_only") == "craft_only"
    for bogus in ("", None, "buy_all", "制饵"):
        assert biz_fishing_game.normalize_bait_source(bogus) == "craft", bogus

    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped bait-source truth table: {type(exc).__name__})")
        return
    buy = executors._fishing_should_buy_bait
    # craft：先制饵，制过一次还没饵才买兜底
    assert buy("craft", crafted_today=False) is False
    assert buy("craft", crafted_today=True) is True
    # craft_only：一分灵石都不花
    assert buy("craft_only", crafted_today=False) is False
    assert buy("craft_only", crafted_today=True) is False
    # buy：直接买，不发群命令
    assert buy("buy", crafted_today=False) is True
    assert buy("buy", crafted_today=True) is True


def test_executor_glue_crafts_once_then_falls_back_to_buying():
    """调度器那段胶水：排 .制饵、当天只排一次、保持武装。"""
    try:
        from tg_game.runtime import executors
    except Exception as exc:  # 本地缺 pydantic/telethon 时跳过；VPS 真 venv 会真跑
        print(f"  (skipped executor glue: {type(exc).__name__})")
        return

    class FakeStorage:
        def __init__(self):
            self.state, self.queued, self.latest = {}, [], None

        def get_external_account(self, profile_id, provider):
            return {}

        def get_game_items(self):
            return {}

        def get_runtime_state(self, key):
            return self.state.get(key)

        def set_runtime_state(self, key, value):
            self.state[key] = value

        def get_latest_outgoing_command(self, chat_id, profile_id=None, text="", thread_id=None):
            return self.latest

        def enqueue_outgoing_command(self, **kwargs):
            self.queued.append(kwargs)
            return len(self.queued)

    storage, now = FakeStorage(), time.time()
    session = {"bait": "灵虫饵", "chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    result = {"status": "need_bait", "data": {"baitName": "灵虫饵", "baitQuantity": 15}}
    kw = dict(profile_id=2, chat_id=-100123, thread_id=1000003, session=session, batch_mode=True, now=now)

    assert executors._fishing_bait_crafted_today(storage, profile_id=2, chat_id=-100123, now=now) is False
    updates = executors._request_fishing_bait_craft(storage, result, **kw)
    assert storage.queued[0]["text"] == ".制饵 灵虫饵 15", storage.queued
    assert updates["enabled"] is True and updates["state"] == "miniapp_batch", updates
    assert updates["next_action_at"] > now, updates
    # 制过了 -> 下一轮允许买饵兜底，不会每 90 秒一直制
    assert executors._fishing_bait_crafted_today(storage, profile_id=2, chat_id=-100123, now=now) is True
    # 同一条命令还在队列里就不重复排
    storage.latest = {"status": "pending", "updated_at": now}
    executors._request_fishing_bait_craft(storage, result, **kw)
    assert len(storage.queued) == 1, storage.queued
    # 饵名不可信时不发命令
    storage.latest = None
    bad = executors._request_fishing_bait_craft(storage, {"status": "need_bait", "data": {"baitName": "凡饵 .离开洞府"}}, **kw)
    assert len(storage.queued) == 1 and bad["enabled"] is False, (storage.queued, bad)


def _preview_fight(difficulty, behavior, rare, seed):
    """形状和数值取自前端 dwelling-fishing-preview.js（本地预览的假服务器）。"""
    duration = {"steady": 900, "leap": 760, "surge": 1200}[behavior]
    struggles = [{"startMs": 3200 + (seed % 5) * 180, "durationMs": duration, "strength": 0.85}]
    if rare:
        struggles.append({"startMs": 8200 + (seed % 7) * 170, "durationMs": duration, "strength": 0.95})
    return {
        "challengeId": f"c{seed}", "mode": "xianxiaFishingV2", "targetLow": 43 - difficulty,
        "targetHigh": 73 - difficulty * 3, "fishPower": 1.7 + difficulty * 0.55, "fishSeed": f"seed-{seed:04x}",
        "difficulty": difficulty, "behaviorVersion": 2, "behavior": behavior, "struggles": struggles,
        "minDurationMs": 5200 + difficulty * 700, "maxDurationMs": 70000, "checkpointIntervalMs": 2500, "maxInputEvents": 1000,
    }


def _replay_fight(challenge, proof, *, late_release=False):
    """照前端 stepFight() 独立重写的复算：服务器拿到的只有题目 + 事件，它能算出的就是这些。
    late_release=True：松线晚一步生效（前端把松线记在上一步，服务器若按那种口径复算就是这样）。"""
    low, high, power = challenge["targetLow"], challenge["targetHigh"], challenge["fishPower"]
    offset = sum(ord(ch) for ch in challenge["fishSeed"]) / 19
    marks = {event["t"]: event["holding"] for event in proof["events"]}
    tension, progress, holding, release_next, out_ms = (low + high) / 2 - 8, 0.0, False, False, 0
    for t in range(20, proof["durationMs"] + 1, 20):
        if release_next:
            holding, release_next = False, False
        if marks.get(t) is True:
            holding = True
        elif marks.get(t) is False:
            release_next = late_release
            holding = holding and late_release
        pull = power * (0.72 + math.sin(t * 0.0027 * power + offset) * 0.24 + max(0, math.sin(t * 0.0041 + offset * 1.7)) * 0.42)
        for s in challenge["struggles"]:
            if 0 <= t - s["startMs"] < s["durationMs"]:
                portion = (t - s["startMs"]) / s["durationMs"]
                wave = math.sin(math.pi * portion)
                if challenge["behavior"] == "steady":
                    pull += power * s["strength"] * 0.18 * wave
                elif challenge["behavior"] == "leap":
                    pull *= 1 + s["strength"] * 0.48 * wave
                else:
                    pull += power * s["strength"] * 0.72 * (0.55 + 0.45 * math.sin(portion * math.pi * 2))
                break
        tension += ((24 + pull * 3.1) if holding else (pull * 4.8 - 24)) * 0.02 + math.sin(t * 0.012 + offset) * 0.24
        tension = max(0.0, min(100.0, tension))
        if low <= tension <= high:
            progress += (8.2 + power * 0.7 + (2.2 if holding else 0.5)) * 0.02
        else:
            out_ms += 20
            progress -= ((1.5 + power * 0.25) if tension > high else 0.9) * 0.02
        if holding and tension < low:
            progress += 1.1 * 0.02
        progress = max(0.0, min(100.0, progress))
    return progress, out_ms


def test_fight_proof_replays_to_a_landed_fish():
    """交上去的事件照前端规则重放必须上鱼；松线晚一步生效（另一种复算口径）也得过 99.5%。"""
    for difficulty in (1, 2, 3):
        for behavior in ("steady", "leap", "surge"):
            for seed in range(12):
                challenge = _preview_fight(difficulty, behavior, seed % 2 == 0, seed * 37 + difficulty)
                plan = fishing.simulate_fishing_fight(challenge)
                proof = plan["proof"]
                assert proof["landed"] and proof["challengeId"] == challenge["challengeId"], proof
                assert proof["durationMs"] % 20 == 0 and proof["durationMs"] >= challenge["minDurationMs"], proof
                times = [event["t"] for event in proof["events"]]
                assert times == sorted(set(times)) and all(t % 20 == 0 and t <= proof["durationMs"] for t in times), times
                assert [event["holding"] for event in proof["events"]] == [i % 2 == 0 for i in range(len(times))], proof
                assert len(times) < 60, len(times)  # 一秒按放两三次，像人手
                progress, out_ms = _replay_fight(challenge, proof)
                assert progress >= 100 and out_ms == 0, (challenge, progress, out_ms)
                assert abs(progress - plan["state"]["progress"]) < 1e-6, (progress, plan["state"])
                late, _ = _replay_fight(challenge, proof, late_release=True)
                assert late >= 99.5, (challenge, late)
                # 前端每 2.5 秒报一次，快照里的事件不超过当时的进度
                assert [cp["durationMs"] for cp in plan["checkpoints"]] == list(range(2500, proof["durationMs"], 2500)), plan["checkpoints"]
                for cp in plan["checkpoints"]:
                    assert all(event["t"] <= cp["durationMs"] for event in cp["events"]), cp
                    assert set(cp["state"]) == {"progress", "tension", "holding", "danger_ms", "slack_ms", "samples", "stable_samples"}, cp
    # 旧私聊入口的交卷格式不变
    assert set(fishing.build_fishing_proof(_preview_fight(1, "steady", False, 1))) == {"mode", "challengeId", "durationMs", "events"}


def test_scheduler_records_every_cast():
    """逐竿记录：调度器跑完一批，钓上、空竿各落一行 fishing_casts，原始 result 原样留着。"""
    try:
        import asyncio
        import tempfile
        from types import SimpleNamespace

        from tg_game.runtime import executors
        from tg_game.storage import Storage
    except Exception as exc:
        print(f"  (skipped fishing_casts: {type(exc).__name__})")
        return

    results = [
        {"ready": True, "caught": True, "fish": {"name": "青鳞小鲫", "weight": 1.2}, "rarityLabel": "凡品", "bonusLoot": [{"name": "灵石", "qty": 3}]},
        {"ready": True, "caught": False},
    ]
    server, _ = _legacy_server()

    def transport(request):
        if request["url"].endswith("/xianxia-fishing/result"):
            return 200, {"ok": True, "result": results.pop(0)}
        return server(request)

    async def fake_flow(client, **kwargs):
        return _run(transport, max_rounds=2)[0]

    real_flow = executors.fishing_miniapp.run_fishing_miniapp_public_production_flow
    executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = fake_flow
    try:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("t").id
            storage.upsert_fishing_session(profile_id=pid, chat_id=-100123, enabled=True, state="miniapp_batch", daily_limit=5)
            client = SimpleNamespace(_tg_game_profile_id=pid)
            asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            with storage.connect() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM fishing_casts ORDER BY id")]
            session = storage.get_fishing_session(pid, -100123)
    finally:
        executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = real_flow

    assert [(r["profile_id"], r["pond"], r["bait"], r["caught"], r["fish"], r["grade"], r["weight"]) for r in rows] == [
        (pid, "青溪浅滩", "凡饵", 1, "青鳞小鲫", "凡品", "1.2"),
        (pid, "青溪浅滩", "凡饵", 0, "", "", ""),
    ], rows
    assert json.loads(rows[0]["result_json"])["bonusLoot"] == [{"name": "灵石", "qty": 3}], rows[0]
    assert all(abs(r["created_at"] - time.time()) < 60 for r in rows), rows
    assert session["catches"] == {"青鳞小鲫": 1}, session  # 会话照常更新


def _legacy_server(*, bait_count=9, entry=True, daily_limit=5):
    """洞府入口 + 老垂钓接口（/api/miniapp/xianxia-fishing/*）的假服务器；log 记 (接口, 载荷)。

    形状按 10-14 的老页面源码和当晚实测回包：start 回 lobby 或 challenge，next 换发新令牌，result 回渔获。
    """
    log, state = [], {"casts": 0, "bait": bait_count}
    challenge = _preview_fight(1, "steady", False, 7)

    def transport(request):
        name = urlsplit(request["url"]).path.split("/api/miniapp/", 1)[-1]
        payload = dict(request["payload"])
        log.append((name, payload))
        if name == "xianxia-dwelling/start":
            return 200, {"ok": True, "account": {"playerId": "1"}}
        if name == "xianxia-dwelling/external":
            assert payload["action"] == "fishing", payload
            if not entry:
                return 400, {"ok": False, "error": "external_app_unavailable"}
            return 200, {"ok": True, "url": "/miniapp/xianxia-fishing?startapp=fish_entry0001"}
        assert payload["token"].startswith("fish_"), (name, payload)
        if name == "xianxia-fishing/start":
            if payload["token"] == "fish_entry0001":
                return 200, {"ok": True, "session": {"phase": "lobby"}}
            return 200, {"ok": True, "session": {"phase": "bite"}, "challenge": challenge}
        if name == "xianxia-fishing/shop":
            return 200, {"ok": True, "shop": {
                "ponds": [{"key": "qingxi", "name": "青溪浅滩", "unlocked": True}],
                "baits": [{"key": "plain", "itemId": "item_fishing_bait_plain", "name": "凡饵", "count": state["bait"], "cost": 12, "unlocked": True}],
                "chums": [{"key": "herb", "name": "灵草窝", "uses": 5, "remainingToday": 1, "cost": [{"name": "凝血草", "qty": 3, "owned": 9}]}],
            }}
        if name == "xianxia-fishing/buy-bait":
            state["bait"] += int(payload["quantity"])
            return 200, {"ok": True}
        if name == "xianxia-fishing/next":
            assert payload["pondKey"] == "qingxi" and payload["baitItemId"] == "item_fishing_bait_plain", payload
            if state["casts"] >= daily_limit:
                return 409, {"ok": False, "error": "fishing_daily_limit_reached"}
            state["casts"] += 1
            state["bait"] -= 1
            return 200, {"ok": True, "token": f"fish_cast{state['casts']:04d}", "today": state["casts"], "limit": daily_limit}
        if name == "xianxia-fishing/finish":
            assert payload["fishingProof"]["mode"] == "xianxiaFishingV2" and "landed" not in payload["fishingProof"], payload
            return 200, {"ok": True, "result": {"details": {}}}
        if name == "xianxia-fishing/result":
            return 200, {"ok": True, "result": {"ready": True, "caught": True, "fish": {"name": "银须灵鲢", "weight": 2.66}, "rarityLabel": "灵鱼",
                                                 "bonusLoot": [], "reason": "lift", "weatherKey": "rain"}}
        raise AssertionError(f"unexpected request: {name}")

    return transport, log


def test_public_flow_prefers_the_legacy_api():
    """10-14 实测：两位侍妾都在海上，洞府钓位 409，老垂钓接口照常上鱼——所以只走老接口，洞府钓位一个请求都不发。"""
    transport, log = _legacy_server()
    result, _ = _run(transport, max_rounds=2)
    names = [name for name, _ in log]
    assert names[:3] == ["xianxia-dwelling/start", "xianxia-dwelling/external", "xianxia-fishing/start"], names
    assert not any(name.startswith("xianxia-dwelling/fishing/") for name in names), names
    assert result["ok"] is True and result["data"]["settled_count"] == 2, json.dumps(result, ensure_ascii=False)[:600]
    # 逐竿记录要的三样：时刻、原因、服务器原样结果（天象、保底标记都在里面）
    rounds = result["data"]["rounds"]
    assert [(r["catch"]["fish"], r["reason"], r["result"]["weatherKey"]) for r in rounds] == [("银须灵鲢", "lift", "rain")] * 2, rounds
    assert all(abs(r["at"] - time.time()) < 60 for r in rounds), rounds
    # 商店现价带出来（抓包报告不存数值）
    prices = result["shopPrices"]   # 挂在结果顶层：放进 data 会被调度器当成今日竿数解析
    assert "shopPrices" not in result["data"], result["data"]
    assert prices["baits"]["凡饵"]["cost"] == 12 and prices["chums"]["灵草窝"] == {"uses": 5, "remainingToday": 1, "cost": {"凝血草": 3}}, prices


def test_public_flow_fails_when_the_legacy_entry_is_gone():
    """老垂钓入口拿不到就按失败交给调度器：不再退回洞府钓位，老接口也一个请求都不发。"""
    for transport, log, expected, sent in (
        (*_legacy_server(entry=False), "external:external_app_unavailable", ["xianxia-dwelling/start", "xianxia-dwelling/external"]),
        (lambda request: (404, {"ok": False, "error": "not_found"}), None, "start:not_found", None),   # 连洞府 start 都不通
    ):
        result, _ = _run(transport, max_rounds=1)
        assert result["ok"] is False and result["status"] == "failed", result
        assert result["error"] == f"fishing_legacy_entry_unavailable({expected})", result
        if sent is not None:
            assert [name for name, _ in log] == sent, log


def test_legacy_flow_reports_missing_bait_or_buys_with_an_operation_id():
    # 制饵优先、还没制过：把缺口报给调度器去排 .制饵，不碰 next
    transport, log = _legacy_server(bait_count=0)
    result, _ = _run(transport, max_rounds=5, auto_buy_bait=False)
    assert result["ok"] is True and result["status"] == "need_bait", result
    assert (result["data"]["baitName"], result["data"]["baitCount"], result["data"]["baitQuantity"]) == ("凡饵", 0, 1), result["data"]
    assert "xianxia-fishing/next" not in [name for name, _ in log], log
    try:  # 调度器不能把「缺饵」记成钓了一竿，也不能把商店里的 count 当成今日竿数（进场时还没有今日竿数可报）
        from tg_game.runtime import executors
        updates = executors._build_fishing_miniapp_result_updates({"daily_count": 3, "daily_limit": 5}, result, now=time.time(), bot_message_id=0)
        assert updates["daily_count"] == 3, updates
    except ImportError as exc:
        print(f"  (skipped need_bait count: {type(exc).__name__})")
    # 允许买：10-14 01:40 丁真人两位侍妾都在海上，老接口买饵 200（洞府钓位买饵是 409）
    transport, log = _legacy_server(bait_count=0)
    result, _ = _run(transport, max_rounds=1, auto_buy_bait=True)
    buy = next(payload for name, payload in log if name == "xianxia-fishing/buy-bait")
    assert buy["baitKey"] == "plain" and buy["quantity"] == 1 and UUID_RE.match(buy["operationId"]), buy
    assert result["ok"] is True and result["data"]["settled_count"] == 1, result
    # 不许买、手里有 3 个：3 个都钓掉再报缺（不能因为「不够整批」就带着饵停下——只制饵模式会把当天关掉）
    transport, log = _legacy_server(bait_count=3)
    result, _ = _run(transport, max_rounds=5, auto_buy_bait=False)
    assert result["status"] == "need_bait" and result["data"]["settled_count"] == 3, result["data"]
    assert (result["data"]["baitCount"], result["data"]["baitQuantity"], result["data"]["dailyUsed"]) == (0, 2, 3), result["data"]
    # 结算一直等不到的那一竿：留在 rounds 里但标 ok=False（调度器不把它记成空竿）
    transport, log = _legacy_server()
    pending = lambda request: (200, {"ok": True, "result": {"ready": False}}) if request["url"].endswith("/xianxia-fishing/result") else transport(request)
    result, _ = _run(pending, max_rounds=1)
    assert result["ok"] is False and [r["ok"] for r in result["data"]["rounds"]] == [False], result


def test_bait_crafting_orders_by_yield_and_skips_when_materials_are_short():
    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped bait crafting: {type(exc).__name__})")
        return

    class FakeStorage:
        def __init__(self, materials):
            self.state, self.queued, self.materials = {}, [], materials

        def get_external_account(self, profile_id, provider):
            return {"me_json": json.dumps({"inventory": json.dumps({"materials": self.materials})})}

        def get_game_items(self):
            return CATALOG

        def get_runtime_state(self, key):
            return self.state.get(key)

        def set_runtime_state(self, key, value):
            self.state[key] = value

        def get_latest_outgoing_command(self, chat_id, profile_id=None, text="", thread_id=None):
            return None

        def enqueue_outgoing_command(self, **kwargs):
            self.queued.append(kwargs)
            return len(self.queued)

    now = time.time()

    def craft(materials, session, *, count=0, batch=True):
        storage = FakeStorage(materials)
        # 老接口进场时的报法：只说下一竿缺 1 个
        result = {"status": "need_bait", "data": {"baitName": session["bait"], "baitCount": count, "baitQuantity": 1}}
        updates = executors._request_fishing_bait_craft(
            storage, result, profile_id=3, chat_id=-100123, thread_id=1000003, session=session, batch_mode=batch, now=now)
        return [q["text"] for q in storage.queued], updates, storage

    small = {"bait": "凡饵", "daily_limit": 5, "daily_count": 0, "chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    big = {**small, "bait": "妖血饵", "daily_limit": 15}
    # 10-12~10-14 小号天天「制饵失败，资源不足：水草团x1」：5 竿下了 5 份凡饵，其实一份出 2 个、3 份就够，水草团 4 个够用
    assert craft({"mat_fish_meat": 125, "mat_fishing_waterweed": 4}, small)[0] == [".制饵 凡饵 3"]
    assert craft({"mat_fish_meat": 125, "mat_fishing_waterweed": 4}, small, count=2)[0] == [".制饵 凡饵 2"]   # 手里还有 2 个
    assert craft({"mat_fish_meat": 125, "mat_fishing_waterweed": 1}, small)[0] == [".制饵 凡饵 1"]            # 能制几份制几份，剩下的下一轮买
    assert craft({"mat_fish_meat": 125, "mat_fishing_waterweed": 4}, {**small, "daily_count": 3})[0] == [".制饵 凡饵 1"]  # 今天只剩 2 竿
    # 一份都凑不出：不发注定失败的 .制饵，记成今天制过了，立刻重跑（按「缺饵时」开关去买或停手）
    queued, updates, storage = craft({"mat_fish_meat": 125}, small)
    assert queued == [] and updates["next_action_at"] == now and updates["enabled"] is True, (queued, updates)
    assert executors._fishing_bait_crafted_today(storage, profile_id=3, chat_id=-100123, now=now) is True
    # 大号：按会话里今天剩的 15 竿一次制齐；灵鱼肉只够 8 个就制 8 个；一阶妖丹只有 2 个就制 2 个
    assert craft({"mat_fish_meat": 60, "mat_fish_scale": 272, "mat_core_1": 20}, big)[0] == [".制饵 妖血饵 15"]
    assert craft({"mat_fish_meat": 33, "mat_fish_scale": 272, "mat_core_1": 20}, big)[0] == [".制饵 妖血饵 8"]
    assert craft({"mat_fish_meat": 60, "mat_fish_scale": 272, "mat_core_1": 2}, big)[0] == [".制饵 妖血饵 2"]
    assert craft({"mat_fish_meat": 60, "mat_fish_scale": 272}, big)[0] == []                                   # 没有一阶妖丹：一份都制不了
    assert craft({"mat_fish_meat": 60, "mat_fish_scale": 272, "mat_core_1": 20}, big, batch=False)[0] == [".制饵 妖血饵 1"]  # 试钓只要 1 个
    # 天机阁没同步到材料：不设上限，照旧发出去试
    assert craft(None, small)[0] == [".制饵 凡饵 3"]
    # 流程报了服务器口径的今日竿数（洞府钓位、老接口钓到一半）：按它算，不看会话里过时的数
    storage = FakeStorage({"mat_fish_meat": 600, "mat_fish_scale": 272, "mat_core_1": 20})
    reported = {"status": "need_bait", "data": {"baitName": "妖血饵", "baitCount": 0, "baitQuantity": 12, "dailyUsed": 3, "dailyLimit": 15}}
    executors._request_fishing_bait_craft(storage, reported, profile_id=3, chat_id=-100123, thread_id=1000003,
                                          session={**big, "daily_limit": 5}, batch_mode=True, now=now)
    assert [q["text"] for q in storage.queued] == [".制饵 妖血饵 12"], storage.queued


CATALOG = {"mat_fish_meat": {"name": "灵鱼肉"}, "mat_fish_scale": {"name": "灵鱼鳞"}, "mat_fishing_waterweed": {"name": "水草团"},
           "mat_core_1": {"name": "一阶妖丹"}, "mat_fish_yinxu_lian": {"name": "银须灵鲢"}, "item_fishing_bait_demon_blood": {"name": "妖血饵"},
           "item_fishing_bait_plain": {"name": "凡饵"}}


class _MidnightStorage:
    """开钓前制饵、零点开鱼用的假库：会话、开关、天机阁库存、物品目录。"""

    def __init__(self, materials, *, bait="妖血饵", daily_limit=15, bait_source="craft", auto_open=True):
        self.materials = materials
        self.session = {"bait": bait, "daily_limit": daily_limit, "bait_source": bait_source}
        self.state = {"fishing_auto_open:2": json.dumps({"enabled": auto_open})}
        self.queued = []

    def get_fishing_session(self, profile_id, chat_id):
        return self.session

    def get_external_account(self, profile_id, provider):
        return {"me_json": json.dumps({"inventory": {"materials": self.materials}})}

    def get_game_items(self):
        return CATALOG

    def get_runtime_state(self, key):
        return self.state.get(key)

    def set_runtime_state(self, key, value):
        self.state[key] = value

    def enqueue_outgoing_command(self, **kwargs):
        self.queued.append(kwargs)
        return len(self.queued)


def test_bait_is_crafted_before_the_fishing_time():
    """用户 10-15：确保零点就开钓——制饵放到开钓前 10 分钟，零点再缺饵就直接买（鱼材已经用光）。"""
    try:
        import asyncio

        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped precraft: {type(exc).__name__})")
        return
    task = {"thread_id": 1000003, "chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    fishing_at = time.time() + 600
    key = "fishing_bait_craft_at:2:-100123"

    def precraft(storage):
        real = executors._refresh_companion_payload
        executors._refresh_companion_payload = lambda _storage, _pid: None   # 刷不出来就用缓存
        try:
            return asyncio.run(executors._precraft_fishing_bait(storage, task=task, profile_id=2, chat_id=-100123, fishing_at=fishing_at))
        finally:
            executors._refresh_companion_payload = real

    # 10-15 大号：手里 1 个妖血饵，鱼材够制 6 个（一阶妖丹只有 6 个）——缺 14，制 6，剩下 8 个零点直接买
    storage = _MidnightStorage({"item_fishing_bait_demon_blood": 1, "mat_fish_meat": 27, "mat_fish_scale": 33, "mat_core_1": 6})
    assert precraft(storage) == ".制饵 妖血饵 6"
    assert [(q["text"], q["thread_id"]) for q in storage.queued] == [(".制饵 妖血饵 6", 1000003)], storage.queued
    # 记成开钓那天已制过：零点缺饵按「缺饵时」直接买，不再制一次、不等 90 秒
    assert executors._fishing_bait_crafted_today(storage, profile_id=2, chat_id=-100123, now=fishing_at + 60) is True
    assert precraft(storage) == "" and len(storage.queued) == 1   # 同一天不制第二次
    # 手里的饵够一天：不制，也记成制过
    enough = _MidnightStorage({"item_fishing_bait_demon_blood": 15, "mat_fish_meat": 99, "mat_fish_scale": 99, "mat_core_1": 99})
    assert precraft(enough) == "" and enough.queued == [] and enough.state.get(key)
    # 小号凡饵：5 竿缺 5 个，一份出 2 个，制 3 份
    small = _MidnightStorage({"mat_fish_meat": 30, "mat_fishing_waterweed": 4}, bait="凡饵", daily_limit=5)
    assert precraft(small) == ".制饵 凡饵 3"
    # 鱼材一份都凑不出：不发，也记成制过（零点直接买）
    poor = _MidnightStorage({"mat_fish_meat": 3}, bait="凡饵", daily_limit=5)
    assert precraft(poor) == "" and poor.queued == [] and poor.state.get(key)
    # 只买饵模式不制；天机阁读不到不制也不记（零点照旧现制）
    assert precraft(_MidnightStorage({"mat_fish_meat": 99}, bait_source="buy")) == ""
    unknown = _MidnightStorage({})
    assert precraft(unknown) == "" and key not in unknown.state


def test_one_yinxu_lian_is_opened_at_the_fishing_time():
    """用户 10-15：每天零点开一条银须灵鲢（开钓时发，钓完开鱼时留着的那条）。"""
    try:
        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped midnight open: {type(exc).__name__})")
        return
    task = {"thread_id": 1000003, "chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    storage = _MidnightStorage({"mat_fish_yinxu_lian": 1})
    assert executors._queue_midnight_fish_open(storage, task=task, profile_id=2, chat_id=-100123) is True
    assert [(q["text"], q["thread_id"]) for q in storage.queued] == [(".开鱼 银须灵鲢 1", 1000003)], storage.queued
    assert executors._queue_midnight_fish_open(_MidnightStorage({}), task=task, profile_id=2, chat_id=-100123) is False   # 没有就不发
    off = _MidnightStorage({"mat_fish_yinxu_lian": 3}, auto_open=False)
    assert executors._queue_midnight_fish_open(off, task=task, profile_id=2, chat_id=-100123) is False and off.queued == []


def test_midnight_daily_limit_before_the_server_rolls_over_is_retried():
    """零点整开钓：服务器还没翻日会回「今日竿数已用完」——一竿没钓、会话还是 0 竿时当成没翻日，别把今天的垂钓关掉。"""
    try:
        from datetime import datetime as _dt

        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped rollover: {type(exc).__name__})")
        return
    from tg_game import pagoda_auto

    midnight = _dt.now(pagoda_auto.SHANGHAI_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rolled = {"ok": True, "status": "daily_limit", "data": {"settled_count": 0, "dailyUsed": 0}}
    fresh = {"daily_count": 0, "daily_limit": 15}
    assert executors._fishing_day_not_rolled_over(rolled, fresh, now=midnight + 3) is True
    assert executors._fishing_day_not_rolled_over(rolled, fresh, now=midnight + 800) is True
    assert executors._fishing_day_not_rolled_over(rolled, fresh, now=midnight + 1000) is False         # 过了 15 分钟就是真用完了
    assert executors._fishing_day_not_rolled_over(rolled, {"daily_count": 4}, now=midnight + 3) is False   # 今天已经钓过
    done = {"ok": True, "status": "daily_limit", "data": {"settled_count": 15}}
    assert executors._fishing_day_not_rolled_over(done, fresh, now=midnight + 200) is False            # 这一轮钓满了


def test_daily_task_precrafts_then_arms_and_opens_one_fish_at_the_fishing_time():
    """调度器整轮：开钓前 10 分钟内排一次 .制饵（不武装）；离开钓不到 90 秒不预制；到点武装会话并开一条银须灵鲢。"""
    try:
        import asyncio
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from tg_game.runtime import executors
        from tg_game.storage import ASC_EXTERNAL_PROVIDER, Storage
    except Exception as exc:
        print(f"  (skipped daily precraft: {type(exc).__name__})")
        return
    chat = -1005550000555
    payload = {"companion": {"name": "t"}, "inventory": {"materials": {
        "item_fishing_bait_demon_blood": 1, "mat_fish_meat": 27, "mat_fish_scale": 33, "mat_core_1": 6, "mat_fish_yinxu_lian": 1}}}

    def run(offset, *, precrafted=False):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("t").id
            storage.create_chat_binding(pid, chat, bot_username="fanrenxiuxian_bot")
            storage.upsert_game_items([{"id": k, "name": v["name"]} for k, v in CATALOG.items()])
            storage.upsert_external_account(pid, ASC_EXTERNAL_PROVIDER, "", "", "connected", "", payload, "")
            storage.upsert_fishing_session(profile_id=pid, chat_id=chat, enabled=False, state="finished", bait="妖血饵", pond="乱星海礁", daily_limit=15)
            storage.set_runtime_state(f"fishing_auto_open:{pid}", json.dumps({"enabled": True}))
            task = storage.upsert_companion_auto_task(profile_id=pid, chat_id=chat, feature_key=executors.biz_fishing_daily_auto.FEATURE_KEY,
                                                      enabled=True, strategy="00:00", bot_username="fanrenxiuxian_bot")
            storage.update_companion_auto_task(task["id"], next_run_at=time.time() + offset, last_run_at=time.time() - 86400 * 2)
            client = SimpleNamespace(_tg_game_profile_id=pid, _tg_game_storage=storage)
            with patch.object(executors, "_refresh_companion_payload", return_value=payload):
                for _ in range(2):   # 同一窗口里跑两轮，只排一次
                    asyncio.run(executors._run_companion_auto_scheduler(client, storage, run_once=True, include_tianxing=False, task_ids={task["id"]}))
            with storage.connect() as conn:
                sent = [r[0] for r in conn.execute("select text from outgoing_commands where profile_id=? order by id", (pid,))]
            return sent, storage.get_fishing_session(pid, chat)

    sent, session = run(300)
    assert sent == [".制饵 妖血饵 6"] and session["state"] == "finished", (sent, session["state"])   # 缺 14，妖丹只够 6 份；还没到点不武装
    assert run(700)[0] == []        # 离开钓还早
    assert run(30)[0] == []         # 离开钓不到 90 秒：不预制，留给开钓时现制
    sent, session = run(-1)
    assert sent == [".开鱼 银须灵鲢 1"] and session["state"] == "miniapp_batch" and session["enabled"], (sent, session["state"])
    sent, session = run(-900)       # 到点任务被推迟了一刻钟（恢复保护、补跑）：照常武装，但零点那条鱼留到明天
    assert sent == [] and session["state"] == "miniapp_batch", (sent, session["state"])


def test_rollover_retry_keeps_the_day_armed():
    """调度器接线：没翻日的 daily_limit 不关当天垂钓、不开鱼、一分钟后再试；真钓满照常收尾。"""
    try:
        import asyncio
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        from tg_game.runtime import executors
        from tg_game.storage import Storage
    except Exception as exc:
        print(f"  (skipped rollover wiring: {type(exc).__name__})")
        return

    async def fake_flow(client, **kwargs):
        return {"ok": True, "status": "daily_limit", "error": "", "entry": {}, "events": [],
                "data": {"settled_count": 0, "rounds": [], "catches": [], "dailyUsed": 0, "dailyLimit": 15}}

    for not_rolled in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("t").id
            storage.upsert_fishing_session(profile_id=pid, chat_id=-100123, enabled=True, state="miniapp_batch", daily_limit=15)
            storage.set_runtime_state(f"fishing_auto_open:{pid}", json.dumps({"enabled": True}))
            client = SimpleNamespace(_tg_game_profile_id=pid)
            with patch.object(executors.fishing_miniapp, "run_fishing_miniapp_public_production_flow", fake_flow), \
                 patch.object(executors, "_fishing_day_not_rolled_over", return_value=not_rolled), \
                 patch.object(executors, "_refresh_companion_payload", return_value=None):
                asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            session = storage.get_fishing_session(pid, -100123)
            if not_rolled:
                assert session["enabled"] and session["state"] == "miniapp_batch" and session["daily_count"] == 0, dict(session)
                assert abs(session["next_action_at"] - time.time() - executors.FISHING_ROLLOVER_RETRY_SECONDS) < 10, session["next_action_at"]
                assert "opened_at" not in json.loads(storage.get_runtime_state(f"fishing_auto_open:{pid}")), "没钓就不该开鱼"
            else:
                assert not session["enabled"] and session["state"] == "finished", dict(session)


def test_scheduler_with_the_legacy_api_records_casts_and_prices():
    try:
        import asyncio
        import tempfile
        from types import SimpleNamespace

        from tg_game.runtime import executors
        from tg_game.storage import Storage
    except Exception as exc:
        print(f"  (skipped legacy scheduler: {type(exc).__name__})")
        return

    flows = []

    async def fake_flow(client, **kwargs):
        return flows.pop(0)

    real_flow = executors.fishing_miniapp.run_fishing_miniapp_public_production_flow
    executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = fake_flow
    try:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.db")
            storage.init_schema()
            pid = storage.create_profile("t").id
            storage.upsert_fishing_session(profile_id=pid, chat_id=-100123, enabled=True, state="miniapp_batch", daily_limit=5)
            client = SimpleNamespace(_tg_game_profile_id=pid)
            rearm = lambda: storage.update_fishing_session(storage.get_fishing_session(pid, -100123)["id"], enabled=True, state="miniapp_batch", next_action_at=0)

            # 老垂钓入口拿不到：走现有的失败分支——当天关掉、原因进 last_error，不排群指令、不记竿
            flows.append(_run(_legacy_server(entry=False)[0], max_rounds=1)[0])
            asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            session = storage.get_fishing_session(pid, -100123)
            assert not session["enabled"] and session["state"] == "miniapp_failed", dict(session)
            assert "fishing_legacy_entry_unavailable(external:external_app_unavailable)" in session["last_error"], session["last_error"]
            with storage.connect() as conn:
                assert conn.execute("SELECT count(*) FROM outgoing_commands").fetchone()[0] == 0

            # 没结算的竿不进逐竿记录
            rearm()
            flows.append({"ok": False, "status": "result_pending", "error": "fishing_result_pending",
                          "data": {"rounds": [{"index": 1, "ok": False, "status": "result_pending", "catch": {}, "at": time.time(), "result": {}}]}, "events": []})
            asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            with storage.connect() as conn:
                assert conn.execute("SELECT count(*) FROM fishing_casts").fetchone()[0] == 0

            # 老接口钓得动的一轮：逐竿记录带服务器原样结果，商店现价存下来
            rearm()
            flows.append(_run(_legacy_server()[0], max_rounds=2)[0])
            asyncio.run(executors._run_fishing_auto_scheduler(client, storage, run_once=True))
            prices = json.loads(storage.get_runtime_state(executors.FISHING_SHOP_PRICES_KEY.format(profile_id=pid)))
            assert prices["baits"]["凡饵"]["cost"] == 12, prices
            with storage.connect() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM fishing_casts ORDER BY id")]
            assert [(r["fish"], r["reason"], json.loads(r["result_json"]).get("weatherKey")) for r in rows] == [("银须灵鲢", "lift", "rain")] * 2, rows
            assert storage.get_fishing_session(pid, -100123)["daily_count"] == 2
    finally:
        executors.fishing_miniapp.run_fishing_miniapp_public_production_flow = real_flow


def test_fish_opening_queues_one_command_per_species_once_a_day():
    try:
        import asyncio

        from tg_game.runtime import executors
    except Exception as exc:
        print(f"  (skipped fish opening: {type(exc).__name__})")
        return

    class FakeStorage:
        def __init__(self, switch):
            self.state = {"fishing_auto_open:3": json.dumps(switch)}
            self.queued = []

        def get_runtime_state(self, key):
            return self.state.get(key)

        def update_runtime_state_fields(self, key, fields, expected_fields=None):
            self.state[key] = json.dumps({**json.loads(self.state.get(key) or "{}"), **fields})

        def get_game_items(self):
            return {"mat_fish_yinxu_lian": {"name": "银须灵鲢"}, "mat_fish_qinglin_ji": {"name": "青鳞小鲫"},
                    "mat_fish_meat": {"name": "灵鱼肉"}, "mat_fish_scale": {"name": "灵鱼鳞"}, "mat_fish_odd": {"name": "怪 鱼"}}

        def get_external_account(self, profile_id, provider):
            return {"me_json": json.dumps({"inventory": {"materials": {"mat_fish_yinxu_lian": 9}}})}   # 缓存是旧的

        def enqueue_outgoing_command(self, **kwargs):
            self.queued.append(kwargs)
            return len(self.queued)

    fresh = {"inventory": json.dumps({"materials": {
        "mat_fish_yinxu_lian": 2, "mat_fish_qinglin_ji": 1, "mat_fish_meat": 125, "mat_fish_scale": 90,
        "mat_fish_odd": 3, "mat_fish_unknown": 2, "mat_fishing_waterweed": 4, "mat_001": 500,
    }})}
    session = {"chat_type": "group", "bot_username": "fanrenxiuxian_bot"}
    now = time.time()
    refreshed = fresh

    def run(storage, at=now):
        real = executors._refresh_companion_payload
        executors._refresh_companion_payload = lambda _storage, _pid: refreshed
        try:
            return asyncio.run(executors._queue_fish_opening(storage, profile_id=3, chat_id=-100123, thread_id=1000003, session=session, now=at))
        finally:
            executors._refresh_companion_payload = real

    # 开着：按刚刷新的库存每种鱼一条指令、带数量（零点全局发送上限 10 分钟 30 条）；鱼肉鱼鳞、不认识的、名字不能进指令的都不开
    storage = FakeStorage({"enabled": True})
    assert run(storage) == 2
    assert [(q["text"], q["delay_seconds"], q["thread_id"]) for q in storage.queued] == [
        (".开鱼 青鳞小鲫 1", 0, 1000003), (".开鱼 银须灵鲢 1", 5, 1000003)], storage.queued   # 银须灵鲢留一条给明天零点
    assert json.loads(storage.state["fishing_auto_open:3"])["opened_count"] == 2
    # 同一天不排第二次（指令还在路上、天机阁里鱼还没扣掉）；第二天再开
    assert run(storage) == 0 and len(storage.queued) == 2
    assert run(storage, at=now + 86400) == 2 and json.loads(storage.state["fishing_auto_open:3"])["enabled"] is True
    # 没开开关：什么都不发
    off = FakeStorage({})
    assert run(off) == 0 and off.queued == [] and "opened_at" not in json.loads(off.state["fishing_auto_open:3"])
    # 天机阁刷不出来：退回缓存里的库存
    refreshed = None
    cached = FakeStorage({"enabled": True})
    assert run(cached) == 1 and [q["text"] for q in cached.queued] == [".开鱼 银须灵鲢 8"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"test_dwelling_fishing_miniapp: ok ({len(tests)} tests)")
