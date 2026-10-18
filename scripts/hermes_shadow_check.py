#!/usr/bin/env python3
"""Offline source/packaging gate for the canonical Xiuxian checkout.

No Telegram, MiniApp, browser, launchctl mutation, or production state access.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import time
from pathlib import Path

EXCLUDED_DIRS = {".git", ".venv", ".pytest_cache", "state", "disabled", "__pycache__", "archive", "references"}
SENSITIVE_NAMES = {".env", "auth.json"}
SENSITIVE_SUFFIXES = {".session", ".session-journal"}


def source_files(root: Path):
    # Git's inventory excludes live sessions/state and ignored local secrets.
    # Include new, untracked source files while a reviewed change is in flight.
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, timeout=10,
    )
    for name in sorted(set(os.fsdecode(output).split("\0")) - {""}):
        path = Path(name)
        if not EXCLUDED_DIRS.intersection(path.parts) and (root / path).is_file():
            yield root / path


def tracked_runtime_files(root: Path) -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, timeout=10)
    return sorted(name for name in os.fsdecode(output).split("\0") if name and (
        "state" in Path(name).parts or Path(name).name in SENSITIVE_NAMES
        or (Path(name).name.startswith(".env.") and not Path(name).name.endswith(".example"))
        or (Path(name).suffix == ".env" and not Path(name).name.endswith(".example"))
        # Account credentials and their .bak/.orig/.swp copies; only the template is source.
        or (Path(name).parent.as_posix() == "config/accounts" and Path(name).name != "account.env.example")
        or any(Path(name).name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    started = time.time()
    issues: list[dict] = []
    try:
        issues.extend({"kind": "tracked_runtime_file", "path": name} for name in tracked_runtime_files(root))
        sources = list(source_files(root))
    except (OSError, subprocess.SubprocessError) as exc:
        sources = []
        issues.append({"kind": "git_inventory_unavailable", "error_type": type(exc).__name__})
    counts = {"python": 0, "shell": 0, "plist": 0}
    legacy_hits = []
    pattern = re.compile(r"/Users/operator/\.openclaw/workspace/xiuxian|ai\.openclaw\.xiuxian")
    for path in sources:
        rel = str(path.relative_to(root))
        if path.name in SENSITIVE_NAMES or any(path.name.endswith(s) for s in SENSITIVE_SUFFIXES):
            continue
        if path.suffix == ".py":
            counts["python"] += 1
            try:
                compile(path.read_bytes(), str(path), "exec")
            except Exception as exc:
                issues.append({"kind": "python_compile", "path": rel, "error": str(exc)[:300]})
        elif path.suffix == ".sh":
            counts["shell"] += 1
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=10)
            if proc.returncode:
                issues.append({"kind": "shell_syntax", "path": rel, "error": proc.stderr[:300]})
        elif path.suffix == ".plist":
            counts["plist"] += 1
            try:
                payload = plistlib.loads(path.read_bytes())
                # A migration explanation or regression fixture may mention an
                # old path; only actual scheduler launch paths must reject it.
                launch = json.dumps({key: payload.get(key) for key in ("Label", "ProgramArguments", "Program", "WorkingDirectory")})
                if pattern.search(launch):
                    legacy_hits.append(rel)
            except Exception as exc:
                issues.append({"kind": "plist_parse", "path": rel, "error": str(exc)[:300]})

    status = "healthy" if not issues and not legacy_hits else "bad"
    payload = {
        "status": status,
        "non_live": True,
        "root": str(root),
        "generated_at": time.time(),
        "started_at": started,
        "duration_seconds": time.time() - started,
        "counts": counts,
        "issues": issues,
        "legacy_runtime_files": sorted(legacy_hits),
        "safety": "offline static shadow only; no send, observer, browser, protocol, or launchd mutation",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"status": status, "issues": len(issues), "legacy_runtime_files": len(legacy_hits)}))
    return 0 if status == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(main())
