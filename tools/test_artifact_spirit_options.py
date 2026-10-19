"""器灵法宝做成勾选框：从天机阁资料识别带器灵的法宝，配置里的分成「勾上的」和「手填的」。

run: PYTHONPATH=app/src python tools/test_artifact_spirit_options.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.web.app_helpers import templates  # noqa: E402
from tg_game.web.module_availability import (  # noqa: E402
    list_artifact_spirits,
    payload_has_artifact_spirit,
    split_artifact_choice,
)

PAYLOAD = {"inventory": {"items": [
    {"name": "金光砖", "type": "treasure"},
    {"name": "青竹蜂云剑（神雷版）", "type": "treasure", "spirit": {"name": "剑灵", "level": 13}},
    {"name": "玄天斩灵剑", "type": "treasure", "spirit": {"name": "玄霜", "level": 6}},
    {"name": "玄天斩灵剑", "type": "treasure", "spirit": {"name": "玄霜", "level": 6}},
    {"name": "空器灵", "type": "treasure", "spirit": {}},
]}}
OPTIONS = ["青竹蜂云剑（神雷版）", "玄天斩灵剑"]


def check_detection():
    spirits = list_artifact_spirits(PAYLOAD)
    assert [s["name"] for s in spirits] == OPTIONS, spirits
    assert spirits[0] == {"name": "青竹蜂云剑（神雷版）", "spirit": "剑灵", "level": 13}
    assert payload_has_artifact_spirit(PAYLOAD)
    assert list_artifact_spirits({"inventory": {"items": [{"name": "金光砖"}]}}) == []
    assert not payload_has_artifact_spirit(None)


def check_split():
    # 抚摸存的是整条指令；名字在选项里的勾上，其余留给手填框
    assert split_artifact_choice(".抚摸法宝 玄天斩灵剑\n.抚摸法宝 某剑", OPTIONS) == (["玄天斩灵剑"], "某剑")
    # 「青竹蜂云剑」不是「青竹蜂云剑（神雷版）」，不能被当成勾上
    assert split_artifact_choice("青竹蜂云剑、玄天斩灵剑", OPTIONS) == (["玄天斩灵剑"], "青竹蜂云剑")
    assert split_artifact_choice("", OPTIONS) == ([], "")


def render(options, current, active=False):
    macro = templates.env.from_string(
        "{% from 'modules/artifact_spirit_picker.html' import spirit_picker %}"
        "{{ spirit_picker(options, current, 'command_text', '抚摸法宝', active) }}"
    )
    return macro.render(options=options, current=current, active=active)


def check_render():
    html = render(list_artifact_spirits(PAYLOAD), ".抚摸法宝 玄天斩灵剑、某剑")
    assert html.count('type="checkbox" name="targets"') == 2, html
    assert 'value="玄天斩灵剑" checked' in html and 'value="青竹蜂云剑（神雷版）" checked' not in html
    assert '剑灵 Lv13' in html and 'name="command_text" value="某剑"' in html
    assert 'required' not in html  # 有勾选框时手填可空
    running = render(list_artifact_spirits(PAYLOAD), "玄天斩灵剑", active=True)
    assert running.count("disabled") == 2 and "readonly" in running
    plain = render([], "玄天斩灵剑")  # 没识别到器灵：退回原来的必填文本框
    assert 'type="checkbox"' not in plain and 'value="玄天斩灵剑"' in plain and "required" in plain


if __name__ == "__main__":
    check_detection()
    check_split()
    check_render()
    print("ok")
