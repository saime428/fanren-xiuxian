import copy
from datetime import datetime

import pytest

from .protocol import (TZ, ExplorationError, accept_preparation, active, apply_fate, choose_star,
                       day, fate_result, group_result, journey_result, midnight, parse_board,
                       required_preparation)

NOW = datetime(2026, 10, 15, 0, 5, tzinfo=TZ).timestamp()
BOARD = """【天机盘】
今日可选命星: 【紫微】、【太阴】、【贪狼】
今日已定命星: 【贪狼】
当前推命: 无
当前改命: 探索（剩余 23小时59分钟）
天机值: 150
逆命劫: 0
命中 / 落空 / 改命: 946 / 14 / 283"""


def test_per_action_star_fallbacks():
    assert choose_star("rift", ["太阴", "贪狼"]) == "贪狼"
    assert choose_star("journey", ["太阴", "贪狼"]) == "太阴"
    assert choose_star("node", ["太阴"]) == "太阴"
    assert choose_star("journey", ["贪狼"]) == "贪狼"
    assert choose_star("rift", ["紫微", "天府"]) is None


def test_existing_protection_needs_only_one_prediction():
    board = parse_board(BOARD, NOW)
    assert required_preparation(board, "rift", NOW) == [".推命 探索"]
    assert required_preparation(board, "journey", NOW) == [".定命 太阴", ".推命 探索"]


def test_both_missing_or_expired_and_money_gate():
    board = parse_board(BOARD.replace("探索（剩余 23小时59分钟）", "无"), NOW)
    assert required_preparation(board, "rift", NOW) == [".推命 探索", ".改命 探索"]
    board["points"] = 2
    with pytest.raises(ExplorationError, match="insufficient_tianji"):
        required_preparation(board, "rift", NOW)
    board["points"] = 100
    board["protection"] = {"known": True, "route": "探索", "expires_at": NOW-1}
    assert ".改命 探索" in required_preparation(board, "rift", NOW)


def test_other_route_must_not_be_overwritten():
    board = parse_board(BOARD.replace("当前改命: 探索", "当前改命: 斗法"), NOW)
    with pytest.raises(ExplorationError, match="another_fate_route"):
        required_preparation(board, "rift", NOW)


def test_mixed_pending_and_triggered_receipt_consumes_both():
    text = "【改命回天】\n【推命命中】\n【改命待发】此道改命尚可维持 22小时38分钟\n你卷回四级妖丹，本次未损修为。"
    result = group_result("rift", text)
    assert result["status"] == "rescued"
    assert result["fate"] == {"prediction_consumed": True, "protection_consumed": True}
    board = apply_fate(parse_board(BOARD, NOW), result)
    assert not active(board, "prediction", NOW)
    assert not active(board, "protection", NOW)
    assert board["points"] == 151


def test_normal_receipt_does_not_extend_protection_lifetime():
    board = parse_board(BOARD, NOW)
    before = copy.deepcopy(board["protection"])
    result = group_result("rift", "【探寻成功】\n命盘【贪狼】照命\n【推命命中】\n【改命待发】23小时59分钟\n满载而归，为你带来了火！")
    after = apply_fate(board, result)
    assert after["protection"] == before
    assert required_preparation(after, "rift", NOW+60) == [".推命 探索"]


def test_absent_receipt_or_flag_is_not_negative_evidence():
    assert fate_result("")["protection_consumed"] is None
    assert fate_result("", protected=False, normal_complete=True)["protection_consumed"] is False
    assert fate_result("", protected=True)["protection_consumed"] is True
    assert group_result("node", "【虚空漫游】收集了一些【虚空尘埃】x2，也算不虚此行。")["fate"]["prediction_consumed"] is None


def test_cooldown_does_not_count_as_execution():
    result = group_result("rift", "空间裂缝尚未稳定，请在 1小时14分钟6秒 后再行探寻。")
    assert result == {"status": "cooldown", "remaining_seconds": 4446, "confirmed": False}


def test_confirmation_requires_route_and_exact_success():
    board = parse_board(BOARD, NOW)
    with pytest.raises(ExplorationError):
        accept_preparation(board, ".改命 探索", "你已有斗法改命，无法预留。", NOW)
    board = accept_preparation(board, ".推命 探索", "你拨动司命盘，为 【探索】 推下一段命数。\n此推命将在 8 小时 内生效。", NOW)
    assert active(board, "prediction", NOW)
    assert board["protection"]["expires_at"] < NOW + 24*3600


def test_midnight_is_shanghai_and_does_not_reset_rift_timer():
    assert day(NOW) == "2026-10-15"
    assert datetime.fromtimestamp(midnight(NOW), TZ).isoformat() == "2026-10-16T00:00:00+08:00"


def test_invalid_board_not_treated_as_empty_effects():
    with pytest.raises(ExplorationError):
        parse_board("【天机盘】未知格式", NOW)
