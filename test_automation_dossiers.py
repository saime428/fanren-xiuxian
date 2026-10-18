"""Read-only QYZ journal projection and actual automation dossier integration."""
import json
from pathlib import Path
import tempfile
import unittest

from automation_dossiers import qyz_evidence,refresh_automation_dossiers
from task_dossiers import DossierStore


class QyzDossierTests(unittest.TestCase):
    def test_qyz_uses_latest_message_in_both_journal_shapes_and_separates_health(self):
        latest={'message_id':'100','status':'settled_partial','updated_at':'2026-10-06 13:42:00',
                'protocol_result':{'accepted_hits':16,'server_perfects':11,'finish_confirmed':True,
                  'server_damage_multiplier_min':1.0,'reference_outcome':{'damage_yi_total':1234,'grade':'甲等',
                  'diagnostics':{'token':'DO_NOT_COPY'}}},'token':'DO_NOT_COPY'}
        older={'message_id':9,'status':'settled_perfect'}
        health={'account':'tg9100000001','runtime_version':'qyz-reference-v32',
                'timing_policy_version':'qyz-vps-timing-v3','connected':True,'business_ready':False,
                'blockers':['latest_battle_not_successful'],'initData':'DO_NOT_COPY'}
        for events in ([latest,older],{'new':latest,'old':older}):
            row=qyz_evidence({'world_boss_health':health,'world_boss_events':events})
            self.assertEqual(row['latest_battle']['message_id'],100)
            self.assertEqual(row['latest_battle']['status'],'settled_partial')
            self.assertEqual(row['latest_battle']['result']['server_perfects'],11)
            self.assertEqual(row['latest_battle']['result']['damage_yi_total'],1234)
            self.assertEqual(row['current_health']['runtime_version'],'qyz-reference-v32')
            self.assertNotIn('runtime_version',row['latest_battle'])
            self.assertFalse(row['current_health']['business_ready'])
            self.assertEqual(row['current_health']['blockers'],['latest_battle_not_successful'])
            self.assertNotIn('DO_NOT_COPY',json.dumps(row))

    def test_qyz_incomplete_latest_record_cannot_inherit_old_success(self):
        row=qyz_evidence({'world_boss_events':[{'message_id':10,'status':'begin_submitted'},
            {'message_id':9,'protocol_result':{'finish_confirmed':True,'server_perfects':16}},
            {'message_id':True},None]})
        self.assertEqual(row['latest_battle']['message_id'],10)
        self.assertEqual(row['latest_battle']['result'],{})
        self.assertNotIn('latest_battle',qyz_evidence({'world_boss_events':None}))
        row=qyz_evidence({'world_boss_events':[{'message_id':10,'protocol_result':{
            'accepted_hits':True,'server_perfects':float('nan'),'finish_confirmed':{'token':'DONT_COPY'}}}]})
        self.assertEqual(row['latest_battle']['result'],{})

    def test_qyz_dossier_has_account_evidence_and_default_objective_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir();state=root/'state'
            label='ai.hermes.xiuxian.qingyuanzi-six.monitor'
            (root/'config/active_flows.json').write_text(json.dumps({'flows':[]}))
            manifest=root/'jobs.json';manifest.write_text(json.dumps({'jobs':[{'Label':label}]}))
            snapshot=root/'scheduler.json';snapshot.write_text(json.dumps({'updated_at':1000,'jobs':{}}))
            path=root/'qingyuanzi-new-source/state/accounts/tg9100000001/world_boss_state.json'
            path.parent.mkdir(parents=True);path.write_text(json.dumps({'world_boss_health':{
                'account':'tg9100000001','connected':True},'world_boss_events':[{'message_id':100,
                'protocol_result':{'accepted_hits':16,'finish_confirmed':True}}]}))
            before=path.read_bytes()
            refresh_automation_dossiers(root,state,now=1000,manifest_path=manifest,supervisor_path=snapshot)
            row=DossierStore(state/'task-dossiers').get('automation:'+label)
            self.assertIn('六账号青元子',row['objective'])
            self.assertEqual(row['business'][0]['latest_battle']['result']['accepted_hits'],16)
            self.assertEqual(path.read_bytes(),before)


class DossierWriteCostTests(unittest.TestCase):
    def test_one_collector_pass_writes_index_and_readme_once(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir();state=root/'state'
            (root/'config/active_flows.json').write_text(json.dumps({'flows':[]}))
            labels=[f'ai.hermes.xiuxian.helper-{n}' for n in range(5)]
            manifest=root/'jobs.json';manifest.write_text(json.dumps({'jobs':[{'Label':label} for label in labels]}))
            snapshot=root/'scheduler.json';snapshot.write_text(json.dumps({'updated_at':1000,'jobs':{}}))
            with patch.object(DossierStore,'_publish',autospec=True,side_effect=DossierStore._publish) as publish:
                result=refresh_automation_dossiers(root,state,now=1000,manifest_path=manifest,supervisor_path=snapshot)
            self.assertEqual(result['updated_dossiers'],5)
            self.assertEqual(publish.call_count,1)
            index=json.loads((state/'task-dossiers/index.json').read_text())
            self.assertEqual(len(index),5)
            self.assertEqual((state/'task-dossiers/README.md').read_text().count('\n- ['),5)

    def test_history_is_trimmed_to_the_newest_lines(self):
        import task_dossiers
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, \
                patch.multiple(task_dossiers,HISTORY_MAX_BYTES=400,HISTORY_KEEP_LINES=3):
            store=DossierStore(Path(tmp))
            for n in range(30):
                store.update('job',event={'id':str(n),'summary':'x'*20})
            history=next(Path(tmp,'history').glob('*.jsonl')).read_text().splitlines()
            self.assertLessEqual(len(history),12)
            self.assertEqual(json.loads(history[-1])['id'],'29')
