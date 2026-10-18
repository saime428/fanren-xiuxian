import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import recipe_notice_monitor as recipe_module
from event_monitor_runtime import RUNTIME_VERSION
from scripts import xiuxian_healthcheck as healthcheck

from recipe_notice_monitor import (
    deliver_with_hermes,
    eligible_recipe_notice,
    extract_recipe_notice,
    format_notification,
    process_recipe_message,
)
from scripts.xiuxian_healthcheck import flow_has_process


EXAMPLE_TEXT = """【温养双修·大成】
在同参契印的加持下，你与 @demo_player_01 灵力完美交融，事半功倍！
@demo_player_03 修为增加了 64 点，并获得 15 点宗门贡献！
@demo_player_01 修为增加了 39 点！

✨ 天赐机缘！
双方在神魂交融之际，脑海中同时浮现出一卷玄奥的纹路，共同领悟了【凝魂丹丹方】！"""


class RecipeNoticeMonitorTests(unittest.TestCase):
    def test_official_tupu_reward_12309907_is_delivered_once_with_its_receipt(self):
        message = SimpleNamespace(
            id=12309907, chat_id=-1009400000004, sender_id=8816935632,
            raw_text=EXAMPLE_TEXT.replace("64 点", "61 点").replace("39 点", "36 点").replace("凝魂丹丹方", "紫铖兜图谱"),
            reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=12309906),
        )
        deliver = AsyncMock(return_value={"target": "telegram:9100000001", "message_id": 123, "success": True})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notification_state.json"
            kwargs = dict(state_path=path, trusted_sender_ids={8816935632}, topic_id=7310786,
                          chat_username="example_game_group", deliver=deliver)
            self.assertTrue(asyncio.run(process_recipe_message(message, **kwargs)))
            self.assertFalse(asyncio.run(process_recipe_message(message, **kwargs)))
            row, = json.loads(path.read_text())["delivered"]
        self.assertEqual(deliver.await_count, 1)
        text = deliver.await_args.args[0]
        self.assertIn("共同领悟了【紫铖兜图谱】", text)
        self.assertIn("12309907", text)
        self.assertEqual(row["actors"], ["demo_player_01", "demo_player_03"])
        self.assertEqual(row["delivery_receipt"]["message_id"], 123)

    def test_all_extra_reward_types_and_unknown_formats_preserve_the_original_details(self):
        for reward in ["获得【紫铖兜图谱】！", "獲得【紫鋮兜圖譜】！", "获得【未知新法宝】x2 和【灵石】x100！",
                       "领悟了一门新神通：无名诀。", "奖励内容尚待展开，请查看原文。"]:
            with self.subTest(reward=reward):
                text = "@demo_player_01 与 @demo_player_03\n✨ 天赐机缘！\n" + reward
                message = SimpleNamespace(id=1, chat_id=-1009400000004, sender_id=8816935632,
                                          raw_text=text, reply_to=SimpleNamespace(reply_to_top_id=7310786))
                notice = eligible_recipe_notice(message, {8816935632}, 7310786)
                self.assertIsNotNone(notice)
                formatted = format_notification(notice, chat_username="example_game_group", topic_id=7310786, message_id=1)
                self.assertIn(reward, formatted)
                self.assertIn("@demo_player_01、@demo_player_03", formatted)

    def test_traditional_bonus_marker_and_other_player_mentions(self):
        message = SimpleNamespace(id=1, chat_id=-1009400000004, sender_id=8816935632,
                                  raw_text="天賜機緣！\n@demo_player_01 與 @sample_peer_12Else 獲得【新物品】！",
                                  reply_to=SimpleNamespace(reply_to_top_id=7310786))
        notice = eligible_recipe_notice(message, {8816935632}, 7310786)
        text = format_notification(notice, chat_username="example_game_group", topic_id=7310786, message_id=1)
        self.assertIn("【新物品】", text)
        self.assertIn("@demo_player_01", text)
        self.assertNotIn("@sample_peer_12Else", text)
        message.raw_text = "天赐机缘！\n@sample_peer_12Else 获得【新物品】！"
        self.assertIsNone(eligible_recipe_notice(message, {8816935632}, 7310786))

    def test_ordinary_cultivation_without_an_extra_reward_does_not_notify(self):
        self.assertIsNone(extract_recipe_notice("@demo_player_01 修为增加了 36 点！"))

    def test_extracts_recipe_and_unique_actors_from_real_notice(self) -> None:
        notice = extract_recipe_notice(EXAMPLE_TEXT)

        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertEqual("凝魂丹丹方", notice.recipe)
        self.assertEqual(("demo_player_01", "demo_player_03"), notice.actors)

    def test_requires_verified_sender_and_canonical_topic(self) -> None:
        valid = SimpleNamespace(
            id=12231446,
            chat_id=-1009400000004,
            sender_id=8735907987,
            raw_text=EXAMPLE_TEXT,
            reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=12231445),
        )
        forged = SimpleNamespace(**{**valid.__dict__, "sender_id": 999})
        wrong_topic = SimpleNamespace(
            **{**valid.__dict__, "reply_to": SimpleNamespace(reply_to_top_id=123, reply_to_msg_id=12231445)}
        )

        self.assertIsNotNone(eligible_recipe_notice(valid, {8735907987}, 7310786))
        self.assertIsNone(eligible_recipe_notice(forged, {8735907987}, 7310786))
        self.assertIsNone(eligible_recipe_notice(wrong_topic, {8735907987}, 7310786))
        forwarded = SimpleNamespace(**{**valid.__dict__, "fwd_from": object()})
        self.assertIsNone(eligible_recipe_notice(forwarded, {8735907987}, 7310786))

    def test_successful_push_is_delivered_once_and_persisted(self) -> None:
        message = SimpleNamespace(
            id=12231446,
            chat_id=-1009400000004,
            sender_id=8735907987,
            raw_text=EXAMPLE_TEXT,
            reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=12231445),
        )
        delivered: list[str] = []

        async def deliver(text: str) -> None:
            delivered.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "notification_state.json"
            first = asyncio.run(
                process_recipe_message(
                    message,
                    state_path=state_path,
                    trusted_sender_ids={8735907987},
                    topic_id=7310786,
                    chat_username="example_game_group",
                    deliver=deliver,
                )
            )
            second = asyncio.run(
                process_recipe_message(
                    message,
                    state_path=state_path,
                    trusted_sender_ids={8735907987},
                    topic_id=7310786,
                    chat_username="example_game_group",
                    deliver=deliver,
                )
            )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(1, len(delivered))
        self.assertIn("【凝魂丹丹方】", delivered[0])
        self.assertIn("https://t.me/example_game_group/7310786/12231446", delivered[0])

    def test_failed_push_is_not_marked_delivered(self) -> None:
        message = SimpleNamespace(
            id=12231446,
            chat_id=-1009400000004,
            sender_id=8735907987,
            raw_text=EXAMPLE_TEXT,
            reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=12231445),
        )
        attempts = 0

        async def deliver(_text: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("delivery failed")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "notification_state.json"
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    process_recipe_message(
                        message,
                        state_path=state_path,
                        trusted_sender_ids={8735907987},
                        topic_id=7310786,
                        chat_username="example_game_group",
                        deliver=deliver,
                    )
                )
            retried = asyncio.run(
                process_recipe_message(
                    message,
                    state_path=state_path,
                    trusted_sender_ids={8735907987},
                    topic_id=7310786,
                    chat_username="example_game_group",
                    deliver=deliver,
                )
            )

        self.assertTrue(retried)
        self.assertEqual(2, attempts)

    def test_wrapper_and_launchagent_use_push_with_hourly_recovery(self) -> None:
        wrapper = Path("scripts/run_recipe_notice_push_tg9100000001.sh").read_text(encoding="utf-8")
        plist = Path("scripts/ai.hermes.xiuxian.tg9100000001.recipe_notice.return_observer.plist").read_text(encoding="utf-8")
        source = Path("recipe_notice_monitor.py").read_text(encoding="utf-8")

        self.assertIn("XIU_ACCOUNT_SLUG='tg9100000001'", wrapper)
        self.assertIn("XIU_FLOW_SLUG='recipe_notice'", wrapper)
        self.assertIn("TG_TARGET='@example_game_group'", wrapper)
        self.assertIn("TG_TOPIC_ID='7310786'", wrapper)
        self.assertIn("TG_RECIPE_RECONCILE_SECONDS='3600'", wrapper)
        self.assertIn("TG_RECIPE_DELIVERY_TARGET='telegram:9100000001'", wrapper)
        self.assertIn("TG_RECIPE_TARGET_USERNAMES='demo_player_01,demo_player_03'", wrapper)
        self.assertIn("events.NewMessage", source)
        self.assertIn("events.MessageEdited", source)
        self.assertIn("KeepAlive", plist)
        self.assertIn("RunAtLoad", plist)
        self.assertNotIn("StartInterval", plist)
        registry = json.loads(Path("config/active_flows.json").read_text(encoding="utf-8"))
        row = next(
            item
            for item in registry["flows"]
            if item.get("account") == "tg9100000001" and item.get("flow") == "recipe_notice"
        )
        self.assertTrue(row["enabled"])
        self.assertEqual(["return_observer"], row["kinds"])

    def test_healthcheck_recognizes_recipe_push_process(self) -> None:
        process_line = (
            "67040 /Users/operator/.hermes/workspace/projects/xiuxian/.venv/bin/python3 "
            "-u ./recipe_notice_monitor.py tg9100000001 recipe_notice "
            "state/accounts/tg9100000001/recipe_notice"
        )

        self.assertTrue(
            flow_has_process(
                process_line,
                "tg9100000001",
                "recipe_notice",
                "return_observer",
            )
        )

    def test_other_players_recipe_does_not_trigger_notification(self):
        message = SimpleNamespace(id=12267104, chat_id=-1009400000004, sender_id=8735907987,
                                  raw_text=EXAMPLE_TEXT.replace('@demo_player_01', '@WalterWA2000').replace('@demo_player_03', '@wisemole'),
                                  reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=12267102))
        delivered = []
        async def deliver(text):
            delivered.append(text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'notification_state.json'
            result = asyncio.run(process_recipe_message(message, state_path=path, trusted_sender_ids={8735907987},
                                 topic_id=7310786, chat_username='example_game_group', deliver=deliver))
            self.assertFalse(result)
            self.assertFalse(path.exists())
        self.assertEqual(delivered, [])

    def test_each_selected_actor_matches_case_insensitively_without_other_players(self):
        # Upper/mixed-case mentions must still match the lower-case targets;
        # str.replace is case-sensitive, so swapping out @demo_player_03 leaves
        # the upper/mixed-case actor under test as the only target in the text.
        for actor in ['DEMO_PLAYER_01', 'Demo_Player_03']:
            message = SimpleNamespace(id=1, chat_id=-1009400000004, sender_id=8735907987,
                                      raw_text=EXAMPLE_TEXT.replace('@demo_player_01', '@' + actor).replace('@demo_player_03', '@sample_peer_12Else'),
                                      reply_to=SimpleNamespace(reply_to_top_id=7310786))
            notice = eligible_recipe_notice(message, {8735907987}, 7310786)
            self.assertEqual(notice.actors, (actor.lower(),))

    def test_similar_names_and_foreign_chat_do_not_match(self):
        message = SimpleNamespace(id=1, chat_id=-1009400000004, sender_id=8735907987,
                                  raw_text=EXAMPLE_TEXT.replace('@demo_player_01', '@demo_player_01_other').replace('@demo_player_03', '@xiuxian__002'),
                                  reply_to=SimpleNamespace(reply_to_top_id=7310786))
        self.assertIsNone(eligible_recipe_notice(message, {8735907987}, 7310786))
        message.raw_text = EXAMPLE_TEXT
        message.chat_id = -1009400000005
        self.assertIsNone(eligible_recipe_notice(message, {8735907987}, 7310786))

    def test_filter_change_preserves_previous_delivery_deduplication(self):
        message = SimpleNamespace(id=12231446, chat_id=-1009400000004, sender_id=8735907987,
                                  raw_text=EXAMPLE_TEXT.replace('@demo_player_03', '@sample_peer_12Else'),
                                  reply_to=SimpleNamespace(reply_to_top_id=7310786))
        async def deliver(_text):
            self.fail('An already delivered recipe must not be delivered again')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'notification_state.json'
            path.write_text(json.dumps({'delivered': [{'key': 'legacy-actors-key', 'message_id': 12231446,
                            'recipe': '凝魂丹丹方', 'actors': ['demo_player_01', 'sample_peer_12Else']}]}))
            result = asyncio.run(process_recipe_message(message, state_path=path, trusted_sender_ids={8735907987},
                                 topic_id=7310786, chat_username='example_game_group', deliver=deliver))
        self.assertFalse(result)


class RecipeHealthTests(unittest.TestCase):
    def test_registered_recipe_requires_structured_health_while_worker_is_running(self):
        entries, error = healthcheck.load_active_config()
        self.assertIsNone(error)
        entries = [entry for entry in entries if entry['flow'] == 'recipe_notice']
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]['monitor_health_required'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / 'state' / 'accounts'
            flow_dir = state_root / 'tg9100000001' / 'recipe_notice'
            flow_dir.mkdir(parents=True)
            proc = 'user 123 0 0 python -u ./recipe_notice_monitor.py --account tg9100000001 --flow recipe_notice'
            with (patch.object(healthcheck, 'XIUXIAN_ROOT', root),
                  patch.object(healthcheck, 'STATE_ROOT', state_root)):
                result, = healthcheck.collect_active_items(proc, entries, {})
                self.assertEqual((result.status, result.severity), ('monitor_health_missing', 'bad'))
                now = time.time() - 2
                data = {'runtime_version': RUNTIME_VERSION, 'account': 'tg9100000001', 'flow': 'recipe_notice',
                        'kind': 'return_observer', 'pid': 123, 'ts': now, 'started_at_ts': now - 60,
                        'last_success_ts': now, 'listener_verified_ts': now, 'source_targets': ['@example_game_group'],
                        'listener_connected': True, 'status': 'healthy', 'reconcile_seconds': 60, 'error_type': None}
                (flow_dir / 'observer_health.json').write_text(json.dumps(data))
                result, = healthcheck.collect_active_items(proc, entries, {})
                self.assertEqual((result.status, result.severity), ('monitor_healthy', 'ok'))

    def test_verified_recipe_source_clears_only_older_log_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time() - 2
            data = {'runtime_version': RUNTIME_VERSION, 'account': 'tg9100000001', 'flow': 'recipe_notice',
                    'kind': 'return_observer', 'pid': 123, 'ts': now, 'started_at_ts': now - 60,
                    'last_success_ts': now, 'listener_verified_ts': now, 'source_targets': ['@example_game_group'],
                    'listener_connected': True, 'status': 'healthy', 'reconcile_seconds': 60, 'error_type': None}
            state = root / 'observer_health.json'
            state.write_text(json.dumps(data))
            log = root / 'observer.launchd.log'
            log.write_text('Traceback (most recent call last):\nConnectionError: old\n')
            os.utime(log, (now - 60, now - 60))
            proc = 'user 123 0 0 python -u ./recipe_notice_monitor.py'
            def classify():
                return healthcheck.classify_event_monitor('tg9100000001', 'recipe_notice', 'return_observer', root, proc, True, {})
            self.assertEqual(classify().severity, 'ok')
            log.write_text('Traceback (most recent call last):\nConnectionError: current\n')
            self.assertEqual(classify().severity, 'bad')

    def test_unverified_or_wrong_source_recipe_state_remains_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time() - 2
            data = {'runtime_version': RUNTIME_VERSION, 'account': 'tg9100000001', 'flow': 'recipe_notice',
                    'kind': 'return_observer', 'pid': 123, 'ts': now, 'started_at_ts': now - 60,
                    'last_success_ts': now, 'listener_verified_ts': now, 'source_targets': ['-1009400000005'],
                    'listener_connected': True, 'status': 'healthy', 'reconcile_seconds': 60, 'error_type': None}
            proc = 'user 123 0 0 python -u ./recipe_notice_monitor.py'
            for changes in [{}, {'source_targets': ['@example_game_group'], 'listener_connected': False},
                            {'source_targets': ['@example_game_group'], 'last_success_ts': now - 1000}]:
                (root / 'observer_health.json').write_text(json.dumps({**data, **changes}))
                result = healthcheck.classify_event_monitor('tg9100000001', 'recipe_notice', 'return_observer', root, proc, True, {})
                self.assertEqual(result.severity, 'bad')


class RecipeHealthLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_delivery_failure_during_probe_requires_history_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            finished = asyncio.get_running_loop().create_future()
            message = SimpleNamespace(id=42, date=datetime.now(timezone.utc))
            (Path(tmp) / 'notification_state.json').write_text(json.dumps({'started_at_ts': time.time() - 10}))
            handlers, limits, snapshots = [], [], []

            async def get_messages(_entity, *, limit):
                limits.append(limit)
                if len(limits) == 2:
                    await handlers[0](SimpleNamespace(message=message))
                return [message] if len(limits) == 3 else []

            client = SimpleNamespace(is_connected=lambda: True, disconnected=finished,
                                     get_me=AsyncMock(return_value=SimpleNamespace(id=9100000001)),
                                     get_entity=AsyncMock(return_value=SimpleNamespace(id=9400000004)),
                                     get_messages=get_messages, add_event_handler=lambda handler, _: handlers.append(handler))
            real_sleep = asyncio.sleep

            async def tick(seconds):
                if seconds == 60:
                    snapshots.append(json.loads((Path(tmp) / 'observer_health.json').read_text()))
                    if len(snapshots) == 3 and not finished.done():
                        finished.set_result(None)
                await real_sleep(0)

            async def run_until_disconnected():
                await finished

            client.run_until_disconnected = run_until_disconnected
            settings = {'TG_STATE_DIR': tmp, 'TG_API_ID': '1', 'TG_API_HASH': 'fixture', 'TG_SESSION_NAME': 'fixture',
                        'TG_RECIPE_TRUSTED_SENDER_IDS': '8735907987', 'TG_RECIPE_RECONCILE_SECONDS': '3600'}
            with (patch.dict(os.environ, settings, clear=True), patch.object(recipe_module, 'load_dotenv'),
                  patch('transport.telegram_client', return_value=client),
                  patch('transport.disconnect_telegram_client', new_callable=AsyncMock),
                  patch.object(recipe_module, 'process_recipe_message', new_callable=AsyncMock,
                               side_effect=[RuntimeError('delivery unavailable'), True]) as process,
                  patch.object(recipe_module.asyncio, 'sleep', side_effect=tick)):
                await asyncio.wait_for(recipe_module._run_persistent_locked(), 2)
            self.assertEqual(limits, [500, 1, 500])
            self.assertEqual([row['status'] for row in snapshots], ['healthy', 'failed', 'healthy'])
            self.assertEqual(snapshots[1]['error_stage'], 'recipe_delivery')
            self.assertEqual(process.await_count, 2)

    async def test_full_history_is_hourly_but_health_reads_the_source_each_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            finished = asyncio.get_running_loop().create_future()
            client = SimpleNamespace(is_connected=lambda: True, disconnected=finished,
                                     get_me=AsyncMock(return_value=SimpleNamespace(id=9100000001)),
                                     get_entity=AsyncMock(return_value=SimpleNamespace(id=9400000004)),
                                     get_messages=AsyncMock(return_value=[]), add_event_handler=lambda *_: None)
            snapshots = []
            real_sleep = asyncio.sleep
            async def tick(seconds):
                self.assertEqual(seconds, 60)
                snapshots.append(json.loads((Path(tmp) / 'observer_health.json').read_text()))
                if len(snapshots) == 2 and not finished.done():
                    finished.set_result(None)
                await real_sleep(0)
            async def run_until_disconnected():
                await finished
            client.run_until_disconnected = run_until_disconnected
            settings = {'TG_STATE_DIR': tmp, 'TG_API_ID': '1', 'TG_API_HASH': 'fixture', 'TG_SESSION_NAME': 'fixture',
                        'TG_RECIPE_TRUSTED_SENDER_IDS': '8735907987', 'TG_RECIPE_RECONCILE_SECONDS': '3600'}
            with (patch.dict(os.environ, settings, clear=True), patch.object(recipe_module, 'load_dotenv'),
                  patch('transport.telegram_client', return_value=client),
                  patch('transport.disconnect_telegram_client', new_callable=AsyncMock),
                  patch.object(recipe_module.asyncio, 'sleep', side_effect=tick)):
                await asyncio.wait_for(recipe_module._run_persistent_locked(), 2)
            self.assertEqual([call.kwargs['limit'] for call in client.get_messages.await_args_list], [500, 1])
            self.assertTrue(all(row['status'] == 'healthy' and row['listener_connected'] for row in snapshots))
            self.assertEqual(snapshots[-1]['source_targets'], ['@example_game_group'])


class HermesDeliveryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_hermes_delivery_records_the_message_id(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "message_id": "456"}), stderr="")
        with patch.object(recipe_module.subprocess, "run", return_value=result) as run:
            receipt = await deliver_with_hermes("实际奖励\n【图谱】", "telegram:9100000001", "/fixture/hermes")
        self.assertEqual(receipt, {"success": True, "message_id": 456, "target": "telegram:9100000001"})
        self.assertEqual(run.call_args.kwargs["input"], "实际奖励\n【图谱】")
        self.assertIn("--json", run.call_args.args[0])

    async def test_skipped_missing_or_failed_receipts_are_not_delivery_success(self):
        for payload in [{"success": True, "skipped": True}, {"success": True},
                        {"success": False, "message_id": 456}, {"success": True, "message_id": True},
                        {"success": True, "message_id": 456, "error": "unconfirmed"}, {}, ["unexpected"]]:
            with self.subTest(payload=payload):
                result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
                with patch.object(recipe_module.subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "hermes_send_unconfirmed"):
                        await deliver_with_hermes("奖励", "telegram:9100000001", "/fixture/hermes")


if __name__ == "__main__":
    unittest.main()
