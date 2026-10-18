#!/usr/bin/env python3
"""Read-only analyzer for Cangkun route choices and rare rewards."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REWARDS = ("残缺阴环", "大衍诀残篇·千机")
ROUTE_PATTERNS = {
    "first": (("匿踪潜行", "1 匿踪潜行"),),
    "third_scroll": (("先取卷轴",),),
    "third_side": (("先探偏殿",),),
    "fourth_betray": (("背盟", "背盟得手"),),
    "fifth_escape": (("夺图先遁", "2 夺图"),),
    "fifth_greed": (("贪线", "3 贪"),),
}
ROOM_RE = re.compile(r"(?:房间|room)[^0-9]{0,8}(\d{2,})", re.I)


def classify(text: str) -> dict[str, bool]:
    return {
        key: any(any(token in text for token in alternatives) for alternatives in groups)
        for key, groups in ROUTE_PATTERNS.items()
    }


def route_label(flags: dict[str, bool]) -> str:
    parts = []
    if flags.get("first"):
        parts.append("1匿踪")
    if flags.get("third_scroll"):
        parts.append("1卷轴")
    elif flags.get("third_side"):
        parts.append("3偏殿")
    if flags.get("fourth_betray"):
        parts.append("背盟")
    if flags.get("fifth_escape"):
        parts.append("2夺图")
    elif flags.get("fifth_greed"):
        parts.append("3贪线")
    return "→".join(parts) or "unknown"


def iter_text(path: Path):
    if path.suffix == ".jsonl":
        for line in path.open(encoding="utf-8", errors="replace"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = line
            if isinstance(value, dict):
                yield json.dumps(value, ensure_ascii=False)
            else:
                yield str(value)
    else:
        for line in path.open(encoding="utf-8", errors="replace"):
            yield line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="state")
    parser.add_argument("--output", default="state/cangkun/route-reward-stats.json")
    args = parser.parse_args()
    root = Path(args.root)
    output = Path(args.output)
    rooms: dict[str, dict] = defaultdict(lambda: {"flags": {}, "rewards": Counter(), "sources": set()})
    scanned_files = matched_lines = 0
    for pattern in ("*.log", "*.jsonl"):
        for path in root.rglob(pattern):
            if not path.is_file() or path == output:
                continue
            scanned_files += 1
            for text in iter_text(path):
                if "苍坤" not in text and not any(reward in text for reward in REWARDS):
                    continue
                flags = classify(text)
                reward_hits = [reward for reward in REWARDS if reward in text]
                if not any(flags.values()) and not reward_hits:
                    continue
                matched_lines += 1
                match = ROOM_RE.search(text)
                key = match.group(1) if match else f"unscoped:{path}"
                room = rooms[key]
                room["flags"].update({name: True for name, hit in flags.items() if hit})
                room["rewards"].update(reward_hits)
                room["sources"].add(str(path))
    by_route: dict[str, dict] = defaultdict(lambda: {"rooms": 0, "rewards": Counter()})
    serialized_rooms = []
    for room_id, room in rooms.items():
        label = route_label(room["flags"])
        by_route[label]["rooms"] += 1
        by_route[label]["rewards"].update(room["rewards"])
        serialized_rooms.append({
            "room": room_id,
            "route": label,
            "rewards": dict(room["rewards"]),
            "sources": sorted(room["sources"]),
        })
    payload = {
        "scanned_files": scanned_files,
        "matched_lines": matched_lines,
        "rooms": sorted(serialized_rooms, key=lambda item: item["room"]),
        "by_route": {
            route: {"rooms": value["rooms"], "rewards": dict(value["rewards"])}
            for route, value in sorted(by_route.items())
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
