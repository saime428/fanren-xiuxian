"""Offline Qing Yuanzi checks: python tools/test_world_boss_combat.py.

Uses the installed module, a virtual monotonic clock and an injected JSON
transport. Telegram and real HTTP are never used; broker/marker files are temporary.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import heapq
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "src"))
from tg_game.features.world_boss import world_boss_features as boss
from tg_game.features.world_boss import world_boss_support as support
from tg_game.features.world_boss.world_boss_turnstile import WorldBossTurnstileBroker
import check_world_boss_event as acceptance


class Clock:
    def __init__(self):
        self.now, self.serial, self.waiters = 0.0, 0, []

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.serial += 1
        heapq.heappush(self.waiters, (self.now + seconds, self.serial, future))
        await future

    async def run(self, operation, on_tick=lambda: None):
        # IsolatedAsyncioTestCase enables asyncio debug, which captures a stack per
        # call_soon (an os.stat per source file). ~1 ms a turn on Windows, 21 us on
        # the VPS: the spin below then turns an 85 s test into a seeming hang.
        asyncio.get_running_loop().set_debug(False)
        task = asyncio.create_task(operation)
        for _ in range(15000):
            # Turns for pure task unwinding before a stall counts.  Python 3.10
            # needs ~12 to unwind a disabled schedule (wait + gather of its two
            # waiters); a real deadlock still stalls at any count.
            for _ in range(32):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            while self.waiters and self.waiters[0][2].done():
                heapq.heappop(self.waiters)
            assert self.waiters, "offline operation stalled without a timer"
            self.now = max(self.now, self.waiters[0][0])
            on_tick()
            while self.waiters and self.waiters[0][0] <= self.now + 1e-9:
                _, _, future = heapq.heappop(self.waiters)
                if not future.done():
                    future.set_result(None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("offline battle did not terminate")


def window(index, center):
    return {"id": f"w{index}", "centerMs": center, "hitMs": 460, "perfectMs": 150}


class Server:
    def __init__(self, clock, windows, *, max_hp=100, phase=2, maximum=28000,
                 duplicate=False, charge_delay=0.01, hit_delay=0.02,
                 hit_error="", empty_finish=False, finish_error=None, finish_hp=None,
                 close_at=None, window_delay=0.0, min_duration=0, finish_counts=None,
                 done_early=False):
        self.clock, self.windows = clock, windows
        self.challenge = {"challengeId": "offline-challenge", "durationMs": maximum,
                          "maxDurationMs": maximum, "phase": phase, "windowCount": len(windows), "windows": []}
        if min_duration:
            self.challenge["minDurationMs"] = min_duration
        self.player = {"maxHp": max_hp}
        self.duplicate, self.charge_delay, self.hit_delay = duplicate, charge_delay, hit_delay
        self.hit_error, self.empty_finish = hit_error, empty_finish
        self.finish_error, self.finish_hp = finish_error, finish_hp
        # close_at: the Boss dies at this clock time; window/charge/hit then answer
        # boss_event_closed while /finish still settles (2026-10-15 and 10-17 production).
        self.close_at, self.window_delay, self.finish_counts = close_at, window_delay, finish_counts
        self.done_early = done_early
        self.requests, self.proof, self.window_calls = [], None, 0

    async def post(self, origin, path, payload, timeout):
        assert origin == "https://asc.aiopenai.app"
        endpoint = path.rsplit("/", 1)[-1]
        self.requests.append((endpoint, dict(payload), self.clock.now))
        if endpoint == "start":
            return {"ok": True, "sessionToken": "qyz_offline_session", "challenge": self.challenge,
                    "player": self.player, "boss": {"actionsUsed": 0, "actionsRemaining": 1}}
        if endpoint == "begin":
            await self.clock.sleep(0.02)
            return {"ok": True, "startsInMs": 100}
        if endpoint in ("window", "charge-start", "hit") and self.close_at is not None and self.clock.now >= self.close_at:
            raise boss.MiniAppBeastError("boss_event_closed", 409)
        if endpoint == "window":
            self.window_calls += 1
            if self.window_delay:
                await self.clock.sleep(self.window_delay)
            if self.done_early:
                first = {"window": self.windows[0]} if self.window_calls == 1 else {}
                return {"ok": True, **first, "done": True, "windowCount": len(self.windows)}
            previous = str(payload["afterWindowId"])
            index = next((i + 1 for i, item in enumerate(self.windows) if item["id"] == previous), 0)
            if self.duplicate and self.window_calls == 2:
                index = 0
            if index >= len(self.windows):
                return {"ok": True, "done": True, "windowCount": len(self.windows)}
            return {"ok": True, "window": self.windows[index], "done": index == len(self.windows)-1,
                    "windowCount": len(self.windows)}
        if endpoint == "charge-start":
            await self.clock.sleep(self.charge_delay)
            return {"ok": True, "chargeTicket": "offline-ticket"}
        if endpoint == "hit":
            assert payload["chargeTicket"] == "offline-ticket"
            await self.clock.sleep(self.hit_delay)
            if self.hit_error:
                raise boss.MiniAppBeastError(self.hit_error)
            return {"ok": True, "hit": {"damageYi": 2, "perfect": True, "holdMs": payload["holdMs"], "deltaMs": 10}}
        if endpoint == "finish":
            self.proof = payload["bossProof"]
            if self.finish_error:
                raise self.finish_error
            if self.empty_finish:
                return {"ok": True}
            player_hp = self.proof["playerHp"] if self.finish_hp is None else self.finish_hp
            return {"ok": True, "result": {"grade": "乙等", "score": 70, "player_hp": player_hp,
                                           **(self.finish_counts or {})}}
        raise AssertionError(f"unexpected endpoint: {endpoint}")


def monitor_for(root, clock, server=None):
    enabled = {"value": True}
    actor = SimpleNamespace(
        client=SimpleNamespace(), config={"world_boss": {"enabled": True, "timing_jitter": False}},
        state={}, state_file=str(root / "state.json"), save_state=lambda: None,
        is_world_boss_enabled=lambda: enabled["value"], target_chats=[-1001],
    )
    async def forbidden(*args, **kwargs):
        raise AssertionError("unexpected HTTP")
    monitor = boss.WorldBossMonitor(
        actor, "offline", logger=logging.getLogger("offline-world-boss"),
        sleep=clock.sleep, monotonic=lambda: clock.now,
        post_json=server.post if server else forbidden,
        turnstile_broker=WorldBossTurnstileBroker(root / "broker", clock=lambda: 100000 + clock.now),
    )
    return monitor, enabled


ENTRY = boss.WorldBossEntry(1, -1001, "https://asc.aiopenai.app", "fanrenxiuxian_bot",
                           hashlib.sha256(b"offline-entry").hexdigest(), "qyz_offline_entry")


async def main():
    with tempfile.TemporaryDirectory(prefix="world-boss-check-") as directory:
        root = Path(directory)
        assert boss.WorldBossMonitor._clock_request_lead_ms(0.507) == 120
        assert boss.WorldBossMonitor._clock_request_lead_ms(0.051) == 26
        assert boss.WorldBossMonitor._clock_request_lead_ms(0) == 0
        clock = Clock()
        monitor, enabled = monitor_for(root, clock)
        monitor.account = "profile_2"
        assert monitor._account_offset_slot() == 0
        assert monitor._hit_offset_ms({"perfectMs": 210}) == 0
        monitor.account = "profile_3"
        assert monitor._account_offset_slot() == 1
        assert monitor._hit_offset_ms({"perfectMs": 210}) == 0
        monitor.account = "profile_4"
        assert monitor._hit_offset_ms({"perfectMs": 210}) == 0
        monitor.account = "offline"
        assert monitor._hit_offset_ms({"perfectMs": 210}) == 0
        monitor._reset_drift()
        monitor._drift_ms, monitor._drift_samples = 312, 5
        assert monitor._schedule_lead_ms(254) == 200
        monitor._reset_drift()
        assert monitor._schedule_lead_ms(26) == 26
        clock.now = 1.0
        assert monitor._charge_timeout_seconds(
            {"centerMs": 2500, "hitMs": 620}, 0.0,
        ) == 1.5
        clock.now = 3.3
        assert monitor._charge_timeout_seconds(
            {"centerMs": 2500, "hitMs": 620}, 0.0,
        ) == 0.2
        monitor._record(ENTRY, "queued")
        event, matches = acceptance.select_event(monitor.actor.state, datetime.now(timezone.utc))
        assert matches == 1 and datetime.fromisoformat(event["updated_at"]).tzinfo == timezone.utc
        windows = [window(i, 1000 + i*3000) for i in range(4)]
        monitor._start_combat({"phase": 2}, {"maxHp": 100}, 0, windows)
        clock.now = 1
        assert monitor._record_local_action(windows[0], {"t": 1000, "holdMs": 1000, "stance": "强攻"}) == (True, True)
        clock.now = 4
        monitor._record_local_action(windows[1], {"t": 4000, "holdMs": 1000, "stance": "强攻"})
        clock.now = 8
        monitor._tick_combat()
        assert monitor._combat["hp"] == 78
        assert monitor._combat["stats"]["combo"] == 0
        assert monitor._combat["stats"]["bestCombo"] == 2
        clock.now = 10
        monitor._record_local_action(windows[3], {"t": 10000, "holdMs": 1000, "stance": "强攻"})
        assert monitor._combat["stats"]["hits"] == 3 and monitor._combat["stats"]["combo"] == 1

        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 5500), window(2, 9000)], duplicate=True)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed", result
        assert result["settlement_confirmed"] is True and result["reward_status"] == "not_reported"
        assert len([row for row in server.requests if row[0] == "hit"]) == 3
        assert len(server.proof["actions"]) == 3
        assert server.proof["playerHp"] == 100 and server.proof["dead"] is False
        assert server.proof["clientStats"]["hits"] == 3
        assert server.proof["clientStats"]["bestCombo"] == 3
        assert result["diagnostics"]["version"] == 5
        assert result["diagnostics"]["guard_checks"]["count"] > 0
        reveal_log = result["diagnostics"]["window_reveal"]["log"]
        assert reveal_log[-1]["status"] == "finished" and reveal_log[-1]["reason"] == "server_done", reveal_log[-1]
        for reveal in reveal_log:
            if reveal["status"] != "revealed":
                continue
            trace = reveal["request"]
            assert trace["attempt_count"] == 1 and trace["last_http_status"] == 200
            assert isinstance(trace["last_duration_ms"], (int, float))
            assert trace["guard_ms"] >= 0
        for hit in result["diagnostics"]["hits"]:
            assert hit["actual_elapsed_ms"] == hit["sent_elapsed_ms"]
        assert all(action["t"] <= server.proof["durationMs"] for action in server.proof["actions"])

        # 2026-09-20 production: maxHp 92 (炎修), full health, server answers player_hp
        # on its own 100-point scale. Must be a confirmed settlement, not a failure.
        clock = Clock()
        server = Server(clock, [window(0, 2000)], max_hp=92, finish_hp=100)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed" and result["settlement_confirmed"] is True, result
        assert server.proof["playerHp"] == 92 and result["player_hp"] == 100

        for finish_error, error_code, http_status, status in (
            (None, "boss_settlement_unconfirmed", 200, "failed"),
            (boss.MiniAppBeastError("server_error", 503, details={"token": "qyz_private", "retryable": True}), "server_error", 503, "failed"),
            (RuntimeError("qyz_private"), "runtimeerror", 0, "failed"),
            (boss.MiniAppBeastError("boss_action_limit", 409), "boss_action_limit", 409, "already_participated"),
            (boss.MiniAppBeastError("turnstile_failed", 403), "turnstile_failed", 403, "skipped_verification"),
        ):
            clock = Clock()
            server = Server(clock, [window(0, 2000)], empty_finish=True, finish_error=finish_error)
            monitor, enabled = monitor_for(root, clock, server)
            result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
            assert result["status"] == status and result["error"] == error_code, result
            assert result["settlement_confirmed"] is False and result["reward_status"] == "not_reported"
            assert "grade" not in result and "score" not in result
            assert result["perfect_count"] == result["hit_count"] == result["window_count"] == 1
            assert result["damage_yi_hit_count"] == 1 and result["damage_yi_total"] == 2
            finish = result["diagnostics"]["finish"]
            assert finish["request"]["path"] == "/finish" and finish["error"] == error_code
            assert finish["request"]["attempts"][0]["http_status"] == http_status
            assert finish["request"]["last_http_status"] == http_status
            evidence = acceptance.identity_evidence(result, 1)
            assert evidence["begin_accepted"] is True and evidence["accepted_hit_count"] == 1
            assert evidence["settlement_confirmed"] is False
            assert len(result["diagnostics"]["hits"]) == 1
            assert all(sum(row[0] == name for row in server.requests) == 1 for name in ("begin", "hit", "finish"))
            assert "private" not in json.dumps(result)

        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 6500)], max_hp=30, phase=3, charge_delay=3)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed", result
        assert server.proof["playerHp"] == 0 and server.proof["dead"] is True
        assert not [row for row in server.requests if row[0] == "hit"]
        assert len([row for row in server.requests if row[0] == "charge-start"]) == 1
        assert server.proof["durationMs"] < 6500
        assert not list(root.glob(".world_boss_defeated_*.json"))

        clock = Clock()
        server = Server(clock, [window(0, 2000)], maximum=5000, hit_delay=6)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed", result
        assert server.proof["durationMs"] == 5000 and clock.now > 8

        clock = Clock()
        server = Server(clock, [window(0, 2000)], hit_error="api_timeout")
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed" and result["hit_count"] == 0, result
        assert len([row for row in server.requests if row[0] == "hit"]) == 1
        assert server.proof["realtimeDamageApplied"] is False

        clock = Clock()
        server = Server(clock, [window(0, 5000)])
        monitor, enabled = monitor_for(root, clock, server)
        def disable():
            if clock.now >= 1:
                enabled["value"] = False
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"), on_tick=disable)
        assert result["status"] == "disabled", result
        assert not [row for row in server.requests if row[0] in {"charge-start", "hit", "finish"}]
        assert not list(root.glob(".world_boss_defeated_*.json"))

        # A passing DB guard is reused for 100 ms; a failing one never is, and
        # monitor.stop() (self.enabled) does not wait for the reuse window.
        clock = Clock()
        monitor, enabled = monitor_for(root, clock)
        calls = []
        monitor.actor.is_world_boss_enabled = lambda: calls.append(clock.now) or enabled["value"]
        monitor._check_enabled()
        monitor._check_enabled()
        assert calls == [0.0], calls
        enabled["value"] = False
        clock.now = 0.09
        monitor._check_enabled()
        clock.now = 0.1
        for message in ("A DB-side stop was hidden past 100 ms", "A failing guard was reused"):
            try:
                monitor._check_enabled()
                raise AssertionError(message)
            except boss._WorldBossDisabledError:
                pass
        assert calls == [0.0, 0.1, 0.1], calls
        enabled["value"] = True
        monitor._check_enabled()
        monitor.enabled = False
        try:
            monitor._check_enabled()
            raise AssertionError("monitor.stop() waited for the guard reuse window")
        except boss._WorldBossDisabledError:
            pass
        assert len(calls) == 4 and monitor._guard_timing["count"] == 7, (calls, monitor._guard_timing)

        clock = Clock()
        monitor, enabled = monitor_for(root, clock)
        monitor._reset_drift()
        monitor._drift_ms, monitor._drift_samples = 120, 1
        sample = monitor._record_drift(280, center_ms=10000, sent_elapsed_ms=9830,
                                       request_completed_elapsed_ms=10290, request_lead_ms=50)
        assert sample["controller_correction_ms"] == 400
        assert monitor._drift_ms > 120
        assert monitor._revealed_window({"id": "bad", "centerMs": float("inf")}) is None
        assert monitor._revealed_window({"id": "bad", "centerMs": 1, "hitMs": 10**9}) is None
        diagnostic = boss._diagnostic_value({"chargeTicket": "private", "message": "token=private qyz_private"})
        assert "private" not in str(diagnostic)
        assert "private" not in str(boss._diagnostic_value({"a": {"b": {"c": {"d": {"token": "private"}}}}}))

        # Production-shaped window logging: flattened timings survive while
        # nested attempts/credentials retain their existing truncation rules.
        trace = {"executor_queue_ms": 2, "transport_ms": 5, "loop_resume_ms": 3,
                 "http_headers_wait_ms": 4,
                 "attempts": [{"duration_ms": 10, "token": "private"}], "token": "private"}
        saved = boss._diagnostic_value([{"request": trace}])[0]["request"]
        assert all(saved[key] == trace[key] for key in ("executor_queue_ms", "transport_ms", "loop_resume_ms", "http_headers_wait_ms"))
        assert saved["attempts"] == ["<truncated>"] and "private" not in str(saved)

        # Test the real monitor -> executor -> transport wiring as well as the
        # sanitizer, so forgetting to pass timing to the transport fails here.
        real_monitor, _ = monitor_for(root, Clock())
        original_post = support._json_post_sync
        support._json_post_sync = lambda *args, **kwargs: {"ok": True}
        real_monitor.post_json = None
        try:
            request_trace = {"transport_ms": 999999, "stale": "private"}
            await real_monitor._request(support.ORIGIN, support.API_PREFIX + "window", {}, trace=request_trace)
            saved_trace = boss._diagnostic_value([{"request": request_trace}])[0]["request"]
            for key in ("executor_queue_ms", "transport_ms", "loop_resume_ms", "guard_ms"):
                assert 0 <= saved_trace[key] < 999999, saved_trace
            assert saved_trace["attempt_count"] == 1 and "private" not in str(saved_trace)
        finally:
            support._json_post_sync = original_post
            if real_monitor._http_executor:
                real_monitor._http_executor.shutdown(wait=True)

        broker = monitor.turnstile_broker
        create = broker.create_request
        def cancelled_request(**kwargs):
            request = create(**kwargs)
            broker.cancel(request["request_id"], reason="turnstile_interaction_required")
            return request
        broker.create_request = cancelled_request
        try:
            await monitor._wait_for_turnstile_token(ENTRY, "主魂", "offline-challenge")
        except boss.MiniAppBeastError as exc:
            assert exc.code == "turnstile_interaction_required"
        else:
            raise AssertionError("cancelled verification was accepted")
        assert clock.now == 0

        # A sender lookup already awaiting when stop begins must not enqueue work.
        started, resume = asyncio.Event(), asyncio.Event()
        async def sender():
            started.set()
            await resume.wait()
            return SimpleNamespace(bot=True, username="fanrenxiuxian_bot")
        message = SimpleNamespace(id=1, chat_id=-1001, raw_text="世界通告 真仙试锋开启",
                                  buttons=[[SimpleNamespace(text="进入真仙战场", url="https://t.me/fanrenxiuxian_bot?startapp=qyz_offline_entry")]],
                                  get_sender=sender)
        task = asyncio.create_task(monitor.process_message(message))
        await started.wait()
        await monitor.stop()
        resume.set()
        assert await task is False
        assert not monitor._tasks and not monitor.enabled
        assert await monitor.process_message(message) is False
        message.buttons[0][0].url = "https://attacker.invalid/?startapp=qyz_offline_entry"
        assert boss.extract_world_boss_entry(message) is None

        # 2026-10-17: the Boss died at 67..73 s into our fights and /finish was
        # refused below challenge.minDurationMs (75 s).  Hits stop at once, but
        # the clock runs on and the proof reports at least the minimum.
        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 5500), window(2, 9000)], close_at=7.0, min_duration=12000)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed" and result["hit_count"] == 2, result
        finish = result["diagnostics"]["finish"]
        assert finish["reason"] == "boss_event_closed" and finish["minimum_duration_ms"] == 12000, finish
        assert server.proof["durationMs"] >= 12000 and finish["local_finished_ms"] >= 12050, finish
        begin_at = next(t for name, _, t in server.requests if name == "begin")
        finish_at = next(t for name, _, t in server.requests if name == "finish")
        assert finish_at - begin_at >= 12.1, (begin_at, finish_at)
        assert not [row for row in server.requests if row[0] == "hit" and row[2] >= 7.0]
        for marker in root.glob(".world_boss_defeated_*.json"):
            marker.unlink()  # the shared ENTRY would otherwise read as already dead

        # Window reads are paced from request start (official 360 ms cadence):
        # a 300 ms reply no longer stretches the gap to 660 ms.
        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 5500), window(2, 9000)], window_delay=0.3)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert result["status"] == "completed" and result["hit_count"] == 3, result
        polls = [t for name, _, t in server.requests if name == "window"]
        gaps = [round(b - a, 6) for a, b in zip(polls, polls[1:])]
        assert gaps and all(gap == 0.36 for gap in gaps), gaps

        # ``done`` before every declared window keeps the cursor polling; a full
        # stall interval without a new window then accepts the shorter count.
        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 30000)], done_early=True, maximum=40000)
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        log = result["diagnostics"]["window_reveal"]["log"]
        assert result["status"] == "completed" and result["hit_count"] == 1, result
        assert [row["status"] for row in log].count("done_early") == 1, log
        assert log[-1]["reason"] == "server_done_short", log[-1]
        assert clock.now >= 20, clock.now

        # Settlement counts outrank per-hit receipts when the server reports them.
        clock = Clock()
        server = Server(clock, [window(0, 2000), window(1, 5500), window(2, 9000)], finish_counts={"hits": 2, "perfects": 1})
        monitor, enabled = monitor_for(root, clock, server)
        result = await clock.run(monitor._run_identity(ENTRY, "主魂", "offline-init-data"))
        assert (result["hit_count"], result["perfect_count"]) == (2, 1), result
        assert result["diagnostics"]["finish"]["counts"]["mismatch"] is True
        reconcile = boss.WorldBossMonitor._reconcile_finish_counts
        outcome = {"hit_count": 16, "perfect_count": 11, "window_count": 16}
        counts = reconcile(outcome, {"hits": 99, "perfects": True})
        assert (outcome["hit_count"], outcome["perfect_count"]) == (16, 11) and not counts["mismatch"], counts
        outcome = {"hit_count": 16, "perfect_count": 11, "window_count": 16}
        counts = reconcile(outcome, {"realtime_hit_count": 10})
        assert (outcome["hit_count"], outcome["perfect_count"]) == (10, 10), counts
        assert counts["perfects_source"] == "bounded_realtime_responses"

        # Arrived timing feedback re-plans a pending schedule instead of waiting
        # for the stale deadline.
        clock = Clock()
        monitor, enabled = monitor_for(root, clock)
        targets = {"value": 5.0}
        def replan():
            if clock.now >= 1.0 and targets["value"] == 5.0:
                targets["value"] = 2.0
                monitor._notify_timing_update()
        assert await clock.run(monitor._wait_for_timing_target(lambda: targets["value"]), on_tick=replan) is True
        assert 1.99 <= clock.now <= 2.11, clock.now

        quality = boss.WorldBossMonitor._hold_feedback_quality
        one = {"total_duration_ms": 20, "attempts": [{}]}
        assert quality(one, one, 10)["eligible"] is True
        assert quality(one, {"total_duration_ms": 20, "attempts": [{}, {}]}, 10)["reason"] == "hit_request_retried"
        assert quality(None, {"attempts": [{}]}, 10)["reason"] == "request_timing_missing"
    print("world_boss combat self-check passed (offline; no Telegram or HTTP)")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
