"""Run a migrated command through the existing account sender schedule."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from miniapp_shared.accounts import ALL_ACCOUNTS
from miniapp_shared.dwelling import error_code
from .catalog import FLOW_COMMANDS, migrated
from .runner import run_flow

ROOT = Path(__file__).resolve().parents[1]
SUCCESSFUL_RUNS = {"active", "started", "success", "settled", "checked_in", "already_checked_in", "no_sect",
                   "cooldown", "manual_retreat", "legacy_wait", "scheduled_wait", "paused", "lock_busy",
                   "transport_busy", "yielding_to_qyz", "read_only", "event_already_complete", "settled_failure"}


def result_exit_code(result, *, observer_immediate=False):
    if observer_immediate and result.get("status") in {"lock_busy", "transport_busy"}:
        return 75  # Preserve the original observer's retryable lease outcome.
    return 0 if result.get("status") in SUCCESSFUL_RUNS else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", choices=ALL_ACCOUNTS, required=True)
    parser.add_argument("--flow", choices=FLOW_COMMANDS, required=True)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not migrated(ROOT, args.account, args.flow):
            return 3  # Only an explicitly unselected route can use its legacy wrapper.
        result = asyncio.run(run_flow(ROOT, args.account, args.flow,
                            read_only=args.read_only or os.environ.get("TG_DRY_RUN") == "1"
                            or os.environ.get("TG_RECONCILE_ONLY") == "1"))
    except Exception as exc:
        print(json.dumps({"status": "configuration_error", "error": error_code(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result_exit_code(result, observer_immediate=os.environ.get("TG_OBSERVER_IMMEDIATE_KICK") == "1")


if __name__ == "__main__":
    raise SystemExit(main())
