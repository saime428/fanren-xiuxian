import asyncio
import copy
import functools
from pathlib import Path

import pytest

from . import PLAYER_ID
from .protocol import (COMMANDS, ExplorationError, accept_preparation, active, day, empty_effect,
                       parse_board)
from .runtime import Cycle, Store, queue_report, report_text
from .test_protocol import BOARD, NOW


async def no_sleep(_):
    pass


def run_async(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class FakeClient:
    def __init__(self, *, count=0, rescues=(), protection=True, history=None):
        self.count = count
        self.rescues = set(rescues)
        self.board = parse_board(BOARD if protection else BOARD.replace("探索（剩余 23小时59分钟）", "无"), NOW)
        self.commands = {".天机盘", ".观命", ".定命", ".推命", ".改命"}
        self.sent = []
        self.modes = []
        self.history = history or {"actions": [], "observed_day": day(NOW)}
        self.node_responses = 0
        self.rift_responses = 0
        self.omit_protected = False

    def snapshot(self):
        return {"ok": True, "account": {"playerId": PLAYER_ID, "journey": {"wildExperience": {
            "dailyDate": day(NOW), "dailyCount": self.count, "dailyLimit": 8,
            "available": self.count < 8, "lastAt": int(NOW*1000), "modes": [{"key": "deep"}]}}}}

    async def open(self):
        return self.snapshot()

    async def details(self):
        self.sent.append("details")
        return self.snapshot()

    async def existing_actions(self):
        return self.history

    async def recover_group(self, pending):
        return getattr(self, "recovery_reply", None)

    def board_text(self):
        def effect(field):
            value = self.board[field]
            if not value.get("route"):
                return "无"
            seconds = max(0, int(value["expires_at"] - NOW))
            return f"{value['route']}（剩余 {seconds}秒）"
        return ("【天机盘】\n今日可选命星: 【紫微】、【太阴】、【贪狼】\n"
                f"今日已定命星: 【{self.board['star']}】\n当前推命: {effect('prediction')}\n"
                f"当前改命: {effect('protection')}\n天机值: {self.board['points']}\n逆命劫: {self.board['debt']}\n"
                f"命中 / 落空 / 改命: {self.board['hits']} / {self.board['misses']} / {self.board['rescues']}")

    async def command(self, command, *, on_sent=None):
        self.sent.append(command)
        if command == ".天机盘":
            text = self.board_text()
        elif command == ".观命":
            text = "今日显化的命星为 【紫微】、【太阴】、【贪狼】"
        elif command.startswith(".定命"):
            text = f"你将今日命轨定在 【{command.split()[1]}】。"
        elif command == ".推命 探索":
            text = "你拨动司命盘，为 【探索】 推下一段命数。\n此推命将在 8 小时 内生效。"
        elif command == ".改命 探索":
            text = "你以 3 点天机值，为 【探索】 预留了一次改命回天的余地。\n这道后手将在 24 小时 内生效一次。"
        elif command == ".搜寻节点":
            assert active(self.board, "prediction", NOW) and active(self.board, "protection", NOW)
            self.node_responses += 1
            text = "【虚空漫游】你顺手收集了一些【虚空尘埃】x2，也算不虚此行。"
        elif command == ".探寻裂缝":
            assert active(self.board, "prediction", NOW) and active(self.board, "protection", NOW)
            self.rift_responses += 1
            self.board["prediction"] = empty_effect()
            self.board["hits"] += 1
            self.board["points"] += 1
            text = f"【探寻成功】\n命盘【{self.board['star']}】照命\n【推命命中】\n【改命待发】23小时59分钟\n满载而归，为你带来了火！"
        else:
            raise AssertionError(command)
        if command.startswith((".观命", ".定命", ".推命", ".改命")):
            self.board = accept_preparation(self.board, command, text, NOW)
        return {"ok": True, "account": {"playerId": PLAYER_ID},
                "actionResult": {"command": command, "ok": True, "completed": True, "message": text},
                "_receipt": {"transport": "miniapp" if command.split()[0] in self.commands else "group", "at": NOW}}

    async def journey(self):
        assert active(self.board, "prediction", NOW), "unprotected prediction"
        assert active(self.board, "protection", NOW), "unprotected rescue"
        self.sent.append("journey")
        self.modes.append("deep")
        self.count += 1
        self.board["prediction"] = empty_effect()
        self.board["hits"] += 1
        self.board["points"] += 1
        rescued = self.count in self.rescues
        if rescued:
            self.board["protection"] = empty_effect()
            self.board["rescues"] += 1
        reply = self.snapshot()
        reply["actionResult"] = {"ok": True, "completed": True, "type": "wild_experience",
            "mode": {"key": "deep"}, "dailyCount": self.count, "outcome": "victory",
            "fateProtected": rescued, "cultivationDelta": 10, "loot": [{"name": "养魂木", "quantity": 1}]}
        if self.omit_protected:
            reply["actionResult"].pop("fateProtected")
        return reply


def make_cycle(tmp_path, client):
    store = Store(tmp_path, clock=lambda: NOW)
    store.data.update(node_done_day=day(NOW), rift_next_at=NOW+3600)
    return store, Cycle(store, client, clock=lambda: NOW, sleep=no_sleep)


@run_async
async def test_eight_normal_journeys_need_one_board_and_no_extra_protection(tmp_path):
    client = FakeClient(protection=False)
    store, cycle = make_cycle(tmp_path, client)
    result = await cycle.run()
    assert result["journey"]["count"] == 8
    assert client.sent.count(".天机盘") == 1
    assert client.sent.count(".推命 探索") == 8
    assert client.sent.count(".改命 探索") == 1
    assert client.sent.count(".定命 太阴") == 1
    assert client.modes == ["deep"]*8
    assert not store.data["pending"]
    assert store.data["board"]["points"] == 155
    assert "+80" in report_text(store.data)


@run_async
async def test_rescues_only_refill_before_a_next_action(tmp_path):
    client = FakeClient(protection=True, rescues={1, 3, 8})
    store, cycle = make_cycle(tmp_path, client)
    await cycle.run()
    assert client.sent.count(".改命 探索") == 2
    assert client.sent.count(".天机盘") == 1
    assert store.data["board"]["protection"]["route"] is None


@run_async
async def test_manual_completed_quota_is_not_replayed(tmp_path):
    client = FakeClient(count=8)
    _, cycle = make_cycle(tmp_path, client)
    await cycle.run()
    assert client.sent == [".天机盘"]


@run_async
async def test_manual_node_and_rift_history_seed_cadence(tmp_path):
    client = FakeClient(count=8, history={"observed_day": day(NOW), "actions": [
        {"kind": "node", "at": NOW-100, "status": "settled", "confirmed": True},
        {"kind": "rift", "at": NOW-100, "status": "success", "confirmed": True}]})
    store = Store(tmp_path, clock=lambda: NOW)
    cycle = Cycle(store, client, clock=lambda: NOW, sleep=no_sleep)
    await cycle.run()
    assert client.sent == [".天机盘"]
    assert store.data["node_done_day"] == day(NOW)
    assert store.data["rift_next_at"] == NOW-100+43200+3


@run_async
async def test_node_then_rift_share_preparation_with_one_node_readback(tmp_path):
    client = FakeClient(count=8)
    store = Store(tmp_path, clock=lambda: NOW)
    cycle = Cycle(store, client, clock=lambda: NOW, sleep=no_sleep)
    await cycle.run()
    assert client.sent.count(".推命 探索") == 1
    assert client.sent.count(".改命 探索") == 0
    assert client.sent.count(".天机盘") == 2
    assert client.node_responses == client.rift_responses == 1


@run_async
async def test_unresolved_journey_is_never_resent(tmp_path):
    client = FakeClient(count=0)
    store, cycle = make_cycle(tmp_path, client)
    store.begin("journey", "wild_experience:deep", before_count=0, transport="miniapp")
    with pytest.raises(ExplorationError, match="journey_outcome_pending"):
        await cycle.run()
    assert "journey" not in client.sent
    assert store.data["pending"]


@run_async
async def test_lost_journey_response_reconciles_quota_without_replay(tmp_path):
    client = FakeClient(count=8)
    store, cycle = make_cycle(tmp_path, client)
    store.begin("journey", "wild_experience:deep", before_count=7, transport="miniapp")
    await cycle.run()
    assert "journey" not in client.sent
    assert store.data["events"][-1]["status"] == "recovered_server_counter"
    assert store.data["events"][-1]["confirmed"] is False


@run_async
async def test_missing_fate_flag_uses_readback_without_extra_rearming(tmp_path):
    client = FakeClient(count=7, protection=True)
    client.omit_protected = True
    _, cycle = make_cycle(tmp_path, client)
    await cycle.run()
    assert client.sent.count(".天机盘") == 2
    assert client.sent.count(".改命 探索") == 0


@run_async
async def test_low_tianji_stops_before_sending_any_preparation(tmp_path):
    client = FakeClient(protection=False)
    client.board["points"] = 2
    _, cycle = make_cycle(tmp_path, client)
    with pytest.raises(ExplorationError, match="insufficient_tianji"):
        await cycle.run()
    assert not any(s in client.sent for s in ("journey", ".推命 探索", ".改命 探索"))


@run_async
async def test_read_only_inspection_never_prepares_or_explores(tmp_path):
    client = FakeClient(protection=False)
    store = Store(tmp_path, clock=lambda: NOW, persist=False)
    cycle = Cycle(store, client, clock=lambda: NOW, sleep=no_sleep)
    await cycle.run(read_only=True)
    assert client.sent == [".天机盘"]
    assert not store.path.exists()


def test_report_deduplicates_queries_and_records_new_business_results(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    store.data["journey"] = {"count": 8, "limit": 8}
    queue_report(store)
    store.data["calls"]["miniapp_reads"] = 200
    queue_report(store)
    assert len(store.data["outbox"]) == 1
    store.data["error"] = "insufficient_tianji"
    queue_report(store)
    assert len(store.data["outbox"]) == 2


@run_async
async def test_new_debt_stops_across_restarts_until_cleared(tmp_path):
    client = FakeClient(count=8)
    store, cycle = make_cycle(tmp_path, client)
    store.data["board"] = copy.deepcopy(client.board)
    client.board["debt"] = 1
    for _ in range(2):
        with pytest.raises(ExplorationError, match="new_fate_debt"):
            await cycle.refresh_board()
    assert store.data["debt_hold_level"] == 0
    client.board["debt"] = 0
    await cycle.refresh_board()
    assert "debt_hold_level" not in store.data
