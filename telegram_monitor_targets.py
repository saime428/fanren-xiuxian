"""Shared Telegram multi-group target parsing for Xiuxian listeners."""
from __future__ import annotations


def parse_monitor_targets(raw: str) -> list[str | int]:
    """Parse a pipe/comma-separated target list, preserving order and deduping."""
    values: list[str | int] = []
    seen: set[str] = set()
    for item in str(raw or "").replace(",", "|").split("|"):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        try:
            values.append(int(value))
        except ValueError:
            values.append(value)
    return values


def monitor_targets(raw: str, primary: str | int) -> list[str | int]:
    values = parse_monitor_targets(raw)
    primary_key = str(primary).strip()
    if primary_key and all(str(item) != primary_key for item in values):
        values.insert(0, primary)
    return values or [primary]
