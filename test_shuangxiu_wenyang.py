import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import shuangxiu_wenyang as wenyang

from shuangxiu_wenyang import (
    PARTNER_ACCOUNT,
    classify_partner_reply,
    compute_next_due_ts,
    select_direct_business_reply,
    select_anchor_round,
)


class ShuangxiuWenyangTests(unittest.TestCase):
    def test_partner_actor_is_exact_demo_player_03_account(self):
        self.assertEqual("tg9100000003", PARTNER_ACCOUNT)

    def test_classifies_success_and_requires_both_actors(self):
        text = (
            "【温养双修·大成】\n"
            "在同参契印的加持下，你与 @demo_player_01 灵力完美交融，事半功倍！\n"
            "@demo_player_03 修为增加了 68 点，并获得 15 点宗门贡献！\n"
            "@demo_player_01 修为增加了 57 点！"
        )
        self.assertEqual("success", classify_partner_reply(text))
        self.assertEqual("unknown", classify_partner_reply(text.replace("@demo_player_01", "@other")))
        self.assertEqual("unknown", classify_partner_reply(text.replace("@demo_player_01", "@demo_player_01_other")))

    def test_classifies_cooldown(self):
        text = "道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中）。"
        self.assertEqual("cooldown", classify_partner_reply(text))

    def test_classifies_pending_settlement(self):
        self.assertEqual("pending", classify_partner_reply("契印感应，温养双修结算中"))

    def test_selects_only_direct_reply_to_partner_command(self):
        replies = [
            {"id": 11, "sender_id": 8917921351, "reply_to_msg_id": 900, "text": "【温养双修·大成】 @demo_player_03 @demo_player_01"},
            {"id": 12, "reply_to_msg_id": 901, "text": "unrelated"},
        ]
        selected, status = select_direct_business_reply(replies, command_message_id=900)
        self.assertEqual(11, selected["id"])
        self.assertEqual("success", status)

    def test_wrong_reply_target_fails_closed(self):
        selected, status = select_direct_business_reply(
            [{"id": 11, "reply_to_msg_id": 777, "text": "【温养双修·大成】 @demo_player_03 @demo_player_01"}],
            command_message_id=900,
        )
        self.assertIsNone(selected)
        self.assertEqual("unknown", status)

    def test_next_due_uses_server_success_time_plus_one_hour(self):
        self.assertEqual(4600.0, compute_next_due_ts(1000.0, 3600))

    def test_explicit_server_countdown_replaces_the_hourly_fallback(self):
        for text, seconds in [('剩余冷却时间：5分钟。', 300), ('请在 2小时 后再试。', 7200), ('剩余时间：0秒。', 0)]:
            reply = {'ts': 1000, 'text': '道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中）。' + text}
            self.assertEqual(wenyang.reply_next_due_ts(reply, 'cooldown'), 1000 + seconds + 3)
        self.assertEqual(wenyang.reply_next_due_ts({'ts': 1000, 'text': '冷却中'}, 'cooldown'), 4600)

    def test_newer_countdown_supersedes_old_success_guard_and_replay_does_not_extend_it(self):
        success = {'ts': 900, 'next_due_ts': 4500}
        cooldown = {'status': 'cooldown', 'ts': 1000, 'next_due_ts': 4600,
                    'reply_sender_id': 8788895566,
                    'reply_text': '道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中），剩余时间：5分钟。'}
        self.assertEqual(wenyang.next_run_due_ts(success, cooldown), 1303)
        self.assertEqual(wenyang.next_run_due_ts(success, cooldown), 1303)
        self.assertEqual(wenyang.next_run_due_ts({'ts': 1100, 'next_due_ts': 4700}, cooldown), 4700)

    def test_recovered_cooldown_saves_the_same_deadline_without_claiming_success(self):
        rows = round_rows()[:2]
        reply = {'id': 20, 'ts': 1000, 'sender_id': 8788895566, 'reply_to_msg_id': 11,
                 'text': '道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中），剩余时间：5分钟。'}
        result = {'anchor': rows[0], 'command': rows[1], 'reply': reply,
                  'status': 'cooldown', 'command_message_ids': [11]}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            saved = wenyang.persist_round_result(directory, {}, result)
            self.assertEqual(saved['next_due_ts'], 1303)
            self.assertFalse((directory / 'last_success.json').exists())

    def test_an_untrusted_direct_reply_cannot_confirm_the_round(self):
        selected, status = select_direct_business_reply(
            [{"id": 11, "sender_id": 999, "reply_to_msg_id": 900,
              "text": "【温养双修·大成】 @demo_player_03 @demo_player_01"}], command_message_id=900)
        self.assertIsNone(selected)
        self.assertEqual(status, "unknown")


def round_rows():
    now = time.time() - 60
    def row(ident, sender, text, parent, seconds):
        return {"id": ident, "sender_id": sender, "text": text, "reply_to_msg_id": parent,
                "reply_to_top_id": 7310786, "chat_id": -1009400000004,
                "date_ts": now + seconds, "ts": now + seconds}
    return [row(10, 9100000001, "1", 7310786, 0),
            row(11, 9100000003, ".双修 温养", 10, 1),
            row(12, 8816935632, "【温养双修·大成】 @demo_player_03 @demo_player_01", 11, 15),
            row(13, 9100000003, ".双修 温养", 10, 16),
            row(14, 8788895566, "道友 @demo_player_03 心神尚未恢复，无法进行双修（冷却中）。", 13, 17)]


class WenyangRoundRecoveryTests(unittest.TestCase):
    def test_first_success_wins_over_later_cooldown_for_same_verified_anchor(self):
        result = select_anchor_round(round_rows(), 10)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["command"]["id"], 11)
        self.assertEqual(result["command_message_ids"], [11, 13])

    def test_wrong_anchor_owner_partner_and_source_are_not_reconciled(self):
        for index, changes in [(0, {"sender_id": 999}), (0, {"chat_id": -1009400000005}),
                               (0, {"reply_to_msg_id": 9})]:
            rows = round_rows()
            rows[index].update(changes)
            self.assertIsNone(select_anchor_round(rows, 10))
        rows = round_rows()[:3]
        rows[1]["sender_id"] = 9100000005
        self.assertIsNone(select_anchor_round(rows, 10))


class WenyangBoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_copies_keep_bounded_reads_and_recover_an_old_parent_from_recent_reply(self):
        def message(mid, actor, text, parent, ts):
            return SimpleNamespace(id=mid, sender_id=actor, message=text, chat_id=-1009400000004,
                fwd_from=None, date=datetime.fromtimestamp(ts, timezone.utc), edit_date=None,
                reply_to=SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=7310786))
        anchor = message(10, 9100000001, '1', 7310786, 1000)
        command = message(150, 9100000003, '.双修 温养', 10, 1100)
        reply = message(1100, 8816935632, '【温养双修·大成】 @demo_player_03 @demo_player_01', 150, 2000)
        pool = {10: anchor, 150: command}
        recent = []
        async def get_messages(_target, *, ids=None, min_id=None, **_kwargs):
            if isinstance(ids, int):
                return pool.get(ids)
            if isinstance(ids, list):
                return [pool[mid] for mid in ids if mid in pool]
            return [] if min_id is not None else recent
        client = SimpleNamespace(get_messages=AsyncMock(side_effect=get_messages))
        base = {'anchor_message_id': 10, 'command_message_ids': list(range(100, 200))}
        with patch.object(wenyang, 'ensure_connected', new_callable=AsyncMock):
            self.assertIsNone(await wenyang.reconcile_anchor_round(client, '@example_game_group', base, 200))
            self.assertLessEqual(client.get_messages.await_count, 5)
            base = json.loads(json.dumps(base))
            recent = [reply]
            client.get_messages.reset_mock()
            result = await wenyang.reconcile_anchor_round(client, '@example_game_group', base, 200)
            self.assertEqual(result['command']['id'], 150)
            self.assertEqual(result['status'], 'success')
            self.assertLessEqual(client.get_messages.await_count, 6)
            self.assertEqual(len(base['command_message_ids']), 100)


class WenyangReplyPollTests(unittest.IsolatedAsyncioTestCase):
    def poll_message(self, message_id, text="other topic", *, direct=False):
        return SimpleNamespace(
            id=message_id, sender_id=8917921351, fwd_from=None,
            chat_id=wenyang.CANONICAL_CHAT_ID, message=text,
            date=datetime.fromtimestamp(message_id, timezone.utc), edit_date=None,
            reply_to=SimpleNamespace(reply_to_msg_id=900 if direct else 1,
                                     reply_to_top_id=wenyang.CANONICAL_TOPIC_ID if direct else 1))

    async def poll_history(self, messages, *, after_sleep=None):
        clock = [0]
        async def iter_messages(_chat, *, min_id, reverse, limit):
            following = sorted((m for m in messages if m.id > min_id), key=lambda m: m.id)
            for message in following[:limit]:
                yield message
        async def get_messages(_chat, *, ids):
            return [message for message in messages if message.id in ids]
        async def sleep(_seconds):
            clock[0] += 1
            if after_sleep:
                after_sleep()
        client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock(side_effect=get_messages))
        with patch.object(wenyang, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(wenyang.asyncio, 'sleep', side_effect=sleep):
            return await wenyang._wait_for_business_reply(
                client, chat_id=wenyang.CANONICAL_CHAT_ID, command_message_id=900,
                timeout_seconds=5, poll_seconds=1, scan_limit=120)

    async def test_receipt_after_a_full_page_of_other_topics_is_confirmed(self):
        messages = [self.poll_message(mid) for mid in range(901, 1022)]
        messages.append(self.poll_message(
            1022, "【温养双修·大成】 @demo_player_03 @demo_player_01", direct=True))
        selected, status = await self.poll_history(messages)
        self.assertEqual(status, 'success')
        self.assertEqual(selected['id'], 1022)

    async def test_pending_receipt_edited_after_the_cursor_passes_it_is_confirmed(self):
        pending = self.poll_message(905, "契印感应，温养双修结算中", direct=True)
        messages = [self.poll_message(mid) for mid in range(901, 1201) if mid != pending.id]
        messages.append(pending)
        def settle():
            pending.message = "【温养双修·大成】 @demo_player_03 @demo_player_01"
            pending.edit_date = datetime.fromtimestamp(2000, timezone.utc)
        selected, status = await self.poll_history(messages, after_sleep=settle)
        self.assertEqual(status, 'success')
        self.assertEqual(selected['id'], pending.id)

    async def test_reply_poll_reads_only_messages_after_the_command(self):
        reply = SimpleNamespace(id=905, sender_id=8917921351, fwd_from=None, chat_id=wenyang.CANONICAL_CHAT_ID,
                                message="【温养双修·大成】 @demo_player_03 @demo_player_01",
                                date=datetime.now(timezone.utc), edit_date=None,
                                reply_to=SimpleNamespace(reply_to_msg_id=900, reply_to_top_id=wenyang.CANONICAL_TOPIC_ID),
                                reply_to_msg_id=900)
        reads = []
        async def iter_messages(chat, **kwargs):
            reads.append(kwargs)
            yield reply
        client = SimpleNamespace(iter_messages=iter_messages, get_messages=AsyncMock(side_effect=AssertionError))
        selected, status = await wenyang._wait_for_business_reply(
            client, chat_id=wenyang.CANONICAL_CHAT_ID, command_message_id=900,
            timeout_seconds=5, poll_seconds=1, scan_limit=120)
        self.assertEqual((selected["id"], status), (905, "success"))
        self.assertEqual(reads, [{"min_id": 900, "reverse": True, "limit": 120}])


class WenyangMutationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.initiator = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=10)))
        self.partner = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=11)))
        async def connect(_session, _api, _hash, ident, _username):
            return (self.initiator if ident == 9100000001 else self.partner), SimpleNamespace(id=ident)
        async def gate(action, **_kwargs):
            return await action()
        self.patches = [
            patch.dict(os.environ, {"TG_STATE_DIR": str(self.root), "TG_TARGET": "@example_game_group",
                       "TG_TOPIC_ID": "7310786", "INITIATOR_SESSION_NAME": "fixture-a",
                       "PARTNER_SESSION_NAME": "fixture-b", "INITIATOR_API_ID": "1", "PARTNER_API_ID": "1",
                       "INITIATOR_API_HASH": "fixture", "PARTNER_API_HASH": "fixture"}, clear=True),
            patch.object(wenyang, "load_dotenv"), patch.object(wenyang, "_connect_identity", side_effect=connect),
            patch.object(wenyang, "send_with_global_gate", side_effect=gate),
            patch.object(wenyang, "disconnect_telegram_client", new_callable=AsyncMock),
        ]
        for item in self.patches: item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    async def test_rpc_error_never_resends_and_next_run_recovers_server_settlement(self):
        self.partner.send_message.side_effect = RuntimeError("fixture InterdcCallError")
        await wenyang.run_once()
        state = json.loads((self.root / "last_attempt.json").read_text())
        self.assertEqual(state["status"], "command_send_unknown")
        self.assertEqual(state["anchor_message_id"], 10)
        self.partner.send_message.assert_awaited_once()
        with patch.object(wenyang, "reconcile_anchor_round", new_callable=AsyncMock,
                          return_value=select_anchor_round(round_rows(), 10)):
            await wenyang.run_once()
        self.partner.send_message.assert_awaited_once()
        self.initiator.send_message.assert_awaited_once()
        success = json.loads((self.root / "last_success.json").read_text())
        self.assertEqual(success["command_message_id"], 11)
        self.assertEqual(success["reply_message_id"], 12)

    async def test_unknown_anchor_blocks_further_mutations_across_runs(self):
        self.initiator.send_message.side_effect = TimeoutError()
        await wenyang.run_once()
        with patch.object(wenyang, "reconcile_anchor_round", new_callable=AsyncMock, return_value=None):
            await wenyang.run_once()
        self.initiator.send_message.assert_awaited_once()
        self.partner.send_message.assert_not_awaited()
        self.assertEqual(json.loads((self.root / "last_attempt.json").read_text())["status"], "anchor_send_unknown")

    async def test_unconfirmed_receipt_reconciles_during_hold_without_new_anchor(self):
        with patch.object(wenyang, "_wait_for_business_reply", new_callable=AsyncMock, return_value=(None, "unknown")):
            await wenyang.run_once()
        state = json.loads((self.root / "last_attempt.json").read_text())
        self.assertIsNone(state["next_due_ts"])
        self.assertGreater(state["retry_not_before_ts"], time.time())
        with patch.object(wenyang, "reconcile_anchor_round", new_callable=AsyncMock, return_value=None) as recover:
            await wenyang.run_once()
        recover.assert_awaited_once()
        self.initiator.send_message.assert_awaited_once()
        self.partner.send_message.assert_awaited_once()

    async def test_noncanonical_outbound_configuration_never_connects_or_sends(self):
        with patch.dict(os.environ, {"TG_TARGET": "-1009400000005"}):
            with self.assertRaises(ValueError):
                await wenyang.run_once()
        self.initiator.send_message.assert_not_awaited()
        self.partner.send_message.assert_not_awaited()

    async def test_unconfirmed_retries_same_anchor_across_restarts_until_a_receipt(self):
        with patch.object(wenyang, "_wait_for_business_reply", new_callable=AsyncMock, return_value=(None, "unknown")), \
                patch.object(wenyang, "reconcile_anchor_round", new_callable=AsyncMock, return_value=None), \
                patch.object(wenyang.time, "time", return_value=10000) as clock:
            self.partner.send_message.side_effect = [SimpleNamespace(id=11), SimpleNamespace(id=13), SimpleNamespace(id=15)]
            for ts in (10000, 10181, 10362):
                clock.return_value = ts
                await wenyang.run_once()
                self.assertFalse((self.root / "last_success.json").exists())
            self.assertEqual(self.partner.send_message.await_count, 3)
            self.initiator.send_message.assert_awaited_once()
            for call in self.partner.send_message.await_args_list:
                self.assertEqual(call.kwargs["reply_to"], 10)
        with patch.object(wenyang, "reconcile_anchor_round", new_callable=AsyncMock,
                          return_value=select_anchor_round(round_rows(), 10)):
            await wenyang.run_once()
        self.assertEqual(self.partner.send_message.await_count, 3)
        self.assertEqual(json.loads((self.root / "last_success.json").read_text())["reply_message_id"], 12)


if __name__ == "__main__":
    unittest.main()
