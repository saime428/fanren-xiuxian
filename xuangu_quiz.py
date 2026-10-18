"""Xuangu question and result parsing; no Telegram side effects."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

FLOW = "xuangu_zuoda"
RETIRED_FLOWS = frozenset({"zuoda_a", "zuoda_dingwai_qianlanbingyan"})
MAX_AGE_SECONDS = 300
RESULT_GRACE_SECONDS = 60
COMMANDS = frozenset(f".作答 {letter}" for letter in "ABCD")
PENDING = frozenset({"awaiting_answer_result", "answer_result_unconfirmed"})
TERMINAL_RESULTS = frozenset({"answer_confirmed", "answer_incorrect", "answer_timed_out", "answer_already_observed"})
BUSINESS_ERRORS = frozenset({"answer_result_unconfirmed", "answer_incorrect", "answer_timed_out"})
OPTION_RE = re.compile(r"^\s*([A-D])\s*[.．、:：]\s*(.+)$", re.M)
VARIANTS = frozenset({"考校", "窥鼎", "夺焰"})
_VARIANT_PATTERN = r"考校|窥鼎|窺鼎|夺焰|奪焰"
_HEADER_VARIANT_RE = re.compile(
    r"\A【(?:天[机機][异異]象\s*[·・]\s*)?玄骨(?P<variant>" + _VARIANT_PATTERN + r")(?:\s*[·・][^】\n]*)?\s*】")
_NOTICE_RE = re.compile(r"\A【天[机機][异異]象\s*[·・]\s*玄骨(?:" + _VARIANT_PATTERN + r")\s*】")

# Reviewed question/answer pairs from Waaiging/fanrenxiuxian_auto's evidenced
# bank at 04d72e9, plus the existing saved short wording. Keep runtime data here,
# independent of the reference checkout. Source links are in the review doc.
# Both the event variant and full stem must match: similar flame questions have
# different answers even when they offer the same options.
SEED_QUESTIONS = {
    "考校": (
        (("乾蓝冰焰在原著中最初是封附在何种通天灵宝之外？",
          "乾蓝冰焰最初封附在何种通天灵宝之外？"), ("虚天鼎",)),
        (("按原著路线，乾蓝冰焰何时才适合开始从虚天鼎上缓慢炼出？",), ("元婴后",)),
        (("玄骨妄图炼化乾蓝冰焰，是想将其修成什么魔焰？",), ("修罗圣火",)),
        (("玄骨老魔最初真正惦记的，并不是虚天鼎本身，而是什么？",), ("鼎外乾蓝冰焰",)),
        (("韩立在内殿对付玄骨时，最能克制魔修的手段是什么？",), ("辟邪神雷",)),
        (("韩立能把虚天鼎从乾蓝冰焰池中拉出的关键倚仗是什么？",), ("血玉蜘蛛",)),
    ),
    "窥鼎": (
        (("原著路线里，乾蓝冰焰是何时才适合从虚天鼎上开始缓慢炼化？",), ("元婴后",)),
        (("按原著流程，虚天鼎与乾蓝冰焰的关系最贴切的说法是？",), ("先得虚天鼎，后把乾蓝自鼎上抽离",)),
        (("玄骨窥视虚天鼎时，真正惦记的并不是鼎本身，而是什么？",), ("鼎外乾蓝冰焰",)),
        (("韩立真正得到独立法宝【乾蓝冰焰】之前，首先拿到的是什么？",), ("虚天鼎",)),
    ),
    "夺焰": (
        (("下列哪一项更符合乾蓝冰焰后期化极所需的辅引思路？",), ("冰凤寒性与偏火残焰同炉",)),
        (("乾蓝冰焰从虚天鼎上抽离后，在玩法上应被视作什么？",), ("独立法宝",)),
        (("乾蓝冰焰后续化极时，要冲击蜕变成什么更凶的焰相？",), ("紫罗极火",)),
        (("玄骨上人的化焰思路，核心在于先压住什么风险？",), ("鼎焰反噬",)),
    ),
}
_CHARACTERS = str.maketrans({
    "虛": "虚", "藍": "蓝", "靈": "灵", "寶": "宝", "時": "时", "適": "适",
    "緩": "缓", "煉": "炼", "韓": "韩", "內": "内", "對": "对", "麼": "么",
    "關": "关", "鍵": "键", "嬰": "婴", "後": "后", "種": "种", "從": "从",
    "開": "开", "風": "风", "線": "线", "來": "来", "練": "练",
    "裏": "里", "裡": "里",
    "圖": "图", "將": "将", "羅": "罗", "聖": "圣", "記": "记", "窺": "窥",
    "視": "视", "與": "与", "係": "系", "貼": "贴", "說": "说", "離": "离",
    "獨": "独", "項": "项", "極": "极", "輔": "辅", "鳳": "凤", "殘": "残",
    "爐": "炉", "應": "应", "續": "续", "衝": "冲", "擊": "击", "蛻": "蜕",
    "變": "变", "兇": "凶", "於": "于", "壓": "压", "險": "险",
})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_CHARACTERS)
    return re.sub(r"[\s·・,，。.!！?？:：;；、()（）【】\[\]《》<>〈〉'\"“”‘’_-]+", "", text).casefold()


def event_variant(text: str) -> str | None:
    text = (text or "").strip()
    variants = set()
    header = _HEADER_VARIANT_RE.match(text)
    if header:
        variants.add(header["variant"].replace("窺", "窥").replace("奪", "夺"))
    for line in text.splitlines():
        line = line.lstrip()
        if line.startswith(("神念直入脑海", "神念直入腦海")):
            variants.add("考校")
        elif line.startswith("那道魔念"):
            variants.add("窥鼎")
        elif line.startswith(("一缕魔念直逼", "一縷魔念直逼")):
            variants.add("夺焰")
    # A conflicting heading and introduction is not a reliable branch signal.
    return next(iter(variants)) if len(variants) == 1 else None


def notice_variant(text: str) -> str | None:
    return event_variant(text) if _NOTICE_RE.match((text or "").strip()) else None


def question_key(variant: str, stem: str) -> str:
    return f"{variant}:{normalize(stem)}"


def command(text: str) -> str | None:
    match = re.fullmatch(r"\s*\.作答\s+([A-D])\s*", text or "", re.I)
    return f".作答 {match.group(1).upper()}" if match else None


def parse_question(text: str) -> dict | None:
    options = OPTION_RE.findall(text or "")
    if len(options) != 4 or {code for code, _ in options} != set("ABCD"):
        return None
    if not any(marker in text for marker in ("玄骨", "神念直入脑海", "神念直入腦海", "那道魔念")):
        return None
    prefix = text[:OPTION_RE.search(text).start()].strip()
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", prefix)
    if len(quoted) == 1:
        stem = quoted[0].strip()
    else:
        parts = re.split(r"(?:提问|提問|发问|發問|炸响|炸響)[：:]\s*", prefix, maxsplit=1)
        stem = parts[-1].strip()
    if not stem or "@" in stem:
        return None
    variant = event_variant(prefix)
    if variant is None:
        return None
    return {"question": stem, "key": question_key(variant, stem), "variant": variant,
            "options": {code: option.strip() for code, option in options}}


def parse_answer_bank(text: str) -> list[str]:
    """TG_ANSWER_BANK_TEXT: answers separated by "|" or newlines."""
    return [part.strip() for part in re.split(r"[|\n]", text or "") if part.strip()]


def load_answer_bank_file(path: Path) -> list[str]:
    """The "answers" list of an answer-bank JSON file; missing or invalid reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    answers = data.get("answers") if isinstance(data, dict) else None
    return [str(item).strip() for item in answers if str(item).strip()] if isinstance(answers, list) else []


def merge_answer_bank(base, extra) -> list[str]:
    """Both lists in order, without blanks or answers equal under normalize()."""
    merged: list[str] = []
    seen: set[str] = set()
    for answer in [*base, *extra]:
        key = normalize(answer)
        if answer.strip() and key not in seen:
            seen.add(key)
            merged.append(answer.strip())
    return merged


def resolve_answer(text: str, allowed_answers, learned_questions: dict) -> str | None:
    question = parse_question(text)
    if not question:
        return None
    allowed = {normalize(answer) for answer in allowed_answers}
    known = set()
    for stems, answers in SEED_QUESTIONS.get(question["variant"], ()):
        if question["key"] in {question_key(question["variant"], stem) for stem in stems}:
            known.update(normalize(answer) for answer in answers if normalize(answer) in allowed)
    known.update(normalize(answer) for answer in learned_questions.get(question["key"], []))
    matches = [code for code, option in question["options"].items() if normalize(option) in known]
    return f".作答 {matches[0]}" if len(matches) == 1 else None


def parse_result(text: str) -> dict | None:
    text = (text or "").strip()
    header = re.match(r"\A【玄骨(?P<variant>考校|窥鼎|窺鼎|夺焰|奪焰)\s*[·・]\s*(?P<outcome>答对|答對|答错|答錯|超时|超時)】\s*\n", text)
    if not header:
        return None
    body = text[header.end():]
    owners = re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)(?![A-Za-z0-9_])", body)
    if len(owners) != 1 or not body.startswith("@" + owners[0]):
        return None
    outcome = {"答对": "correct", "答對": "correct", "答错": "incorrect", "答錯": "incorrect",
               "超时": "timeout", "超時": "timeout"}[header["outcome"]]
    option = re.search(r"答案\s*([A-D])(?![A-Za-z0-9_])", body)
    if outcome == "correct" and not re.match(
            r"@" + re.escape(owners[0]) + r"\s*的答案\s*[A-D]\s*完全正[确確][！!]", body):
        return None
    return {"username": owners[0], "variant": header["variant"].replace("窺", "窥").replace("奪", "夺"),
            "outcome": outcome, "option": option[1] if option else None}
