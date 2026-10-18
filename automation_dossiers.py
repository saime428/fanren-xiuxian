"""Generate a dossier for every actual scheduler/interval task from read-only evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

from flow_registry import active_entries, launchagent_label, registry_path
from task_dossiers import DossierStore, read_json, write_json

SUPERVISOR_STATE=Path('/Users/operator/.hermes/workspace/state/docker-supervisor.json')
MANIFEST=Path('/opt/runner/jobs.json')
FIELDS=('status','actor_id','flow_slug','acked','succeeded','receipt_confirmed','pending','last_date','day',
        'sent_message_id','ack_reply_message_id','reply_message_id','ack_reply_sender_id','reply_sender_id',
        'next_due_ts','next_due_at','retry_not_before_ts','daily_count','daily_cast_count','identity_count',
        'last_success_ts','last_reconcile_ts','runtime_version','error_type',
        'transport','player_id','flow','api_origin','endpoint','operation','server_ok','confirmed',
        'received_at','response_digest','last_success_at','command_transport','command_transport_version')
MINIAPP={'tower-miniapp.daily':'tower_miniapp','journey-miniapp.daily':'journey_miniapp','fate-cards.daily':'fate_cards',
         'star-observatory.send':'star_observatory','pavilion-loop.send':'pavilion_loop','sect-daily':'sect_daily'}


def qyz_evidence(value):
    """Project the live journal, keeping current readiness and old results apart."""
    def project(source, *, numbers=(), flags=(), codes=(), versions=()):
        source=source if isinstance(source,dict) else {}
        result={k:source[k] for k in numbers if type(source.get(k)) in (int,float)
                and math.isfinite(source[k])}
        result.update({k:source[k] for k in flags if type(source.get(k)) is bool})
        result.update({k:source[k] for k in codes if isinstance(source.get(k),str)
                       and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',source[k])})
        result.update({k:source[k] for k in versions if isinstance(source.get(k),str)
                       and re.fullmatch(r'qyz-[a-z-]+-v\d+',source[k])})
        return result

    health=value.get('world_boss_health')
    health=health if isinstance(health,dict) else {}
    current=project(health,numbers=('timestamp','pid'),codes=('status',),
                    flags=('connected','identity_verified','membership_verified','source_verified',
                           'verification_available','monitor_active','business_ready'),
                    versions=('runtime_version','timing_policy_version'))
    if isinstance(health.get('blockers'),list):
        current['blockers']=[b for b in health['blockers'] if isinstance(b,str)
                             and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',b)]
    if isinstance(health.get('account'),str) and re.fullmatch(r'tg\d+',health['account']):
        current['account']=health['account']
    events=value.get('world_boss_events')
    rows=list(events.values()) if isinstance(events,dict) else events if isinstance(events,list) else []
    def message_id(row):
        raw=row.get('message_id') if isinstance(row,dict) else None
        return int(raw) if type(raw) is int and raw>0 or isinstance(raw,str) and re.fullmatch(r'[1-9]\d{0,19}',raw) else 0
    valid=[row for row in rows if message_id(row)>0]
    result={'current_health':current,'recorded_event_count':len(rows)}
    if not valid:return result
    latest=max(valid,key=message_id)
    battle=project(latest,codes=('status',),versions=('runtime_version',))
    battle['message_id']=message_id(latest)
    if isinstance(latest.get('updated_at'),str) and re.fullmatch(r'[0-9TZ:.+ -]{10,40}',latest['updated_at']):
        battle['updated_at']=latest['updated_at']
    protocol=latest.get('protocol_result')
    protocol=protocol if isinstance(protocol,dict) else {}
    battle['result']=project(protocol,numbers=('expected_windows','accepted_hits','server_perfects',
        'local_missed_windows','server_damage_multiplier_min'),flags=('finish_confirmed','local_dead'),codes=('status',))
    outcome=protocol.get('reference_outcome')
    outcome=outcome if isinstance(outcome,dict) else {}
    battle['result'].update(project(outcome,numbers=('damage_yi_total','damage_yi_average','score')))
    if outcome.get('grade') in ('甲等','乙等','丙等','丁等'):
        battle['result']['grade']=outcome['grade']
    result['latest_battle']=battle
    return result


def fishing_performance(value):
    raw=value.get('catches');catches=[row for row in raw if isinstance(row,dict)] if isinstance(raw,list) else []
    caught=sum(row.get('caught') is True for row in catches)
    empty=[row for row in catches if row.get('caught') is False]
    timeout=sum(row.get('reason')=='timeout' for row in empty)
    miss=sum(row.get('reason')=='miss' for row in empty)
    scores=[row['control_score'] for row in catches if type(row.get('control_score')) in (int,float)
            and math.isfinite(row['control_score']) and 0<=row['control_score']<=100]
    known=caught+len(empty)
    day=value.get('settlement_date')
    day=day if isinstance(day,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) else None
    result={'settlement_date':day,'recorded_casts':len(catches),
            'known_outcomes':known,'caught':caught,'empty':len(empty),'timeout':timeout,'miss':miss,
            'unclassified_empty':len(empty)-timeout-miss,'unknown_outcomes':len(catches)-known,
            'score_samples':len(scores)}
    if known:result['empty_percent_of_known']=round(len(empty)*100/known,2)
    if scores:result['average_control_score']=round(sum(scores)/len(scores),2)
    conditions={}
    for row in catches:
        pond=row.get('pond');weather=row.get('weather')
        pond=pond if isinstance(pond,str) and pond in {'qingxi','hantan','luanxing'} else 'unrecorded'
        weather=weather if isinstance(weather,str) and weather in {'sunny','drizzle','rain','thunder','moon','wind','snow'} else 'unrecorded'
        key=f'{pond}/{weather}';count=conditions.setdefault(key,{'casts':0,'caught':0,'empty':0})
        count['casts']+=1;count['caught']+=row.get('caught') is True;count['empty']+=row.get('caught') is False
    result['conditions']=conditions
    return result


def evidence_paths(root,label,entry):
    if label.endswith('qingyuanzi-six.monitor') or label.endswith('qyz-battle-report'):
        return list((root/'qingyuanzi-new-source/state/accounts').glob('tg*/world_boss_state.json'))
    if (not entry or entry.get('account')=='helpers'):
        match=re.search(r'\.((?:tg)\d+)\.([A-Za-z0-9_-]+)\.',label)
        if match:entry={'account':match.group(1),'flow':match.group(2)}
    if entry and str(entry.get('account','')).startswith('tg'):
        directory=root/'state/accounts'/entry['account']/entry['flow']
        if entry.get('flow') == 'tianxing_exploration':
            return [directory/'state.json', directory/'summary.json']
        return [directory/name for name in ('last_result.json','last_attempt.json','observer_health.json','monitor_health.json',
                                            'notification_state.json','miniapp_state.json','miniapp_last_result.json')]
    for suffix,flow in MINIAPP.items():
        if label.endswith(suffix):return (list((root/'state/accounts').glob(f'tg*/{flow}/summary.json'))
            +list((root/'state/accounts').glob(f'tg*/{flow}/state.json'))
            +list((root/'state/accounts').glob(f'tg*/{flow}/miniapp_last_result.json')))
    if label.endswith('miniapp-fishing.send'):
        return list((root/'fishing/state/miniapp_fishing/accounts').glob('tg*/fishing.json'))
    if '.yuanshen-' in label or label.endswith('yuanshen.return_observer'):
        return list((root/'state/yuanshen').glob('tg*/state.json'))
    return []


def summarize_file(path,root):
    if not path.exists() or path.is_symlink() or path.stat().st_size>2*1024*1024:return None
    row=read_json(path);result={k:row[k] for k in FIELDS if k in row}
    if path.name=='world_boss_state.json':
        result=qyz_evidence(row)
    if isinstance(row.get('account'),str) and re.fullmatch(r'tg\d+',row['account']):result['account']=row['account']
    if row.get('flow') == 'tianxing_exploration':
        result['pending'] = bool(row.get('pending'))
        journey = row.get('journey') or {}
        board = row.get('board') or {}
        result['exploration'] = {
            'journey': {k:journey[k] for k in ('count','limit') if type(journey.get(k)) is int},
            'fate': {k:board[k] for k in ('points','debt','hits','rescues') if type(board.get(k)) is int},
            'confirmed_actions': sum(e.get('confirmed') is True for e in row.get('events',[]) if isinstance(e,dict)),
            'reports_delivered': sum(e.get('status') == 'sent' for e in row.get('outbox',[]) if isinstance(e,dict)),
        }
        if board.get('star') in ('紫微','天府','太阴','贪狼'):
            result['exploration']['star'] = board['star']
        if isinstance(row.get('error'),str) and re.fullmatch(r'[A-Za-z0-9_]{1,100}',row['error']):
            result['exploration']['error'] = row['error']
    if row.get('flow') == 'treasure_hunt':
        result['hunt'] = {k:row[k] for k in ('used','limit','remaining','settled_runs','preexisting_used','unconfirmed_runs',
                            'contribution','main_found','quota_confirmed_at')
                          if type(row.get(k)) in (int,float) and math.isfinite(row[k]) and row[k] >= 0}
        result['hunt']['grades'] = [g for g in row.get('grades',[]) if g in ('甲等','乙等','丙等','失败')]
        rewards = row.get('rewards')
        if isinstance(rewards,dict):
            result['hunt']['rewards'] = {n:q for n,q in rewards.items()
                if isinstance(n,str) and re.fullmatch(r'[\w\u3400-\u9fff·（）() +\-]{1,80}',n)
                and not any(s in n.lower() for s in ('token','initdata','signature','session'))
                and type(q) is int and q >= 0}
    for name in ('result','health'):
        if isinstance(row.get(name),dict):result[name]={k:row[name][k] for k in FIELDS if k in row[name]}
    if path.name=='fishing.json' and isinstance(row.get('result'),dict):
        result['fishing_performance']=fishing_performance(row['result'])
        if isinstance(row.get('updated_at'),str) and re.fullmatch(r'[0-9TZ:.+ -]{10,40}',row['updated_at']):
            result['result_updated_at']=row['updated_at']
    if isinstance(row.get('identities'),list):
        result['identities']=[{k:r[k] for k in ('player_id','identity','status','completed','limit','receipt_confirmed') if k in r}
                              for r in row['identities'] if isinstance(r,dict)]
    if isinstance(row.get('events'),dict):result['recorded_event_count']=len(row['events'])
    for key in ('filter_version','rare_item_accounts','rare_item_outbox'):
        if key in row:result[key]=row[key]
    if isinstance(row.get('rare_activation_receipt'),dict):
        result['rare_activation_receipt']={k:row['rare_activation_receipt'].get(k) for k in ('target','message_id','success')}
    if not result:return None
    result['source_file']=str(path.relative_to(root));return result


def refresh_automation_dossiers(root,state_root,*,now=None,manifest_path=MANIFEST,supervisor_path=SUPERVISOR_STATE,force=False):
    root=Path(root);state_root=Path(state_root);now=time.time() if now is None else now
    output=state_root/'task-dossiers';meta=read_json(output/'collector.json')
    if not force and now<float(meta.get('next_scan_at') or 0):return {'status':'waiting'}
    manifest=read_json(manifest_path)
    if not isinstance(manifest.get('jobs'),list):return {'status':'manifest_unavailable'}
    registry=read_json(registry_path(root));entries=active_entries(registry)
    by_label={}
    for entry in entries:
        for kind in entry['kinds']:by_label.setdefault(launchagent_label(entry,kind),[]).append(entry)
    jobs={job['Label']:job for job in manifest['jobs'] if isinstance(job,dict) and isinstance(job.get('Label'),str)}
    for entry in entries:
        if entry.get('interval_dispatch_seconds') is not None:
            label=launchagent_label(entry,'send')
            jobs[label]={'Label':label,'StartInterval':entry['interval_dispatch_seconds'],'interval_dispatch':True,
                         'ProgramArguments':[str(root/'scripts'/f"send_{entry['flow']}_{entry['account']}.sh")]}
    snapshot=read_json(supervisor_path);fresh=now-float(snapshot.get('updated_at') or 0)<30
    store=DossierStore(output);fingerprints=meta.get('fingerprints',{});updated=0
    for label,job in jobs.items():
        owners=by_label.get(label,[]);entry=owners[0] if owners else None
        paths=[]
        for owner in owners or [None]:paths.extend(evidence_paths(root,label,owner))
        args=job.get('ProgramArguments') or []
        for arg in args:
            match=re.search(r'xiuxian_tianji_trial_(tg\d+)\.sh',str(arg))
            if match:paths.append(root/'tianji_trial/state/accounts'/match.group(1)/'summary.json')
        evidence=[value for path in dict.fromkeys(paths) if (value:=summarize_file(path,root)) is not None]
        runtime=(snapshot.get('jobs') or {}).get(label,{})
        if job.get('interval_dispatch') and entry:
            schedule=read_json(state_root/'accounts'/entry['account']/entry['flow']/'interval_dispatch.json')
            runtime={k:schedule.get(k) for k in ('last_start_ts','next_due_ts','pid')}
        scheduler={'snapshot_fresh':fresh,'observed':runtime,'disabled':label in manifest.get('disabled_labels',[]),
                   'interval_seconds':job.get('StartInterval'),'calendar':job.get('StartCalendarInterval'),
                   'cron':job.get('Cron'),'persistent':bool(job.get('KeepAlive'))}
        # A dossier is updated from observed evidence, not by claiming every
        # intermediate scheduler tick was individually captured.
        programs=sorted(set(re.findall(r'[\w./-]+\.(?:py|sh)', ' '.join(str(a) for a in args))))
        if '-m' in args:
            index=args.index('-m')
            if index+1<len(args) and re.fullmatch(r'[A-Za-z0-9_.]+',str(args[index+1])):
                programs.append(str(args[index+1]).replace('.','/')+'.py')
        if (entry or {}).get('miniapp_module') == 'treasure_hunt':
            programs.extend('treasure_hunt/'+name+'.py' for name in
                            ('run_cycle','runtime','protocol','strategy','reports','health'))
        if (entry or {}).get('miniapp_module') == 'tianxing_exploration':
            programs.extend('tianxing_exploration/'+name+'.py' for name in
                            ('__init__','run_cycle','runtime','protocol','client','health'))
        digests={}
        for name in programs:
            path=Path(name);path=path if path.is_absolute() else root/path
            if path.is_file() and path.is_relative_to(root):digests[name]=hashlib.sha256(path.read_bytes()).hexdigest()
        proof={'scheduler':scheduler,'business':evidence,'program_digests':digests}
        fingerprint=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest()
        if fingerprints.get(label)==fingerprint:continue
        status='disabled' if scheduler['disabled'] else 'observed' if fresh else 'snapshot_stale'
        objective=(entry or {}).get('schedule')
        if (entry or {}).get('transport') == 'miniapp':
            objective = ('通过天机阁 MiniApp 执行原账号流程；服务器回执确认结果，失败不回退群发送。 '
                         + (objective or '沿用原调度和账号范围。'))
        if not objective:
            objective=('六账号青元子：监听官方入口、按账号完成战斗和结算；分别保留运行就绪与正式战果。'
                       if label.endswith(('qingyuanzi-six.monitor','qyz-battle-report')) else
                       '按正式任务入口运行；具体业务判据见原程序和回执。')
        store.update('automation:'+label,title=label,kind='automation',status=status,
                     objective=objective,
                     source={'label':label,'programs':programs,'program_digests':digests,'manifest':str(manifest_path)},scheduler=scheduler,business=evidence,
                     scope={'accounts':sorted(set(e.get('account','') for e in owners)),
                            'flows':sorted(set(e.get('flow','') for e in owners))} if owners else {},
                     lessons='进程退出码和运行次数用于排障；业务成功必须有账号匹配的官方回执。人工优化结论写入同名 .notes.md。',
                     event={'id':fingerprint,'type':'runtime_observation','summary':'调度或业务证据更新（只读观测）'},
                     publish=False)
        fingerprints[label]=fingerprint;updated+=1
    store.publish()
    write_json(output/'collector.json',{'updated_at':now,'next_scan_at':now+300,'task_count':len(jobs),
                                       'updated_dossiers':updated,'fingerprints':fingerprints,'snapshot_fresh':fresh})
    return {'status':'ok','task_count':len(jobs),'updated_dossiers':updated,'snapshot_fresh':fresh}
