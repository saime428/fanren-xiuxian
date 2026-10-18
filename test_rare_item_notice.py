import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from rare_item_notice import (ACCOUNT_USERNAMES, ITEMS, ItemReward, RewardOutbox,
                              parse_gains, telegram_rewards, spool_rewards, structured_gains)

BOT = 8788895566


def message(text, ident=101, sender=BOT, parent=100):
    return SimpleNamespace(id=ident, sender_id=sender, chat_id=-1009400000004, raw_text=text,
                           fwd_from=None, reply_to=SimpleNamespace(reply_to_top_id=7310786, reply_to_msg_id=parent))


class RareItemParsingTests(unittest.TestCase):
    def test_all_six_accounts_and_all_four_items(self):
        for account, username in ACCOUNT_USERNAMES.items():
            for item in ITEMS:
                with self.subTest(account=account, item=item):
                    self.assertEqual(parse_gains(f'恭喜 @{username} 获得了【{item}】×2！'),
                                     (ItemReward(account,item,2),))

    def test_traditional_names_and_ascii_parentheses(self):
        self.assertEqual(parse_gains('@demo_player_02 獲得【青元劍訣(中)】x1'),
                         (ItemReward('tg9100000002','青元剑诀（中）',1),))

    def test_inventory_costs_probabilities_similar_items_and_foreign_players_are_not_gains(self):
        for text in ['背包：【庚金】×2', '消耗了【庚金】×2', '可能获得【庚金】', '有概率获得【庚金】',
                     '可以获得【庚金】', '尚未获得【庚金】', '未能获得【庚金】', '获得【庚金】的概率为1%',
                     '材料不足，需要【庚金】', '获得【太乙庚金】', '获得【庚金精华】', '获得【庚金】×0']:
            with self.subTest(text=text):
                self.assertEqual(parse_gains('@demo_player_01 '+text), ())
        self.assertEqual(parse_gains('@sample_peer_12else 获得【庚金】',context_actors=('demo_player_01',)), ())
        self.assertEqual(parse_gains('@demo_player_01_other 获得【庚金】'), ())

    def test_mixed_recipients_are_attributed_per_reward_clause(self):
        self.assertEqual(parse_gains('@demo_player_02 获得【庚金】×2\n@sample_peer_12else 获得【青元剑诀（上）】'),
                         (ItemReward('tg9100000002','庚金',2),))
        self.assertEqual(parse_gains('@sample_peer_12else 获得【庚金】\n@demo_player_01 获得【灵石】'), ())

    def test_joint_bonus_and_multiple_items(self):
        text='@demo_player_01 与 @demo_player_03\n✨ 天赐机缘！\n双方共同领悟了【青元剑诀（上）】、【青元剑诀（下）】！'
        gains=parse_gains(text)
        self.assertEqual({r.account for r in gains},{'tg9100000001','tg9100000003'})
        self.assertEqual({r.item for r in gains},{'青元剑诀（上）','青元剑诀（下）'})

    def test_structured_reward_scope_does_not_scan_inventory_or_request_credentials(self):
        payload={'rewards':[{'name':'庚金','qty':3}], 'account':{'inventory':[{'name':'青元剑诀（中）','qty':1}]},
                 'token':'never-store-me'}
        self.assertEqual(structured_gains(payload,'tg9100000002'),(ItemReward('tg9100000002','庚金',3),))


class RareItemAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gap_cursor_is_not_advanced_by_a_newer_overlap_read(self):
        from recipe_notice_monitor import read_reward_history
        async def iterate(*args,**kwargs):
            for ident in [101,102]:yield message('text',ident=ident)
        client=SimpleNamespace(iter_messages=iterate,get_messages=AsyncMock(return_value=[message('edited',ident=200),message('old',ident=90)]))
        rows,cursor,backlog=await read_reward_history(client,'chat',cursor=100,limit=5)
        self.assertEqual(cursor,102);self.assertFalse(backlog)
        self.assertEqual([m.id for m in rows],[90,101,102,200])
        rows,cursor,backlog=await read_reward_history(client,'chat',cursor=100,limit=2)
        self.assertTrue(backlog);self.assertEqual(cursor,102)
        self.assertEqual([m.id for m in rows],[101,102])

    async def test_activation_message_requires_a_real_receipt_and_is_sent_once(self):
        from recipe_notice_monitor import report_rare_activation
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json'
            bad=AsyncMock(return_value=None)
            self.assertFalse(await report_rare_activation(path,bad,now=100))
            good=AsyncMock(return_value={'success':True,'target':'telegram:9100000001','message_id':7})
            self.assertFalse(await report_rare_activation(path,good,now=399))
            self.assertTrue(await report_rare_activation(path,good,now=400))
            self.assertFalse(await report_rare_activation(path,good,now=1000))
            self.assertEqual(good.await_count,1)
            self.assertIn('不代表实际获得',good.await_args.args[0])

    async def test_unmentioned_direct_reply_resolves_each_actual_account(self):
        for account in ACCOUNT_USERNAMES:
            parent=message('.问道',ident=100,sender=int(account[2:]),parent=7310786)
            lookup=AsyncMock(return_value=parent)
            result=await telegram_rewards(message('你获得了【庚金】×1'),trusted_sender_ids={BOT},lookup_parent=lookup)
            self.assertEqual(result,(ItemReward(account,'庚金',1),))

    async def test_wrong_source_forward_and_unrelated_parent_are_rejected(self):
        for change in [{'sender_id':1},{'chat_id':-1},{'fwd_from':object()}]:
            m=message('@demo_player_01 获得【庚金】')
            for key,value in change.items():setattr(m,key,value)
            self.assertEqual(await telegram_rewards(m,trusted_sender_ids={BOT}),())
        wrong=message('.问道',ident=99,sender=9100000001,parent=7310786)
        self.assertEqual(await telegram_rewards(message('你获得【庚金】'),trusted_sender_ids={BOT},
                                               lookup_parent=AsyncMock(return_value=wrong)),())


class RewardOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_message_drain_reuses_one_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('rare_item_notice.sqlite3.connect', wraps=sqlite3.connect) as opened:
            box=RewardOutbox(Path(tmp)/'outbox.sqlite3')
            box.enqueue([ItemReward('tg9100000001','庚金')],source='telegram',source_id='chat:7',now=0)
            send=AsyncMock(return_value={'success':True,'target':'telegram:9100000001','message_id':5})
            for _ in range(3):
                await box.drain(send,now=1)
            self.assertEqual(box.summary(),{'delivered':1})
        self.assertEqual(opened.call_count,1)

    async def test_existing_bonus_receipt_prevents_duplicate_rare_notice_for_covered_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            box=RewardOutbox(Path(tmp)/'outbox.sqlite3')
            box.enqueue([ItemReward('tg9100000001','庚金'),ItemReward('tg9100000002','庚金')],source='telegram',source_id='chat:101',now=0)
            box.confirm_existing_notice(source='telegram',source_id='chat:101',accounts=['tg9100000001'],
                                        receipt={'success':True,'target':'telegram:9100000001','message_id':9})
            send=AsyncMock(return_value={'success':True,'target':'telegram:9100000001','message_id':10})
            self.assertEqual(await box.drain(send,now=1),1)
            self.assertIn('@demo_player_02',send.await_args.args[0]);self.assertEqual(box.summary(),{'delivered':2})
    async def test_edit_replay_restart_and_spool_do_not_duplicate_confirmed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'outbox.sqlite3';box=RewardOutbox(path)
            reward=ItemReward('tg9100000002','庚金',2)
            spool_rewards(root,[reward],source='telegram',source_id='chat:101',link='https://t.me/example/101')
            self.assertEqual(box.import_spool(root),1)
            self.assertEqual(list((root/'state/rare_item_notice/inbox').glob('*.json')),[])
            deliver=AsyncMock(return_value={'success':True,'target':'telegram:9100000001','message_id':88})
            self.assertEqual(await box.drain(deliver,now=1),1)
            restarted=RewardOutbox(path)
            spool_rewards(root,[reward],source='telegram',source_id='chat:101',link='https://t.me/example/101')
            self.assertEqual(restarted.import_spool(root),0)
            self.assertEqual(await restarted.drain(deliver,now=1000),0)
            self.assertEqual(deliver.await_count,1)
            self.assertIn('@demo_player_02',deliver.await_args.args[0])
            self.assertEqual(restarted.summary(),{'delivered':1})

    async def test_failed_or_unconfirmed_delivery_remains_pending_and_retries_after_restart(self):
        for receipt in [None,{}, {'success':True,'message_id':1}, {'success':True,'target':'telegram:9100000001','message_id':True}]:
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'outbox.sqlite3';box=RewardOutbox(path)
                box.enqueue([ItemReward('tg9100000006','青元剑诀（上）')],source='telegram',source_id='101',now=0)
                self.assertEqual(await box.drain(AsyncMock(return_value=receipt),now=100),0)
                self.assertEqual(box.summary(),{'pending':1})
                send=AsyncMock(return_value={'success':True,'target':'telegram:9100000001','message_id':1})
                restarted=RewardOutbox(path)
                self.assertEqual(await restarted.drain(send,now=129),0)
                self.assertEqual(await restarted.drain(send,now=130),1)

    async def test_shared_miniapp_transport_only_queues_bound_confirmed_rewards(self):
        from miniapp_shared.dwelling import JsonTransport
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            reply={'ok':True,'actionResult':{'ok':True,'receiptId':'event-1','rewards':[{'name':'庚金','qty':1}]}}
            transport=JsonTransport({'/reward':True},post=lambda *a,**kw:reply)
            transport.bind_reward_account('tg9100000005','sensitive-init-data',root)
            await transport('/reward',{'initData':'sensitive-init-data','token':'secret'},mutation=True)
            await transport('/reward',{'initData':'sensitive-init-data','token':'secret'},mutation=True)
            files=list((root/'state/rare_item_notice/inbox').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertNotIn('sensitive',files[0].read_text());self.assertNotIn('secret',files[0].read_text())
            reply['actionResult'].update(ok=False,receiptId='event-2')
            await transport('/reward',{'initData':'sensitive-init-data'},mutation=True)
            self.assertEqual(len(list(files[0].parent.glob('*.json'))),1)
