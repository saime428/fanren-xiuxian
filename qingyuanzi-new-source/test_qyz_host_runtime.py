"""Host regressions use in-memory actors and never open a real WebView."""
import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch, call
import json
import os
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from qyz_protocol import ProtocolRuntime
from test_qyz_protocol import FakeClock, BattleFixture, CallbackFixtureBroker

import qingyuanzi_host as host
from test_qyz_source_policy import entry_message


def message(token, *, bot_username="hantianzun16_bot"):
    value = entry_message(chat_id=host.CANONICAL_CHAT_ID, bot_username=bot_username)
    value.buttons[0][0].url = f"https://t.me/{bot_username}?startapp=" + token
    return value


def actor_fixture() -> Any:
    return SimpleNamespace(account_key="tg9100000001", client=SimpleNamespace(), state={},
                           target_chat_ids=host.TARGET_CHAT_IDS, save_state=lambda: None)


def messages_for_chat(template):
    def read(chat_id, **_options):
        topic = host.SOURCE_CHAT_TOPICS[chat_id]
        return SimpleNamespace(**{**vars(template), "chat_id": chat_id,
            "reply_to": SimpleNamespace(reply_to_top_id=topic, reply_to_msg_id=topic)})
    return read


class HostIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The page policy is shared by the fleet; one test's read is not another's.
        self.enterContext(patch.dict(host._SECURITY_POLICY, clear=True))

    async def test_account_shard_lifecycle_is_inside_account_process_lease(self):
        held = []

        async def close(account):
            self.assertEqual(held, [account])

        service = SimpleNamespace(start_account=Mock(), close_account=AsyncMock(side_effect=close))

        @contextmanager
        def lease(account):
            held.append(account)
            try:
                yield
            finally:
                held.remove(account)

        async def run(account, _stop):
            self.assertEqual(held, [account])
            service.start_account.assert_called_once_with(account)

        with patch.object(host, 'VERIFICATION_SERVICE', service), \
             patch.object(host, 'account_process_lease', lease), \
             patch.object(host, '_run_account_unlocked', side_effect=run):
            await host.run_account('tg9100000001', asyncio.Event())
        service.close_account.assert_awaited_once_with('tg9100000001')

    async def test_host_cleanup_clears_global_when_service_close_raises(self):
        self.assertTrue(hasattr(host, 'close_verification_service'))
        service = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('fixture_close_failed')))
        with patch.object(host, 'VERIFICATION_SERVICE', service):
            with self.assertRaisesRegex(RuntimeError, 'fixture_close_failed'):
                await host.close_verification_service(service)
            self.assertIsNone(host.VERIFICATION_SERVICE)

    async def test_verification_builder_serves_every_account_from_the_proven_profile(self):
        self.assertTrue(hasattr(host, "build_verification_service"))
        service = host.build_verification_service()
        try:
            self.assertEqual(service.account_order, tuple(host.ACCOUNTS))
            self.assertEqual(service.profile_dir.name, "browser-profile")
            self.assertTrue(callable(getattr(service.broker, "want_presolve", None)))
            for account in host.ACCOUNTS:
                self.assertTrue(service.capability(account)["account"] == account)
        finally:
            await service.close()

    async def test_per_window_state_survives_polling_and_is_bound_to_its_event(self):
        actor = actor_fixture()
        saved = []
        actor.save_state = lambda: saved.append(json.loads(json.dumps(actor.state)))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        first = host.extract_world_boss_entry(message("qyz_fixture_window_states"))
        monitor._trace(first, {"account": actor.account_key, "phase": "charge-start_submitted",
            "window_state": {"window_fingerprint": "b" * 64, "index": 4, "phase": "charge_pending",
                             "center_ms": 14900, "server_perfect": None}})
        self.assertEqual(len(saved), 0)
        monitor._trace(first, {"account": actor.account_key, "phase": "window_confirmed", "expected_windows": 16})
        self.assertEqual(len(saved), 0)
        self.assertEqual(actor.state["world_boss_events"][0]["window_states"]["b" * 64]["phase"], "charge_pending")
        monitor._trace(first, {"account": actor.account_key, "phase": "hit_accepted",
            "window_state": {"window_fingerprint": "b" * 64, "index": 4, "phase": "confirmed_hit",
                             "server_perfect": False, "server_hold_ms": 500},
            "receipt": {"window_fingerprint": "b" * 64, "accepted": True, "server_perfect": False}})
        self.assertEqual(len(saved), 1)
        event = saved[-1]["world_boss_events"][0]
        self.assertEqual(event["window_states"]["b" * 64]["phase"], "confirmed_hit")
        self.assertFalse(event["receipts"][0]["server_perfect"])
        second = host.extract_world_boss_entry(message("qyz_fixture_next_window_states"))
        monitor._record(second, "queued")
        self.assertNotIn("window_states", actor.state["world_boss_events"][1])

    async def test_new_battle_cannot_inherit_previous_settlement_counters(self):
        actor = actor_fixture()
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor._health("settled_perfect", event_fingerprint="old", finish_confirmed=True,
                        expected_windows=16, accepted_hits=16, server_perfects=16)
        monitor._health("queued", event_fingerprint="new")
        state = actor.state["world_boss_health"]
        self.assertFalse(state["finish_confirmed"])
        self.assertNotIn("accepted_hits", state)
        self.assertNotIn("server_perfects", state)

    async def test_admission_and_exact_reread_do_not_depend_on_sender_metadata(self):
        actor = actor_fixture()
        msg = message("qyz_fixture_without_sender")
        msg.sender = None
        msg.get_sender = AsyncMock(side_effect=RuntimeError("sender unavailable"))
        actor.client.get_messages = AsyncMock(return_value=msg)
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(monitor, "_run_exclusive", new=AsyncMock()):
            self.assertTrue(await monitor.process_message(msg))
            await asyncio.gather(*monitor._tasks)
        entry = host.extract_world_boss_entry(msg)
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=False)):
            result = await monitor.run_entry(entry)
        self.assertEqual(result["status"], "blocked_membership")
        self.assertEqual(result["phase"], "membership")
        msg.get_sender.assert_not_awaited()

    async def test_webview_requires_the_entry_target_to_resolve_to_a_bot(self):
        client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(id=1234, bot=False)),
                                 get_input_entity=AsyncMock())
        with self.assertRaises(host.ProtocolError) as caught:
            await host.request_official_webview_url(client, "hantianzun23_bot", "qyz_fixture_only")
        self.assertEqual(caught.exception.code, "entry_target_not_bot")
        client.get_input_entity.assert_not_awaited()

    async def test_official_drag_verification_page_is_accepted_only_as_a_known_official_interstitial(self):
        client = AsyncMock()
        client.get_entity.return_value = SimpleNamespace(id=1234, bot=True)
        client.get_input_entity.return_value = SimpleNamespace()
        client.return_value = SimpleNamespace(
            url="https://asc.aiopenai.app/miniapp/xianxia-verify?tgWebAppStartParam=qyz_fixture_only"
        )
        with patch.object(host.utils, "get_input_user", return_value=SimpleNamespace()):
            value = await host.request_official_webview_url(
                client, "sample_peer_06", "qyz_fixture_only"
            )
        self.assertIn("/miniapp/xianxia-verify", value)

    async def test_drag_verification_block_remains_bad_after_read_only_health_refresh(self):
        actor = actor_fixture()
        actor.client.is_connected = lambda: True
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        actor.state["world_boss_events"] = [{
            "fingerprint": "a" * 64,
            "message_id": 12267212,
            "status": "blocked_official_verification_required",
            "error": "official_drag_verification_required",
            "mutation_started": False,
        }]
        class Context:
            async def __aenter__(self):
                return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}))
            async def __aexit__(self, *_args):
                return None
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor.target_chats = list(host.TARGET_CHAT_IDS)
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "latest_source_message", new=AsyncMock(return_value=None)), \
             patch.object(host, "HttpTransport", return_value=Context()):
            await monitor.refresh_health()
        health = actor.state["world_boss_health"]
        self.assertEqual(health["status"], "blocked_official_verification_required")
        self.assertIn("latest_battle_not_successful", health["blockers"])
        self.assertFalse(health["business_ready"])

    async def test_unresolved_journal_is_retained_when_newer_events_are_trimmed(self):
        actor = actor_fixture()
        actor.state["world_boss_events"] = [{
            "fingerprint": "a" * 64, "status": "unknown_mutation_outcome", "mutation_started": True,
        }] + [{"fingerprint": str(i), "status": "settled_perfect"} for i in range(25)]
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        msg = message("qyz_fixture_new")
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        monitor._record(entry, "queued")
        self.assertEqual(actor.state["world_boss_events"][0]["fingerprint"], "a" * 64)
        self.assertEqual(len(actor.state["world_boss_events"]), 21)

    async def test_restart_preserves_unfinished_mutation_after_fresh_heartbeat(self):
        for phase in ("begin_submitted", "hit_submitted", "hit_accepted", "finish_submitted"):
            with self.subTest(phase=phase):
                actor = actor_fixture()
                actor.state.update(world_boss_monitor_active=True, world_boss_events=[{
                    "fingerprint": "a" * 64, "message_id": 12248312,
                    "status": phase, "mutation_started": True,
                    "receipts": [{"index": 1, "accepted": True, "server_perfect": True}],
                }])
                actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
                msg = message("qyz_fixture_next")
                msg.date = datetime.now(timezone.utc) - timedelta(seconds=61)
                actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(msg))
                actor.client.is_connected = lambda: True
                class Context:
                    async def __aenter__(self):
                        return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}))
                    async def __aexit__(self, *_args): pass
                monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
                with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
                     patch.object(host, "HttpTransport", return_value=Context()):
                    await monitor.refresh_health()
                health = actor.state["world_boss_health"]
                self.assertEqual(health["status"], "unknown_mutation_outcome")
                self.assertFalse(health["business_ready"])
                self.assertIn("unfinished_battle_requires_reconciliation", health["blockers"])
                record = actor.state["world_boss_events"][0]
                self.assertEqual(record["status"], "unknown_mutation_outcome")
                self.assertEqual(record["recovered_from_status"], phase)
                self.assertEqual(len(record["receipts"]), 1)

    async def test_miss_journal_keeps_local_events_separate_from_server_receipts(self):
        actor = actor_fixture()
        saved = []
        actor.save_state = lambda: saved.append(json.loads(json.dumps(actor.state)))
        msg = message("qyz_fixture_miss")
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        receipt = {"index": 1, "accepted": True, "server_perfect": True, "damage_yi": 1}
        monitor._trace(entry, {"account": actor.account_key, "phase": "hit_accepted", "receipt": receipt})
        missed = {"index": 2, "window_fingerprint": "b" * 64, "counter_damage": 16}
        with patch.object(host, "JOURNAL_FLUSH_SECONDS", 0):
            monitor._trace(entry, {"account": actor.account_key, "phase": "window_missed", "missed_window": missed,
                                   "missed_windows": 1, "local_player_hp": 84, "local_dead": False})
        self.assertEqual(len(saved), 1)
        await asyncio.sleep(.01)
        self.assertEqual(len(saved), 2)
        record = saved[-1]["world_boss_events"][-1]
        self.assertEqual(record["receipts"], [receipt])
        self.assertEqual(record["missed_window_records"], [missed])
        self.assertEqual(record["missed_windows"], 1)
        self.assertEqual(record["local_player_hp"], 84)

    async def test_private_group_is_resolved_from_account_dialogs_when_memory_cache_is_empty(self):
        actor = actor_fixture()
        channel = host.types.Channel(id=9400000004, title="fixture", photo=host.types.ChatPhotoEmpty(),
                                     date=datetime.now(timezone.utc), megagroup=True)
        actor.client.get_entity = AsyncMock(side_effect=ValueError("not cached"))
        async def dialogs(**options):
            self.assertEqual(options, {"limit":200})
            yield SimpleNamespace(id=-1001234, entity=None)
            yield SimpleNamespace(id=host.CANONICAL_CHAT_ID, entity=channel)
        actor.client.iter_dialogs = dialogs
        self.assertEqual(await host.resolve_actor_target_chats(actor), [host.CANONICAL_CHAT_ID])

    async def test_expired_last_announcement_waits_for_next_event_without_consuming_it(self):
        actor = actor_fixture()
        actor.state["world_boss_monitor_active"] = True
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        msg = message("qyz_fixture_expired")
        msg.date = datetime.now(timezone.utc) - timedelta(seconds=61)
        actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(msg))
        actor.client.is_connected = lambda: True
        class Context:
            async def __aenter__(self):
                return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled":False}))
            async def __aexit__(self, *_args): pass
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "request_official_webview_url", side_effect=AssertionError("read-only check")):
            await monitor.refresh_health()
        health = actor.state["world_boss_health"]
        self.assertTrue(health["source_verified"])
        self.assertFalse(health["entry_fresh"])
        self.assertEqual(health["status"], "idle_waiting_event")
        self.assertTrue(health["business_ready"])
        self.assertFalse(await monitor.process_message(msg))
        self.assertFalse(actor.state.get("world_boss_events"))

    async def test_heartbeat_reports_missing_source_even_when_monitor_is_active(self):
        actor = actor_fixture()
        actor.state["world_boss_monitor_active"] = True
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        actor.client.get_messages = AsyncMock(side_effect=OSError("source read failed"))
        actor.client.is_connected = lambda: True
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        self.assertTrue(hasattr(monitor, "refresh_health"))
        policy = AsyncMock(return_value={"enabled": True, "page_sha256": "a"*64})
        class Context:
            async def __aenter__(self): return SimpleNamespace(security_policy=policy)
            async def __aexit__(self, *_args): pass
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "request_official_webview_url", side_effect=AssertionError("must be read-only")):
            await monitor.refresh_health()
        health = actor.state["world_boss_health"]
        self.assertEqual(health["status"], "blocked_source_unavailable")
        self.assertEqual(health["blockers"], ["source_unavailable", "official_verification_required"])
        self.assertTrue(health["identity_verified"])
        self.assertFalse(health["business_ready"])

    async def test_readable_second_group_without_an_entry_is_healthy_idle(self):
        actor = actor_fixture()
        actor.state["world_boss_monitor_active"] = True
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        msg = message("qyz_fixture_old_primary")
        msg.date = datetime.now(timezone.utc) - timedelta(hours=10)
        actor.client.get_messages = AsyncMock(side_effect=lambda chat_id, **_options:
                                              [msg] if chat_id == host.CANONICAL_CHAT_ID else [])
        actor.client.is_connected = lambda: True
        class Context:
            async def __aenter__(self):
                return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}))
            async def __aexit__(self, *_args): pass
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor.target_chats = list(host.TARGET_CHAT_IDS)
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "request_official_webview_url", side_effect=AssertionError("read-only check")):
            await monitor.refresh_health()
        health = actor.state["world_boss_health"]
        self.assertEqual(health["status"], "idle_waiting_event")
        self.assertTrue(health["source_verified"])
        self.assertEqual(health["latest_source_message_id"], msg.id)
        self.assertFalse(health["entry_fresh"])
        self.assertEqual(len(health["source_checks"]), 2)
        self.assertTrue(health["source_checks"][1]["source_verified"])
        self.assertFalse(health["source_checks"][1]["entry_found"])
        self.assertFalse(actor.state.get("world_boss_events"))

    async def test_health_reads_one_message_per_source_and_the_page_once_a_minute(self):
        actor = actor_fixture()
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        actor.client.get_messages = AsyncMock(return_value=[])
        actor.client.is_connected = lambda: True
        policy = AsyncMock(return_value={"enabled": False})
        class Context:
            async def __aenter__(self): return SimpleNamespace(security_policy=policy)
            async def __aexit__(self, *_args): pass
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        self.assertFalse(host.account_scans_telegram_history(actor.account_key))
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "HttpTransport", return_value=Context()):
            await monitor.refresh_health()
            await monitor.refresh_health()
        self.assertEqual(actor.state["world_boss_health"]["status"], "idle_waiting_event")
        policy.assert_awaited_once()
        reads = actor.client.get_messages.await_args_list
        self.assertEqual(len(reads), 2 * len(host.TARGET_CHAT_IDS))
        self.assertTrue(all(read.kwargs["limit"] == 1 and "search" not in read.kwargs for read in reads))

    async def test_unresolved_source_does_not_install_an_unfiltered_listener(self):
        actor = actor_fixture()
        actor.client.add_event_handler = unittest.mock.Mock()
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(host, "resolve_actor_target_chats", new=AsyncMock(return_value=[])):
            self.assertFalse(await monitor.install())
        actor.client.add_event_handler.assert_not_called()
        self.assertFalse(actor.state["world_boss_monitor_active"])

    async def test_exact_canonical_group_resolves_to_marked_peer(self):
        actor = actor_fixture()
        actor.client.add_event_handler = unittest.mock.Mock()
        actor.client.get_entity = AsyncMock(return_value=host.types.Channel(
            id=9400000004, title="fixture", photo=host.types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc), megagroup=True))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        self.assertTrue(await monitor.install())
        self.assertEqual(monitor.target_chats, [host.CANONICAL_CHAT_ID])
        self.assertEqual(actor.client.add_event_handler.call_count, 2)
        actor.client.get_entity.assert_any_await(host.TARGET_CHAT_IDS[0])

    async def test_source_resolver_rejects_foreign_group_broadcast_and_untrusted_config(self):
        for chat_id, megagroup in ((1234, True), (9400000004, False)):
            actor = actor_fixture()
            actor.client.get_entity = AsyncMock(return_value=host.types.Channel(
                id=chat_id, title="fixture", photo=host.types.ChatPhotoEmpty(),
                date=datetime.now(timezone.utc), megagroup=megagroup))
            self.assertEqual(await host.resolve_actor_target_chats(actor), [])
        actor = actor_fixture()
        actor.target_chat_ids = ("@sample_peer_04",)
        actor.client.get_entity = AsyncMock()
        self.assertEqual(await host.resolve_actor_target_chats(actor), [])
        actor.client.get_entity.assert_not_awaited()

    async def test_trace_persists_journal_and_receipt_together_once(self):
        actor = actor_fixture()
        saved = []
        actor.save_state = lambda: saved.append(json.loads(json.dumps(actor.state)))
        msg = message("qyz_fixture_journal")
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(host, "JOURNAL_FLUSH_SECONDS", 0):
            monitor._trace(entry, {"account":actor.account_key, "phase":"hit_submitted"})
        self.assertEqual(len(saved), 0)
        receipt = {"index":1, "accepted":True, "damage_yi":1}
        monitor._trace(entry, {"account":actor.account_key, "phase":"hit_accepted", "receipt":receipt})
        await asyncio.sleep(.01)
        # The receipt write carries the deferred strike and cancels its flush.
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[-1]["world_boss_events"][-1]["mutation_started"])
        self.assertEqual(saved[-1]["world_boss_events"][-1]["receipts"], [receipt])

    async def test_battle_progress_is_flushed_at_most_once_per_interval(self):
        actor = actor_fixture()
        saved = []
        actor.save_state = lambda: saved.append(json.loads(json.dumps(actor.state)))
        entry = host.extract_world_boss_entry(message("qyz_fixture_coalesced"))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor._trace(entry, {"account": actor.account_key, "phase": "start_submitted"})
        self.assertEqual(len(saved), 1)
        with patch.object(host, "JOURNAL_FLUSH_SECONDS", .05):
            for index in range(30):
                monitor._trace(entry, {"account": actor.account_key, "phase": "window_progress",
                                       "window_state": {"window_fingerprint": "c" * 64, "index": index}})
            self.assertEqual(len(saved), 1)
            await asyncio.sleep(.1)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[-1]["world_boss_events"][-1]["window_states"]["c" * 64]["index"], 29)

    async def test_window_polling_does_not_rewrite_durable_battle_history(self):
        actor = actor_fixture()
        saved = []
        actor.save_state = lambda: saved.append(json.loads(json.dumps(actor.state)))
        msg = message("qyz_fixture_poll_journal")
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor._trace(entry, {"account": actor.account_key, "phase": "hit_submitted"})
        receipt = {"index": 1, "accepted": True, "damage_yi": 1}
        monitor._trace(entry, {"account": actor.account_key, "phase": "hit_accepted", "receipt": receipt})
        for phase in ("window_submitted", "window_confirmed") * 10:
            monitor._trace(entry, {"account": actor.account_key, "phase": phase, "expected_windows": 16})
        self.assertEqual(len(saved), 1)
        # An interruption during reads still has the last mutation/receipt;
        # the next write includes all in-memory updates.
        prior = saved[-1]["world_boss_events"][-1]
        self.assertTrue(prior["mutation_started"])
        self.assertEqual(prior["receipts"], [receipt])
        with patch.object(host, "JOURNAL_FLUSH_SECONDS", 0):
            monitor._trace(entry, {"account": actor.account_key, "phase": "charge-start_submitted"})
        await asyncio.sleep(.01)
        self.assertEqual(len(saved), 2)
        current = saved[-1]["world_boss_events"][-1]
        self.assertEqual(current["expected_windows"], 16)
        self.assertEqual(current["receipts"], [receipt])
        self.assertEqual(current["status"], "charge-start_submitted")

    async def test_history_reconciliation_searches_all_official_announcements(self):
        actor = actor_fixture()
        actor.client.get_messages = AsyncMock(return_value=None)
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor.target_chats = [host.CANONICAL_CHAT_ID]
        self.assertFalse(await monitor.scan_recent_entries())
        actor.client.get_messages.assert_has_awaits([
            call(host.CANONICAL_CHAT_ID, limit=100, reply_to=host.CANONICAL_TOPIC_ID),
            call(host.CANONICAL_CHAT_ID, limit=32, search="世界通告"),
        ], any_order=True)

    async def test_history_selects_a_new_bot_entry_and_ignores_results_without_an_entry(self):
        actor = actor_fixture()
        good = message("qyz_fixture_rotated", bot_username="hantianzun99_bot")
        good.id += 10
        foreign = message("qyz_fixture_foreign")
        foreign.chat_id = -1009400000003
        result = message("qyz_fixture_result", bot_username="sample_peer_06")
        result.raw_text = "【世界通告｜真仙试锋败退】"
        result.buttons = []
        actor.client.get_messages = AsyncMock(return_value=[foreign, result, good])
        self.assertIs(await host.latest_source_message(actor.client), good)

    async def test_both_requested_groups_are_installed_and_reconciled(self):
        actor = actor_fixture()
        def channel(chat_id):
            return host.types.Channel(id=host.utils.resolve_id(chat_id)[0], title="fixture",
                photo=host.types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True)
        actor.client.get_entity = AsyncMock(side_effect=channel)
        actor.client.add_event_handler = unittest.mock.Mock()
        actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(message("qyz_fixture_both")))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        self.assertTrue(await monitor.install())
        self.assertEqual(monitor.target_chats, list(host.TARGET_CHAT_IDS))
        monitor.process_message = AsyncMock(return_value=True)
        self.assertTrue(await monitor.scan_recent_entries())
        self.assertEqual({args.args[0].chat_id for args in monitor.process_message.await_args_list}, set(host.TARGET_CHAT_IDS))

    async def test_another_process_owner_cannot_acquire_the_same_account(self):
        self.assertTrue(hasattr(host, "account_process_lease"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(host, "STATE_ROOT", Path(tmp)):
            with host.account_process_lease("tg9100000001"):
                with self.assertRaises(host.ProtocolError):
                    with host.account_process_lease("tg9100000001"):
                        self.fail("duplicate owner acquired lease")
                with host.account_process_lease("tg9100000003"):
                    pass
            with host.account_process_lease("tg9100000001"):
                pass

    async def test_router_entry_runs_reference_protocol_without_signed_browser_handoff(self):
        self.assertTrue(hasattr(host, "HttpTransport"), "Host has not been connected to the protocol transport")
        actor = actor_fixture()
        actor.my_info = SimpleNamespace(id=9100000001, username="demo_player_01")
        msg = message("qyz_fixture_host", bot_username="hantianzun23_bot")
        msg.sender = None
        msg.get_sender = AsyncMock(side_effect=RuntimeError("sender unavailable"))
        actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(msg))
        actor.client.get_me = AsyncMock(return_value=actor.my_info)
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        assert entry is not None
        fields = {"user":json.dumps({"id":9100000001,"username":"demo_player_01"}), "start_param":entry.token, "auth_date":str(int(time.time())), "hash":"a"*64}
        router_url = "https://asc.aiopenai.app/miniapp/xianxia-verify?tgWebAppStartParam=" + entry.token + "#" + urlencode({"tgWebAppData":urlencode(fields)})
        clock = FakeClock()
        api = BattleFixture(clock, security_required=True)
        broker = CallbackFixtureBroker()
        original = api.post_json
        async def verified_request(endpoint, body, **options):
            if endpoint == "begin" and not body.get("turnstileToken"):
                raise host.ProtocolError("turnstile_required", http_status=403)
            return await original(endpoint, body, **options)
        api.post_json = verified_request
        service = SimpleNamespace(
            broker=broker,
            capability=lambda _account=None: {"available": True, "error": ""},
            verify_drag=AsyncMock(side_effect=AssertionError("signed browser handoff is forbidden")),
        )
        class Context:
            async def __aenter__(self):
                return api
            async def __aexit__(self, *_args):
                pass
        def runtime(**kwargs):
            return ProtocolRuntime(**kwargs, monotonic=clock.now, sleep=clock.sleep)
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "request_official_webview_url", new=AsyncMock(return_value=router_url)) as webview_request, \
             patch.object(host, "VERIFICATION_SERVICE", service), \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "ProtocolRuntime", side_effect=runtime):
            result = await monitor.run_entry(entry)
        self.assertFalse(hasattr(host, "browser"))
        self.assertEqual(result["status"], "settled_perfect")
        self.assertEqual(result["accepted_hits"], 16)
        record = actor.state["world_boss_events"][-1]
        self.assertTrue(record["mutation_started"])
        self.assertEqual(record["protocol_result"]["server_perfects"], 16)
        serialized = json.dumps(actor.state)
        self.assertNotIn("fixture_session_secret", serialized)
        self.assertNotIn("tgWebAppData", serialized)
        self.assertNotIn(broker.generated[0][1], serialized)
        self.assertIn("verification_accepted", [row["phase"] for row in record["confirmed_phases"]])
        self.assertEqual(actor.state["world_boss_execution_owner"], "official_protocol")
        msg.get_sender.assert_not_awaited()
        service.verify_drag.assert_not_awaited()
        self.assertEqual(webview_request.await_count, 1)

    async def test_battle_runs_on_its_own_thread_and_journals_in_order_on_the_host_loop(self):
        """v23: strike timers never share the host loop with Telegram, scans or journal writes."""
        import threading
        actor = actor_fixture()
        actor.my_info = SimpleNamespace(id=9100000001, username="demo_player_01")
        msg = message("qyz_fixture_thread", bot_username="hantianzun23_bot")
        actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(msg))
        actor.client.get_me = AsyncMock(return_value=actor.my_info)
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        fields = {"user": json.dumps({"id": 9100000001, "username": "demo_player_01"}), "start_param": entry.token,
                  "auth_date": str(int(time.time())), "hash": "a" * 64}
        url = "https://asc.aiopenai.app/miniapp/xianxia-verify#" + urlencode({"tgWebAppData": urlencode(fields)})
        main_thread = threading.current_thread()
        seen = {"runtime_thread": None, "trace_threads": set(), "phases": []}
        clock = FakeClock()
        api = BattleFixture(clock, count=2)

        class Context:
            async def __aenter__(self):
                return api
            async def __aexit__(self, *_args):
                pass

        def runtime(**kwargs):
            seen["runtime_thread"] = threading.current_thread()
            original_trace = kwargs["trace"]
            return ProtocolRuntime(**kwargs, monotonic=clock.now, sleep=clock.sleep)

        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        original_trace = monitor._trace

        def spy_trace(current, record):
            seen["trace_threads"].add(threading.current_thread())
            seen["phases"].append(record["phase"])
            return original_trace(current, record)

        monitor._trace = spy_trace
        with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "request_official_webview_url", new=AsyncMock(return_value=url)), \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "ProtocolRuntime", side_effect=runtime):
            result = await monitor.run_entry(entry)
        self.assertEqual(result["status"], "settled_perfect")
        self.assertIsNot(seen["runtime_thread"], main_thread)
        self.assertTrue(seen["runtime_thread"].name.startswith("qyz-battle"))
        self.assertEqual(seen["trace_threads"], {main_thread})
        phases = seen["phases"]
        self.assertLess(phases.index("start_submitted"), phases.index("begin_submitted"))
        self.assertLess(phases.index("begin_confirmed"), phases.index("finish_confirmed"))
        self.assertEqual(phases[-1], "settled_perfect")
        record = actor.state["world_boss_events"][-1]
        self.assertEqual((record["status"], record["accepted_hits"]), ("settled_perfect", 2))

    async def test_settlement_evidence_waits_for_the_other_battles(self):
        """v25: a finished account journals counts at once; the heavy record and archive wait."""
        actor = actor_fixture()
        actor.my_info = SimpleNamespace(id=9100000001, username="demo_player_01")
        msg = message("qyz_fixture_deferred", bot_username="hantianzun23_bot")
        actor.client.get_messages = AsyncMock(side_effect=messages_for_chat(msg))
        actor.client.get_me = AsyncMock(return_value=actor.my_info)
        entry = host.extract_world_boss_entry(msg, sender=msg.sender)
        fields = {"user": json.dumps({"id": 9100000001, "username": "demo_player_01"}), "start_param": entry.token,
                  "auth_date": str(int(time.time())), "hash": "a" * 64}
        url = "https://asc.aiopenai.app/miniapp/xianxia-verify#" + urlencode({"tgWebAppData": urlencode(fields)})
        clock = FakeClock()
        api = BattleFixture(clock, count=2)

        class Context:
            async def __aenter__(self):
                return api
            async def __aexit__(self, *_args):
                pass

        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        archived = []
        monitor._archive_event = AsyncMock(side_effect=lambda _entry: archived.append(True))
        host._battle_started()  # another account is still fighting
        try:
            with patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
                 patch.object(host, "request_official_webview_url", new=AsyncMock(return_value=url)), \
                 patch.object(host, "HttpTransport", return_value=Context()), \
                 patch.object(host, "ProtocolRuntime",
                              side_effect=lambda **kw: ProtocolRuntime(**kw, monotonic=clock.now, sleep=clock.sleep)):
                task = asyncio.create_task(monitor.run_entry(entry))
                for _ in range(400):
                    record = (actor.state.get("world_boss_events") or [{}])[-1]
                    # The trace can publish its terminal phase just before the
                    # host attaches the lightweight result from the worker.
                    if record.get("status") == "settled_perfect" and "protocol_result" in record:
                        break
                    await asyncio.sleep(0.01)
                record = actor.state["world_boss_events"][-1]
                self.assertEqual(record["status"], "settled_perfect")
                light = record["protocol_result"]
                self.assertEqual(light["accepted_hits"], 2)
                self.assertIn("server_result", light)
                self.assertNotIn("transport_diagnostics", light)
                self.assertNotIn("diagnostics", light.get("reference_outcome", {}))
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertEqual(archived, [])
                host._battle_finished()
                result = await asyncio.wait_for(task, 5)
        finally:
            host._BATTLES["in_flight"] = 0
        self.assertEqual(result["status"], "settled_perfect")
        self.assertEqual(archived, [True])
        full = actor.state["world_boss_events"][-1]["protocol_result"]
        self.assertIn("diagnostics", full["reference_outcome"])
        self.assertNotIn(entry.fingerprint, monitor._inflight)

    def test_light_result_keeps_counts_verdicts_and_failure_summary_only(self):
        result = {"status": "runtime_error", "accepted_hits": 3, "server_perfects": 2,
                  "server_result": {"grade": "甲等"}, "server_damage_multiplier_min": 1.0,
                  "transport_diagnostics": [1] * 10, "window_states": {"w": {}}, "verification_requests": [],
                  "reference_outcome": {"grade": "甲等", "diagnostics": {"hits": [1] * 10}},
                  "diagnostics": {"browser": {"browser_event": "widget_ready"}, "hits": [1] * 10}}
        light = host.light_protocol_result(result)
        self.assertEqual(light["server_perfects"], 2)
        self.assertEqual(light["server_result"], {"grade": "甲等"})
        self.assertEqual(light["reference_outcome"], {"grade": "甲等"})
        self.assertEqual(light["diagnostics"], {"browser": {"browser_event": "widget_ready"}})
        for key in ("transport_diagnostics", "window_states", "verification_requests"):
            self.assertNotIn(key, light)
        # The full result stays intact for the deferred record.
        self.assertIn("diagnostics", result["reference_outcome"])

    async def test_unknown_or_submitted_mutation_is_never_redispatched(self):
        for prior_status in ("begin_submitted", "hit_submitted", "finish_submitted", "unknown_mutation_outcome", "settled_partial"):
            actor = actor_fixture()
            msg = message("qyz_fixture_unknown")
            entry = host.extract_world_boss_entry(msg, sender=msg.sender)
            assert entry is not None
            actor.state["world_boss_events"] = [{"fingerprint":entry.fingerprint, "status":prior_status}]
            monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
            monitor.run_entry = AsyncMock(return_value={"status":"fixture_finished"})
            self.assertFalse(await monitor.process_message(msg), prior_status)
            monitor.run_entry.assert_not_awaited()

    async def test_failure_before_any_mutation_is_redelivered_inside_the_window(self):
        actor = actor_fixture()
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        msg = message("qyz_fixture_redeliver")
        entry = host.extract_world_boss_entry(msg)
        actor.client.get_messages = AsyncMock(side_effect=[ConnectionError("rpc"), msg])
        fields = {"user": json.dumps({"id": 9100000001, "username": "demo_player_01"}), "start_param": entry.token,
                  "auth_date": str(int(time.time())), "hash": "a" * 64}
        url = "https://asc.aiopenai.app/miniapp/xianxia-verify#" + urlencode({"tgWebAppData": urlencode(fields)})

        class Context:
            async def __aenter__(self):
                return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}))
            async def __aexit__(self, *_args):
                pass

        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        monitor._run_battle_isolated = AsyncMock(return_value={"status": "settled_perfect"})
        with patch.object(host, "ENTRY_RETRY_SECONDS", 0), \
             patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "request_official_webview_url", new=AsyncMock(return_value=url)), \
             patch.object(host, "HttpTransport", return_value=Context()):
            self.assertTrue(await monitor.process_message(msg))
            for _ in range(200):
                await asyncio.sleep(0)
                if monitor._run_battle_isolated.await_count and not monitor._tasks:
                    break
        monitor._run_battle_isolated.assert_awaited_once()
        self.assertEqual(actor.client.get_messages.await_count, 2)
        record = actor.state["world_boss_events"][-1]
        self.assertEqual(record["status"], "settled_perfect")
        self.assertNotIn("retry_at_ts", record)

    async def test_flood_wait_retries_after_the_wait_but_uncertain_outcomes_never_retry(self):
        from telethon.errors import FloodWaitError
        actor = actor_fixture()
        msg = message("qyz_fixture_flood")
        entry = host.extract_world_boss_entry(msg)
        actor.client.get_messages = AsyncMock(side_effect=FloodWaitError(request=None, capture=30))
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        await monitor.run_entry(entry)
        self.assertEqual(monitor._event(entry)["status"], "runtime_error")
        self.assertGreaterEqual(monitor._event(entry)["retry_at_ts"], time.time() + 29)
        actor.client.get_messages = AsyncMock(
            side_effect=host.ProtocolError("api_unreachable", phase="start", uncertain=True))
        await monitor.run_entry(entry)
        self.assertEqual(monitor._event(entry)["status"], "unknown_mutation_outcome")
        self.assertNotIn("retry_at_ts", monitor._event(entry))

    async def test_entry_falls_back_to_a_recent_page_policy(self):
        api = SimpleNamespace(security_policy=AsyncMock(side_effect=host.ProtocolError(
            "official_security_policy_unavailable", phase="preflight", http_status=503)))

        class Context:
            async def __aenter__(self):
                return api
            async def __aexit__(self, *_args):
                pass

        host._SECURITY_POLICY.update(policy={"enabled": False}, at=time.time() - 200)
        with patch.object(host, "HttpTransport", return_value=Context()):
            self.assertEqual(await host.official_security_policy("tg9100000001", fallback_age=240),
                             {"enabled": False})
            with self.assertRaises(host.ProtocolError):
                await host.official_security_policy("tg9100000001")
            host._SECURITY_POLICY["at"] = time.time() - 241
            with self.assertRaises(host.ProtocolError):
                await host.official_security_policy("tg9100000001", fallback_age=240)

    async def test_entry_expiring_during_webview_request_never_starts_game_runtime(self):
        simulated_now = datetime.now(timezone.utc)

        class ClockDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return simulated_now.astimezone(tz) if tz else simulated_now.replace(tzinfo=None)

        actor = actor_fixture()
        msg = message("qyz_fixture_expiring")
        msg.date = ClockDatetime.fromtimestamp(simulated_now.timestamp(), timezone.utc)
        actor.client.get_messages = AsyncMock(return_value=msg)
        actor.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))
        entry = host.extract_world_boss_entry(msg)
        fields = {"user": json.dumps({"id": 9100000001, "username": "demo_player_01"}),
                  "start_param": entry.token, "auth_date": str(int(time.time())), "hash": "a" * 64}
        url = "https://asc.aiopenai.app/miniapp/xianxia-verify#" + urlencode({"tgWebAppData": urlencode(fields)})

        async def delayed_webview(*_args):
            nonlocal simulated_now
            await asyncio.sleep(0)
            simulated_now += timedelta(seconds=61)
            return url

        api = SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}), post_json=AsyncMock())

        class Context:
            async def __aenter__(self):
                return api

            async def __aexit__(self, *_args):
                pass

        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        with patch.object(host, "datetime", ClockDatetime), \
             patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)), \
             patch.object(host, "request_official_webview_url", new=AsyncMock(side_effect=delayed_webview)) as webview, \
             patch.object(host, "HttpTransport", return_value=Context()), \
             patch.object(host, "ProtocolRuntime") as runtime:
            self.assertTrue(host.message_recent_enough(msg))
            result = await monitor.run_entry(entry)
        self.assertEqual(result["status"], "blocked_entry_expired")
        self.assertEqual(result["phase"], "source")
        webview.assert_awaited_once()
        runtime.assert_not_called()
        api.post_json.assert_not_awaited()
        self.assertFalse(actor.state["world_boss_events"][-1].get("mutation_started"))

    async def test_two_distinct_entries_cannot_overlap_within_one_account(self):
        actor = actor_fixture()
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        released = asyncio.Event()
        active = peak = 0
        async def work(entry):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await released.wait()
                return {"status": "fixture_finished"}
            finally:
                active -= 1
        monitor.run_entry = work
        self.assertTrue(await monitor.process_message(message("qyz_fixture_first")))
        self.assertTrue(await monitor.process_message(message("qyz_fixture_second")))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(peak, 1, "Different entry fingerprints must still share an account mutex")
        finally:
            released.set()
            await asyncio.gather(*list(monitor._tasks), return_exceptions=True)


    async def test_live_entry_text_mentioning_the_board_is_still_admitted(self):
        """2026-09-21 regression: the entry post says "…可查看贡献榜" and was dropped as a board."""
        actor = actor_fixture()
        msg = message("qyz_fixture_event_132", bot_username="hantianzun23_bot")
        msg.raw_text = ("━━━━━━━━━━━━━━━\n【世界通告｜真仙试锋开启】\n━━━━━━━━━━━━━━━\n"
                        "万魂幡主·阴罗真影 青元子 降下阴罗法身。\n\n参战\n点击下方按钮可直接进入真仙战场。\n"
                        ".世界boss 可查看战况，.世界boss榜 可查看贡献榜。\n\n贡献提示\n- 本轮废弃破幡、镇魂、护阵，强攻即为主要攻击。")
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        boards = []
        with patch.object(monitor, "_run_exclusive", new=AsyncMock(return_value={"status": "fixture"})), \
             patch.object(host, "_write_shared_board", side_effect=lambda m, b: boards.append(b)):
            self.assertTrue(await monitor.process_message(msg, source="telegram_update"))
            await asyncio.gather(*monitor._tasks)
        self.assertEqual([], boards)
        record = actor.state["world_boss_events"][-1]
        self.assertEqual(record["message_id"], msg.id)
        self.assertEqual(record["fingerprint"], host.extract_world_boss_entry(msg).fingerprint)

    async def test_settlement_post_without_a_deep_link_only_records_the_board(self):
        actor = actor_fixture()
        msg = message("qyz_fixture_unused")
        msg.buttons = []
        msg.entities = []
        msg.raw_text = ("【世界通告｜真仙试锋败退】\n战果\n- 结果：天道败退\n- 参战：30 人\n\n贡献榜\n"
                        "1. @sample_peer_16 - 3760 分｜强攻 16｜伤害 7.42亿亿\n2. @sample_peer_10 - 3760 分｜强攻 16｜伤害 7.38亿亿\n")
        monitor = host.OfficialWorldBossMonitor(actor, actor.account_key)
        boards = []
        with patch.object(monitor, "_run_exclusive", new=AsyncMock(side_effect=AssertionError("no battle for a board"))), \
             patch.object(host, "_write_shared_board", side_effect=lambda m, b: boards.append(b)):
            self.assertFalse(await monitor.process_message(msg, source="telegram_update"))
        self.assertEqual(1, len(boards))
        self.assertEqual((boards[0]["participants"], boards[0]["row_count"]), (30, 2))
        self.assertFalse(actor.state.get("world_boss_events"))

    async def test_stop_unregisters_exact_handler_builders(self):
        removed = []
        client = SimpleNamespace(
            remove_event_handler=lambda callback, builder: removed.append((callback, builder))
        )
        actor = SimpleNamespace(client=client, state={}, save_state=lambda: None)
        monitor = host.OfficialWorldBossMonitor(actor, "tg9100000001")
        monitor._new_handler = monitor._on_message
        monitor._edit_handler = monitor._on_message
        monitor._new_builder = object()
        monitor._edit_builder = object()
        await monitor.stop()
        self.assertEqual(
            [(monitor._on_message, monitor._new_builder), (monitor._on_message, monitor._edit_builder)],
            removed,
        )

    async def test_one_account_startup_failure_is_supervised_without_stopping_the_fleet(self):
        stop = asyncio.Event()
        calls = 0

        async def fake_run(_account, event):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")
            event.set()

        with patch.object(host, "run_account", side_effect=fake_run), \
             patch.object(host.asyncio, "sleep", return_value=None), \
             patch.object(host.logging, "error"):
            await host.supervise_account("tg9100000001", stop)
        self.assertEqual(2, calls)


class HostConnectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(host._SECURITY_POLICY, clear=True))
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "TG_TELEGRAM_CONNECTION_GATE_PATH": str(Path(self.temp) / "connection.lock")
        }))
        self.actor = actor_fixture()
        self.actor.state.update(world_boss_monitor_active=True, world_boss_events=[{
            "fingerprint": "a" * 64, "status": "runtime_error",
            "error": "official_webview_origin_invalid", "mutation_started": False,
        }])
        self.client = self.actor.client
        self.client.connected = False
        self.client.is_connected = lambda: self.client.connected
        self.client.disconnected = asyncio.get_running_loop().create_future()
        self.client.disconnected.set_exception(ConnectionResetError("terminal disconnect"))
        self.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9100000001, username="demo_player_01"))

        async def connect():
            self.client.connected = True
            self.client.disconnected = asyncio.get_running_loop().create_future()

        self.connect = connect
        self.client.connect = AsyncMock(side_effect=connect)
        self.monitor = host.OfficialWorldBossMonitor(self.actor, self.actor.account_key)
        self.monitor.target_chats = list(host.TARGET_CHAT_IDS)
        self.membership = self.enterContext(patch.object(host, "verify_account_membership", new=AsyncMock(return_value=True)))
        self.source = self.enterContext(patch.object(host, "latest_source_message", new=AsyncMock(return_value=None)))
        self.webview = self.enterContext(patch.object(host, "request_official_webview_url", new=AsyncMock(
            side_effect=AssertionError("health recovery must never open a WebView"))))

        class Policy:
            async def __aenter__(self):
                return SimpleNamespace(security_policy=AsyncMock(return_value={"enabled": False}))

            async def __aexit__(self, *_args):
                pass

        self.enterContext(patch.object(host, "HttpTransport", return_value=Policy()))

    async def asyncTearDown(self):
        await self.monitor.stop()
        self.webview.assert_not_awaited()

    async def test_terminal_disconnect_retries_and_preserves_failed_battle(self):
        original_events = json.loads(json.dumps(self.actor.state["world_boss_events"]))
        self.client.connect.side_effect = ConnectionRefusedError("listener unavailable")
        await self.monitor.refresh_health()
        failed = self.actor.state["world_boss_health"]
        self.assertFalse(failed["connected"])
        self.assertIn("telegram_disconnected", failed["blockers"])
        self.client.get_me.assert_not_awaited()

        self.client.connect.side_effect = self.connect
        await self.monitor.refresh_health()
        recovered = self.actor.state["world_boss_health"]
        self.assertTrue(all(recovered[key] for key in ("connected", "identity_verified", "membership_verified", "source_verified")))
        self.source.assert_has_awaits([call(self.client, chat_id, full=False) for chat_id in host.TARGET_CHAT_IDS])
        self.assertEqual(recovered["blockers"], ["latest_battle_not_successful"])
        self.assertEqual(recovered["status"], "runtime_error")
        self.assertEqual(self.actor.state["world_boss_events"], original_events)
        self.assertEqual(self.client.connect.await_count, 2)
        self.assertIs(self.monitor._disconnect_waiter, self.client.disconnected)

    async def test_reconnected_socket_still_requires_identity_and_both_sources(self):
        self.client.get_me.return_value = SimpleNamespace(id=123, username="other_account")
        await self.monitor.refresh_health()
        health = self.actor.state["world_boss_health"]
        self.assertTrue(health["connected"])
        self.assertFalse(health["identity_verified"])
        self.assertIn("identity_unverified", health["blockers"])

        self.client.get_me.return_value = SimpleNamespace(id=9100000001, username="demo_player_01")
        self.source.side_effect = OSError("source unavailable")
        await self.monitor.refresh_health()
        health = self.actor.state["world_boss_health"]
        self.assertTrue(health["connected"])
        self.assertFalse(health["source_verified"])
        self.assertIn("source_unavailable", health["blockers"])

    async def test_health_does_not_reconnect_or_cancel_an_active_battle(self):
        self.actor.state["world_boss_health"] = {"status": "battle_in_progress", "accepted_hits": 7}
        battle = asyncio.create_task(asyncio.Event().wait())
        self.monitor._tasks.add(battle)
        async with self.monitor._account_lock:
            await self.monitor.refresh_health()
        self.client.connect.assert_not_awaited()
        self.client.get_me.assert_not_awaited()
        self.source.assert_not_awaited()
        self.assertFalse(battle.done())
        self.assertEqual(self.actor.state["world_boss_health"]["status"], "battle_in_progress")
        self.assertEqual(self.actor.state["world_boss_health"]["accepted_hits"], 7)


if __name__ == "__main__":
    unittest.main()
