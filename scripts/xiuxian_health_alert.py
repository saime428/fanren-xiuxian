#!/usr/bin/env python3
"""Emit only new, actionable Xiuxian health failures.

The canonical health snapshot remains complete.  This module is only the
notification policy used by the script-only Hermes cron job.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Bad notes quote the newest log line, which starts with log_print's stamp.
_LOG_TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")

def actionable_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    # The collector owns severity, including fresh errors on an otherwise idle
    # or cooling-down flow. A status label must not hide that evidence.
    items = [item for item in snapshot.get("active_items", [])
             if isinstance(item, dict) and item.get("severity") == "bad"]
    # A missing registry/state root cannot produce per-flow rows, but is still
    # an actionable collector failure in the canonical summary.
    for issue in (snapshot.get("summary") or {}).get("top_issues", []):
        if (isinstance(issue, dict) and issue.get("severity") == "bad"
                and issue.get("kind") in {"active_config", "state_root"}):
            items.append({"account": "system", "flow": issue["kind"], "status": "check_failed", **issue})
    return items


def fingerprint(item: dict[str, Any]) -> str:
    fields = (
        item.get("account") or item.get("account_slug") or "unknown",
        item.get("flow") or item.get("name") or "unknown",
        item.get("kind") or "check",
        item.get("status") or "bad",
        _LOG_TIMESTAMP.sub("", str(item.get("note") or item.get("recent_log_error") or "")),
    )
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def load_previous(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    values = payload.get("active_actionable_fingerprints", []) if isinstance(payload, dict) else []
    return {value for value in values if isinstance(value, str)}


def write_state_atomic(path: Path, fingerprints: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"active_actionable_fingerprints": sorted(fingerprints)},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_alert(snapshot: dict[str, Any], new_items: list[dict[str, Any]]) -> str:
    if not new_items:
        return ""
    summary = snapshot.get("summary", {})
    counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    for item in new_items:
        account = item.get("account") or item.get("account_slug") or "unknown"
        flow = item.get("flow") or item.get("name") or "unknown"
        kind = item.get("kind") or "check"
        status = item.get("status") or "bad"
        note = " ".join(str(item.get("note") or item.get("recent_log_error") or "").split())[:240]
        grouped.setdefault((flow, kind, status, note), []).append(account)
    details = []
    for (flow, kind, status, note), accounts in grouped.items():
        detail = f"{','.join(dict.fromkeys(accounts))}/{flow} [{kind}] = {status}"
        if note and note != status:
            detail += f" ({note})"
        details.append(detail)
    return (
        "Xiuxian active healthcheck alert: "
        f"overall_status={summary.get('overall_status')}, ok={counts.get('ok')}, "
        f"warn={counts.get('warn')}, bad={counts.get('bad')}; "
        + "; ".join(details)
    )


def notify(snapshot_path: Path, notification_state_path: Path,
           confirmation_snapshot_path: Path | None = None) -> str:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    current_items = actionable_items(snapshot)
    current = {fingerprint(item) for item in current_items}
    previous = load_previous(notification_state_path)
    if confirmation_snapshot_path is None:
        first = current
    else:
        first_snapshot = json.loads(confirmation_snapshot_path.read_text(encoding="utf-8"))
        first = {fingerprint(item) for item in actionable_items(first_snapshot)}
    confirmed = first & current
    new_items = [item for item in current_items if fingerprint(item) in confirmed - previous]
    # A one-pass failure must not be recorded as notified. Keep an existing
    # notification through one-pass recovery, clearing it only after two clean
    # observations, so flapping cannot repeatedly re-arm the same alert.
    write_state_atomic(notification_state_path, (previous & (first | current)) | confirmed)
    return build_alert(snapshot, new_items)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("notification_state", type=Path)
    parser.add_argument("--confirmation-snapshot", type=Path)
    args = parser.parse_args()
    message = notify(args.snapshot, args.notification_state, args.confirmation_snapshot)
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
