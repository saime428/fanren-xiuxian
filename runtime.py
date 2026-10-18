#!/usr/bin/env python3
from __future__ import annotations

import builtins
import os
from datetime import datetime

_ORIGINAL_PRINT = builtins.print


def log_print(*args: object, **kwargs: object) -> None:
    prefix = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    kwargs.setdefault("flush", True)
    _ORIGINAL_PRINT(prefix, *args, **kwargs)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise SystemExit(f"Missing required env: {name}")
    return str(value) if value is not None else ""
