#!/usr/bin/env python3
"""Hermes-native runtime path resolver; no credentials or side effects."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("XIU_PROJECT_DIR", Path(__file__).resolve().parents[1])).resolve()
STATE_ROOT = Path(os.environ.get("XIU_STATE_DIR", PROJECT_ROOT / "state")).resolve()
WORKSPACE_ROOT = Path(os.environ.get("XIU_WORKSPACE_DIR", PROJECT_ROOT.parent.parent)).resolve()

__all__ = ["PROJECT_ROOT", "STATE_ROOT", "WORKSPACE_ROOT"]
