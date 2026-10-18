import json
from pathlib import Path
import tempfile
import unittest

from automation_dossiers import summarize_file, refresh_automation_dossiers
from flow_registry import launchagent_labels
from miniapp_shared.accounts import ALL_ACCOUNTS
from miniapp_shared.state import write_json
from miniapp_shared.notify import Notifier
from scripts.interval_flow_dispatch import configured_jobs
from task_dossiers import DossierStore
from treasure_hunt.health import health
from treasure_hunt.runtime import fresh_state, summarize
from treasure_hunt.reports import report

NOW=1791612000.0
ACCOUNT=ALL_ACCOUNTS[0]


class HuntIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_all_six_accounts_are_owned_by_existing_dispatcher(self):
        root=Path(__file__).resolve().parents[1]
        registry=json.loads((root/'config/active_flows.json').read_text())
        jobs={k:v for k,v in configured_jobs(registry).items() if v['flow']=='treasure_hunt'}
        self.assertEqual({v['account'] for v in jobs.values()},set(ALL_ACCOUNTS))
        self.assertEqual(len(jobs),6)
        for entry in jobs.values():
            self.assertEqual(entry['miniapp_module'],'treasure_hunt')
            self.assertEqual(entry['interval_dispatch_seconds'],300)
            self.assertEqual(launchagent_labels([entry]),['ai.hermes.xiuxian.return-deadline-dispatch'])
            body=(root/'scripts'/f'send_treasure_hunt_{entry["account"]}.sh').read_text()
            self.assertIn('-m treasure_hunt.run_cycle --account '+entry['account'],body)
            self.assertNotIn('send_topic_message',body)

    def test_quota_cannot_mask_missing_new_payouts_in_health(self):
        s=fresh_state(ACCOUNT,NOW);s.update(status='daily_complete',quota={'used':3,'limit':3,'remaining':0},
            quota_confirmed_at=NOW,preexisting_used=0)
        write_json(self.root/'state.json',s)
        self.assertEqual(health(self.root,ACCOUNT,NOW)[0],'bad')
        s['preexisting_used']=3;write_json(self.root/'state.json',s)
        severity,status,note,_=health(self.root,ACCOUNT,NOW)
        self.assertEqual(severity,'ok');self.assertIn('0 confirmed payouts',note)

    def test_pending_settlement_is_unhealthy_when_no_worker_is_live(self):
        s=fresh_state(ACCOUNT,NOW);s['pending']={'operation':'settle','at':NOW-200}
        write_json(self.root/'state.json',s)
        self.assertEqual(health(self.root,ACCOUNT,NOW,running=False)[0],'bad')

    def test_health_process_matching_is_scoped_to_the_module_and_account(self):
        from scripts.xiuxian_healthcheck import flow_has_process
        line='123 python -B -m treasure_hunt.run_cycle --account '+ACCOUNT
        self.assertTrue(flow_has_process(line,ACCOUNT,'treasure_hunt','send'))
        self.assertFalse(flow_has_process(line,ALL_ACCOUNTS[1],'treasure_hunt','send'))
        self.assertFalse(flow_has_process(line,ACCOUNT,'shendubiguan','send'))

    def test_dossier_contains_gains_without_game_handles_or_auth(self):
        path=self.root/'state/accounts'/ACCOUNT/'treasure_hunt/last_result.json'
        write_json(path,{'account':ACCOUNT,'flow':'treasure_hunt','settled_runs':3,'contribution':100,
            'rewards':{'灵石':77,'token':111},'grades':['甲等'],'sessionId':'DO_NOT_COPY',
            'initData':'DO_NOT_COPY','raw_response':{'sensitive':'DO_NOT_COPY'}})
        value=summarize_file(path,self.root)
        self.assertEqual(value['hunt']['rewards'],{'灵石':77})
        self.assertNotIn('DO_NOT_COPY',json.dumps(value))
        self.assertNotIn('token',value['hunt']['rewards'])

    def test_all_new_interval_tasks_get_automatic_dossiers(self):
        entries=[{'account':a,'flow':'treasure_hunt','kinds':['send'],'enabled':True,
                 'interval_dispatch_seconds':300,'miniapp_module':'treasure_hunt'} for a in ALL_ACCOUNTS]
        write_json(self.root/'config/active_flows.json',{'flows':entries})
        write_json(self.root/'manifest.json',{'jobs':[]})
        write_json(self.root/'scheduler.json',{'updated_at':NOW,'jobs':{}})
        result=refresh_automation_dossiers(self.root,self.root/'state',now=NOW,
            manifest_path=self.root/'manifest.json',supervisor_path=self.root/'scheduler.json')
        self.assertEqual(result['task_count'],6)
        for a in ALL_ACCOUNTS:
            row=DossierStore(self.root/'state/task-dossiers').get('automation:ai.hermes.xiuxian.'+a+'.treasure_hunt.send')
            self.assertEqual(row['scope']['accounts'],[a])

    def test_repeated_scheduled_report_sends_one_confirmed_message(self):
        delivered=[]
        def sender(text):
            delivered.append(text)
            return {'success':True,'message_id':123,'target':'telegram:9100000001'}
        for a in ALL_ACCOUNTS:
            s=fresh_state(a,NOW);s.update(status='daily_complete',quota={'used':3,'limit':3,'remaining':0},preexisting_used=3)
            write_json(self.root/'state/accounts'/a/'treasure_hunt/last_result.json',summarize(s))
        notifier=Notifier(self.root,'treasure_hunt',sender=sender,now=lambda:NOW)
        first=report(self.root,now=NOW,notifier=notifier)
        second=report(self.root,now=NOW,notifier=notifier)
        self.assertEqual(first['notification']['status'],'delivered')
        self.assertEqual(second['notification']['status'],'already_delivered')
        self.assertEqual(len(delivered),1)


    def test_an_overlong_report_is_sent_truncated_and_kept_in_full(self):
        delivered=[]
        def sender(text):
            delivered.append(text)
            return {'success':True,'message_id':124,'target':'telegram:9100000001'}
        for a in ALL_ACCOUNTS:
            s=fresh_state(a,NOW);s.update(status='unconfirmed_start',error='x'*700)
            write_json(self.root/'state/accounts'/a/'treasure_hunt/last_result.json',summarize(s))
        result=report(self.root,now=NOW,notifier=Notifier(self.root,'treasure_hunt',sender=sender,now=lambda:NOW))
        self.assertEqual(result['notification']['status'],'delivered')
        self.assertLessEqual(len(delivered[0]),3400)
        self.assertIn('已截断',delivered[0])
        from miniapp_shared.state import day_key, read_json
        saved=read_json(self.root/'state/treasure_hunt/reports'/f'{day_key(NOW)}.json')
        self.assertTrue(saved['truncated']);self.assertGreater(len(saved['text']),3400)

if __name__=='__main__':unittest.main()
