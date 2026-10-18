"""Re-observed boards retain original publication time and correct prior errors."""
import json
import unittest

import test_qyz_battle_report as fixtures
import qyz_battle_report as report


class ReportRepairTests(unittest.TestCase):
    setUp=fixtures.BattleReportTests.setUp
    tearDown=fixtures.BattleReportTests.tearDown

    def battle(self):
        for account in list(report.ACCOUNTS)[2:]:
            fixtures._journal(self.root,account,{'status':'settled_partial','expected_windows':16,
                'accepted_hits':16,'server_perfects':12,'protocol_result':{'finish_confirmed':True}})
        board={'chat_id':-1009400000004,'message_id':12406301,
               'published_at':'2026-09-19T13:47:16+08:00',
               'observed_at':'2026-09-19T14:15:56','participants':49,
               'rows':[{'rank':1,'username':'demo_player_03','score':3756,'attacks':16,'damage':'7.18亿亿'}]}
        fixtures._write(self.root/'state/world_boss_shared_board.json',board)
        return board

    def test_reobserved_board_with_original_time_retains_four_confirmed_accounts(self):
        self.battle();entry,records=report.collect(self.root/'state')
        key,text=report.build_report(entry,records,now_ts=self.entry_ts+3600)
        self.assertEqual(key,fixtures.FP[:16]+':deadline')
        self.assertIn('开战 4/6，正式结算 4/6，命中 64/96，PERFECT 48',text)
        self.assertIn('贡献榜前三',text)
        self.assertNotIn('未参战 0/6',text)

    def test_previously_wrong_missed_report_gets_one_correction_with_delivery_receipt(self):
        board=self.battle();path=self.root/'state/qyz_battle_report/notifications.json'
        fixtures._write(path,{'delivered':[
            {'key':fixtures.FP[:16]+':deadline','at':self.entry_ts+901},
            {'key':f"board:{board['message_id']}:missed",'at':self.entry_ts+2000}]})
        sent=[]
        def sender(text):
            sent.append(text);return {'success':True,'target':'telegram:1','message_id':123}
        first=report.run_once(self.root,now_ts=self.entry_ts+3600,sender=sender)
        self.assertEqual(first['status'],'delivered');self.assertEqual(first['receipt']['message_id'],123)
        self.assertIn('【青元子战报更正】',sent[0]);self.assertIn('命中 64/96',sent[0])
        self.assertEqual(report.run_once(self.root,now_ts=self.entry_ts+3660,sender=sender)['status'],'already_delivered')
        self.assertEqual(len(sent),1)
        self.assertEqual(len(json.loads(path.read_text())['delivered']),3)

    def test_failed_correction_remains_retryable(self):
        board=self.battle()
        fixtures._write(self.root/'state/qyz_battle_report/notifications.json',{'delivered':[
            {'key':f"board:{board['message_id']}:missed",'at':self.entry_ts+2000}]})
        def fail(text):raise RuntimeError('fixture_send_failed')
        self.assertEqual(report.run_once(self.root,now_ts=self.entry_ts+3600,sender=fail)['status'],'delivery_failed')
        self.assertEqual(report.run_once(self.root,now_ts=self.entry_ts+3660,
            sender=lambda _: {'success':True,'message_id':124})['status'],'delivered')

    def test_next_day_other_chat_and_older_message_are_not_matched(self):
        board=self.battle();entry,_=report.collect(self.root/'state')
        for altered in ({**board,'published_at':'2026-09-20T14:15:56'},
                        {**board,'chat_id':999},{**board,'message_id':1}):
            self.assertFalse(report.board_belongs_to_entry(altered,entry))

    def test_unknown_entry_does_not_claim_zero_participation(self):
        board=self.battle();_,text=report.build_missed_event_report(board,{})
        self.assertIn('参战数量待核对',text)
        self.assertNotIn('本方未参战 0/',text)
