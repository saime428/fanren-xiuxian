#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "accounts"
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "state" / "xiuxian-log-retention-plan.json"
PER_RUN_PREFIXES = ("all-run-",)


def has_symlink_component(path: Path, *, max_levels: int = 2) -> bool:
    current = path if path.is_absolute() else Path.cwd() / path
    for _ in range(max_levels):
        if current.is_symlink():
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def build_retention_plan(
    state_root: Path,
    *,
    rolling_limit_bytes: int,
    rolling_keep_bytes: int,
    per_run_keep_count: int,
    open_paths: set[Path] | None = None,
    open_detection_status: str = "ok",
    trusted_root: Path | None = None,
) -> dict[str, Any]:
    if rolling_limit_bytes < 0 or rolling_keep_bytes < 0 or per_run_keep_count < 0:
        raise ValueError('retention parameters must be non-negative')
    rolling_logs: list[dict[str, Any]] = []
    per_run_logs: list[dict[str, Any]] = []
    skipped_symlinks: list[str] = []
    open_paths = {path.resolve() for path in (open_paths or set())}

    estimate_known = open_detection_status == "ok"
    if state_root.is_symlink():
        return {
            "state_root": str(state_root),
            "root_status": "rejected_symlink",
            "rolling_logs": [],
            "per_run_logs": [],
            "skipped_symlinks": [str(state_root)],
            "open_file_detection": open_detection_status,
            "estimated_reclaim_bytes": None,
        }
    boundary = trusted_root or state_root.parent
    try:
        normalized_state_root = state_root.resolve(strict=False)
        normalized_boundary = boundary.resolve(strict=False)
        normalized_state_root.relative_to(normalized_boundary)
    except (OSError, ValueError):
        return {
            "state_root": str(state_root),
            "root_status": "rejected_outside_trusted_root",
            "rolling_logs": [],
            "per_run_logs": [],
            "skipped_symlinks": [],
            "open_file_detection": open_detection_status,
            "estimated_reclaim_bytes": None,
        }
    current = state_root.parent
    ancestor_symlink = False
    while True:
        if current.is_symlink():
            ancestor_symlink = True
            break
        if current == boundary or current.parent == current:
            break
        current = current.parent
    if ancestor_symlink:
        return {
            "state_root": str(state_root),
            "root_status": "rejected_symlink_ancestor",
            "rolling_logs": [],
            "per_run_logs": [],
            "skipped_symlinks": [str(state_root)],
            "open_file_detection": open_detection_status,
            "estimated_reclaim_bytes": None,
        }
    if not state_root.exists():
        return {
            "state_root": str(state_root),
            "root_status": "missing",
            "rolling_logs": rolling_logs,
            "per_run_logs": per_run_logs,
            "skipped_symlinks": skipped_symlinks,
            "open_file_detection": open_detection_status,
            "estimated_reclaim_bytes": 0 if estimate_known else None,
        }
    if not state_root.is_dir():
        return {
            "state_root": str(state_root),
            "root_status": "not_directory",
            "rolling_logs": [],
            "per_run_logs": [],
            "skipped_symlinks": [],
            "open_file_detection": open_detection_status,
            "estimated_reclaim_bytes": None,
        }

    for path in state_root.rglob("*.log"):
        if path.is_symlink():
            skipped_symlinks.append(str(path))
            continue
        if path.name.startswith(PER_RUN_PREFIXES):
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        size = stat_result.st_size
        if size <= rolling_limit_bytes:
            continue
        resolved = path.resolve()
        is_open = resolved in open_paths
        rolling_logs.append(
            {
                "path": str(path),
                "size_bytes": size,
                "mtime_ns": stat_result.st_mtime_ns,
                "inode": stat_result.st_ino,
                "keep_bytes": min(rolling_keep_bytes, size),
                "open_detected": is_open,
                "estimated_reclaim_bytes": (
                    None if not estimate_known else 0 if is_open else max(0, size - rolling_keep_bytes)
                ),
                "action": "retain_tail",
            }
        )

    for path in state_root.rglob('*'):
        if path.is_symlink():
            skipped_symlinks.append(str(path))

    flow_dirs: list[Path] = []
    root_resolved = state_root.resolve()
    for account_dir in state_root.iterdir():
        if account_dir.is_symlink():
            skipped_symlinks.append(str(account_dir))
            continue
        if not account_dir.is_dir():
            continue
        for flow_dir in account_dir.iterdir():
            if flow_dir.is_symlink():
                skipped_symlinks.append(str(flow_dir))
                continue
            if not flow_dir.is_dir():
                continue
            try:
                flow_dir.resolve().relative_to(root_resolved)
            except (OSError, ValueError):
                skipped_symlinks.append(str(flow_dir))
                continue
            flow_dirs.append(flow_dir)

    for flow_dir in sorted(flow_dirs):
        candidates = sorted(
            (
                path
                for prefix in PER_RUN_PREFIXES
                for path in flow_dir.glob(f"{prefix}*.log")
                if not path.is_symlink() and path.is_file()
            ),
            key=lambda path: safe_mtime(path),
            reverse=True,
        )
        for path in candidates[max(0, per_run_keep_count):]:
            try:
                stat_result = path.stat()
            except OSError:
                continue
            resolved = path.resolve()
            is_open = resolved in open_paths
            per_run_logs.append(
                {
                    "path": str(path),
                    "size_bytes": stat_result.st_size,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "inode": stat_result.st_ino,
                    "open_detected": is_open,
                    "estimated_reclaim_bytes": None if not estimate_known else 0 if is_open else stat_result.st_size,
                    "action": "remove_old_per_run_log",
                }
            )

    rolling_logs.sort(key=lambda item: item["estimated_reclaim_bytes"] or 0, reverse=True)
    per_run_logs.sort(key=lambda item: item["path"])
    estimated_reclaim = (
        sum(item["estimated_reclaim_bytes"] or 0 for item in rolling_logs + per_run_logs)
        if estimate_known
        else None
    )
    return {
        "state_root": str(state_root),
        "root_status": "ok",
        "policy": {
            "rolling_limit_bytes": rolling_limit_bytes,
            "rolling_keep_bytes": rolling_keep_bytes,
            "per_run_keep_count": per_run_keep_count,
        },
        "rolling_logs": rolling_logs,
        "per_run_logs": per_run_logs,
        "skipped_symlinks": sorted(set(skipped_symlinks)),
        "open_file_detection": open_detection_status,
        "estimated_reclaim_bytes": estimated_reclaim,
    }


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float('-inf')


def collect_open_log_paths(state_root: Path) -> tuple[set[Path], str]:
    try:
        result = subprocess.run(
            ["lsof", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return set(), "unavailable"
    except subprocess.TimeoutExpired:
        return set(), "timeout"
    except OSError:
        return set(), "error"
    if result.returncode != 0:
        return set(), f"nonzero_exit:{result.returncode}"
    root = state_root.resolve()
    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        if not line.startswith('n'):
            continue
        candidate = Path(line[1:])
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.suffix == '.log':
            paths.add(resolved)
    return paths, "ok"


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError('value must be non-negative')
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a dry-run Xiuxian log retention plan. No files are deleted.")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--rolling-limit-mib", type=non_negative_int, default=25)
    parser.add_argument("--rolling-keep-mib", type=non_negative_int, default=10)
    parser.add_argument("--per-run-keep-count", type=non_negative_int, default=100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    open_paths, open_detection_status = collect_open_log_paths(args.state_root)
    plan = build_retention_plan(
        args.state_root,
        rolling_limit_bytes=args.rolling_limit_mib * 1024 * 1024,
        rolling_keep_bytes=args.rolling_keep_mib * 1024 * 1024,
        per_run_keep_count=args.per_run_keep_count,
        open_paths=open_paths,
        open_detection_status=open_detection_status,
        trusted_root=PROJECT_ROOT,
    )
    write_json_atomic(args.output, plan)

    print(
        json.dumps(
            {
                "mode": "dry_run_only",
                "rolling_log_count": len(plan["rolling_logs"]),
                "old_per_run_log_count": len(plan["per_run_logs"]),
                "estimated_reclaim_bytes": plan["estimated_reclaim_bytes"],
                "estimated_reclaim": (
                    format_bytes(plan["estimated_reclaim_bytes"])
                    if plan["estimated_reclaim_bytes"] is not None
                    else "unknown"
                ),
                "open_file_detection": plan["open_file_detection"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
