"""Local combat bookkeeping from the official Mini App fetched on 2026-09-10.

This records browser-side actions and counter damage, never server acceptance.
The protocol adapter keeps server receipts separately.
"""
from __future__ import annotations

from typing import Any, Callable

from qyz_protocol import digest, number


class CombatState:
    def __init__(self, now: Callable[[], float], emit: Callable[..., None]):
        self.now, self.emit = now, emit
        self.started_at: float | None = None
        self.player_hp = 100
        self.counter_damage = 16
        self.dead = False
        self.windows: dict[str, dict[str, Any]] = {}
        self.processed: set[str] = set()
        self.released: set[str] = set()
        self.actions: list[dict[str, Any]] = []
        self.missed: list[dict[str, Any]] = []
        self.stats = dict(dodges=0, grazes=0, damage=0, hits=0, perfects=0, combo=0, bestCombo=0)

    def configure(self, payload: dict[str, Any]) -> None:
        self.player_hp = int(number((payload.get("player") or {}).get("maxHp", 100),
                                    name="max_hp", minimum=1, maximum=1e6))
        phase = (payload.get("challenge") or {}).get("phase") or 1
        self.counter_damage = {1: 16, 2: 22, 3: 30}.get(phase, 22)

    def register(self, window: dict[str, Any]) -> None:
        # Called with windows already normalized by the reference parser.
        self.windows.setdefault(window["id"], window)

    def advance(self, elapsed_ms: float | None = None) -> None:
        if self.dead or self.started_at is None:
            return
        if elapsed_ms is None:
            elapsed_ms = max(0, (self.now() - self.started_at) * 1000)
        for window in sorted(self.windows.values(), key=lambda item: item["centerMs"]):
            if window["id"] in self.processed or elapsed_ms <= window["centerMs"] + window["hitMs"] + 80:
                continue
            self.processed.add(window["id"])
            self.stats["combo"] = 0
            self.player_hp = max(0, self.player_hp - self.counter_damage)
            self.dead = self.player_hp <= 0
            missed = {"index": len(self.missed) + 1, "window_fingerprint": digest(window["id"]),
                      "center_ms": window["centerMs"], "counter_damage": self.counter_damage}
            self.missed.append(missed)
            self.emit("window_missed", missed_window=missed, local_missed_windows=len(self.missed),
                      local_player_hp=self.player_hp, local_dead=self.dead)
            if self.dead:
                break

    def release(self, window_id: str, action: dict[str, Any]) -> bool:
        # The adapter records a release before awaiting /hit. A slow or failed
        # HTTP receipt must not turn a locally matched action into a counter.
        if window_id in self.released:
            return True
        self.advance(action["t"])
        if self.dead:
            return False
        self.released.add(window_id)
        self.actions.append(dict(action))
        window = self.windows[window_id]
        delta = abs(action["t"] - window["centerMs"])
        if window_id not in self.processed and delta <= window["hitMs"]:
            self.processed.add(window_id)
            self.stats["hits"] += 1
            self.stats["dodges"] += 1
            self.stats["combo"] += 1
            self.stats["bestCombo"] = max(self.stats["bestCombo"], self.stats["combo"])
            if delta <= window["perfectMs"] and 520 <= action["holdMs"] <= 1250:
                self.stats["perfects"] += 1
        else:
            self.stats["combo"] = 0
            if action["holdMs"] > 1800:
                self.player_hp = max(0, self.player_hp - 8)
                self.dead = self.player_hp <= 0
                self.emit("overcharge_damage", local_player_hp=self.player_hp, local_dead=self.dead)
        return True

    def finish_proof(self, proof: dict[str, Any]) -> dict[str, Any]:
        self.advance()
        return {**proof, "playerHp": self.player_hp, "dead": self.dead,
                "actions": sorted(self.actions, key=lambda item: item["t"]), "clientStats": dict(self.stats)}
