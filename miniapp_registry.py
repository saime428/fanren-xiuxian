"""Explicit local MiniApp capability registry; catalog entries are not production proof."""
from __future__ import annotations

from typing import Any

_REGISTRY: dict[str, dict[str, Any]] = {
    "qingyuanzi": {
        "adapter": "qyz_protocol",
        "flow": "qyz_miniapp",
        # The monitor is installed, but the current source/verification gates
        # block battles. A static catalog is never a server settlement receipt.
        "production_enabled": False,
        "live_verified": False,
    }
}


def register_miniapp(name: str, *, adapter: str, production_enabled: bool = False, live_verified: bool = False, **metadata: Any) -> dict[str, Any]:
    if production_enabled and not live_verified:
        raise ValueError("production MiniApp must be live_verified")
    item = {"adapter": str(adapter), "production_enabled": bool(production_enabled), "live_verified": bool(live_verified), **metadata}
    _REGISTRY[str(name)] = item
    return dict(item)


def get_miniapp(name: str) -> dict[str, Any] | None:
    item = _REGISTRY.get(str(name))
    return dict(item) if item else None


def list_enabled_miniapps() -> list[str]:
    return sorted(name for name, item in _REGISTRY.items() if item.get("production_enabled") is True and item.get("live_verified") is True)


__all__ = ["get_miniapp", "list_enabled_miniapps", "register_miniapp"]

if __name__ == "__main__":
    raise SystemExit(0)
