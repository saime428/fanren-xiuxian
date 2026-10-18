#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

WORKSPACE = Path(__import__('os').environ.get('XIU_WORKSPACE_DIR', '/Users/operator/.hermes/workspace'))
STATUS_PATH = Path(__import__('os').environ.get('XIU_HEALTHCHECK_OUTPUT', str(WORKSPACE / 'state' / 'xiuxian-healthcheck.json')))


def main() -> int:
    payload = json.loads(STATUS_PATH.read_text(encoding='utf-8'))
    summary = payload.get('summary') or {}
    items = payload.get('active_items') or []
    overall = summary.get('overall_status', 'unknown')
    counts = summary.get('counts') or {}
    top_issues = [item for item in items if item.get('severity') != 'ok'][:3]

    lines = []
    lines.append(f"Xiuxian(active): {overall}")
    lines.append(f"Counts: ok={counts.get('ok', 0)} warn={counts.get('warn', 0)} bad={counts.get('bad', 0)}")
    lines.append(f"Active checks: {summary.get('active_flow_count', 0)}")
    lines.append(f"Ignored legacy: {summary.get('ignored_legacy_count', 0)}")
    lines.append(f"Checked: {summary.get('checked_at', 'unknown')}")
    if top_issues:
        first = top_issues[0]
        lines.append(f"Top: {first.get('account')}/{first.get('flow')} [{first.get('kind')}] = {first.get('status')}")
    else:
        lines.append('Top: none')
    print('\n'.join(lines[:6]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
