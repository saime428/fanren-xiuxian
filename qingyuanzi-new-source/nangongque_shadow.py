#!/usr/bin/env python3
"""Network-free validator and captured-fixture replay for Nangongque."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nangongque_protocol import coordinator_check, replay_fixture

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "nangongque_config.json")
    parser.add_argument("--state-root", type=Path, default=ROOT / "state")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        fixture = json.loads(args.replay.read_text(encoding="utf-8"))
        result = replay_fixture(fixture, state_root=args.state_root)
    else:
        result = coordinator_check(args.config, state_root=args.state_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
