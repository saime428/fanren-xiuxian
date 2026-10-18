"""Local, redacted task dossiers. No model calls, network calls or business mutations."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = 'task-dossiers-v1'
SECRET = re.compile(r'''(?i)(\b(?:api[_-]?(?:key|hash)|access[_-]?token|refresh[_-]?token|token|password|passwd|authorization|cookie|initData|tgWebAppData)\b[\s"']*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&]+)''')
BOT_TOKEN = re.compile(r'\b\d{6,12}:[A-Za-z0-9_-]{25,}\b')
KEY = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b')
SHANGHAI = ZoneInfo('Asia/Shanghai')
# history/*.jsonl is append-only; trim it to the newest lines once it grows
# past the byte cap (amortized, instead of rewriting it on every append).
HISTORY_MAX_BYTES = 512 * 1024
HISTORY_KEEP_LINES = 1000


def safe_text(value, limit=8000):
    text = str(value or '')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[REDACTED PRIVATE KEY]',text,flags=re.S)
    text = re.sub(r'(https?://)[^/@:\s]+:[^/@\s]+@',r'\1[REDACTED]@',text)
    return KEY.sub('[REDACTED]',BOT_TOKEN.sub('[REDACTED]',SECRET.sub(r'\1[REDACTED]',text)))[:limit]


def safe(value):
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if re.fullmatch(r'(?i)(?:token|password|api_key|api_hash|authorization|cookie|initData|session)',str(k))
                         else safe(v)) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [safe(v) for v in value[:250]]
    if isinstance(value,str):return safe_text(value)
    if value is None or isinstance(value,(int,float,bool)):return value
    return safe_text(value)


def read_json(path):
    try:
        value=json.loads(Path(path).read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o600);os.replace(temp,path)


@contextmanager
def dossier_lock(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'.dossier.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        yield


def stamp(ts):
    return datetime.fromtimestamp(float(ts),SHANGHAI).strftime('%Y-%m-%d %H:%M:%S %Z')


def append_history(path,event):
    path.parent.mkdir(exist_ok=True)
    with path.open('a') as out:out.write(json.dumps(event,ensure_ascii=False)+'\n')
    if path.stat().st_size>HISTORY_MAX_BYTES:
        lines=path.read_text().splitlines(keepends=True)[-HISTORY_KEEP_LINES:]
        temp=path.with_name(f'.{path.name}.{os.getpid()}.tmp');temp.write_text(''.join(lines));os.replace(temp,path)


class DossierStore:
    def __init__(self,root):
        self.root=Path(root)
        self._unpublished={}

    def identifier(self,key):return hashlib.sha256(str(key).encode()).hexdigest()[:24]

    def get(self,key):return read_json(self.root/'records'/f'{self.identifier(key)}.json')

    def update(self,key,*,title='',kind='maintenance',status=None,objective=None,event=None,publish=True,**fields):
        """Write one record. publish=False defers index/README until publish()."""
        with dossier_lock(self.root):
            ident=self.identifier(key);path=self.root/'records'/f'{ident}.json';row=read_json(path)
            row.setdefault('key',str(key));row.setdefault('created_at',time.time());row.setdefault('events',[])
            row.update(version=VERSION,kind=kind,updated_at=time.time())
            if title:row['title']=safe_text(title,180)
            if status is not None:row['status']=safe_text(status,100)
            if objective is not None:row['objective']=safe_text(objective)
            fields=safe(fields)
            if isinstance(fields.get('source'),dict):fields['source']={**row.get('source',{}),**fields['source']}
            row.update(fields)
            if event:
                ev=safe(event);event_id=ev.get('id') or hashlib.sha256(json.dumps(ev,sort_keys=True).encode()).hexdigest()
                if not any(e.get('id')==event_id for e in row['events']):
                    ev.update(id=event_id,observed_at=time.time());row['events'].append(ev)
                    append_history(self.root/'history'/f'{ident}.jsonl',ev)
                row['events']=row['events'][-200:]
            write_json(path,row)
            md=self.root/'dossiers'/f'{ident}.md';md.parent.mkdir(parents=True,exist_ok=True)
            temp=md.with_suffix('.tmp');temp.write_text(self.render(row));temp.chmod(0o600);os.replace(temp,md)
            self._unpublished[ident]={'title':row.get('title') or ident,'kind':kind,'status':row.get('status','observed'),
                                      'updated_at':row['updated_at'],'file':str(md.relative_to(self.root))}
            if publish:self._publish()
            return md

    def publish(self):
        """Write index.json and README.md once for every deferred update."""
        if self._unpublished:
            with dossier_lock(self.root):self._publish()

    def _publish(self):
        # README lists every dossier: write it once per batch (O(N)), not once
        # per updated record (O(N^2) per collector pass).
        index=read_json(self.root/'index.json');index.update(self._unpublished);self._unpublished={}
        write_json(self.root/'index.json',index)
        lines=['# 自动任务了解档案','',f'生成器：{VERSION}。仅记录经过筛选的信息；“正常退出”不等于业务成功。','']
        if (self.root/'automation/README.md').exists():
            lines+=['[生产自动任务档案](automation/README.md)','']
        for item in sorted(index.values(),key=lambda x:x['updated_at'],reverse=True):
            label=item['title'].replace('\n',' ').replace('[','(').replace(']',')')
            lines.append(f"- [{label}]({item['file']}) — {item['kind']} / {item['status']}")
        (self.root/'README.md').write_text('\n'.join(lines)+'\n')

    @staticmethod
    def render(row):
        lines=[f"# {row.get('title') or row['key']}：了解档案",'',f"- 类型：{row.get('kind')}",
               f"- 状态：{row.get('status','observed')}",f"- 建档：{stamp(row['created_at'])}",f"- 更新：{stamp(row['updated_at'])}",
               '', '## 目标', '', row.get('objective') or '由任务入口和已观测事件生成，详细目标待补充。']
        for key,label in [('source','来源与入口'),('scope','适用范围'),('scheduler','调度观测'),('business','业务与回执证据'),
                          ('changes','文件变更'),('validation','验证'),('result','结果'),('next_steps','后续工作'),('lessons','复盘与优化')]:
            value=row.get(key)
            if value in (None,[],{},''):continue
            lines+=['',f'## {label}','']
            if isinstance(value,str):lines.append(value)
            else:lines+=['```json',json.dumps(value,ensure_ascii=False,indent=2),'```']
        lines+=['','## 行动记录','']
        for ev in row.get('events',[]):
            summary=ev.get('summary') or ev.get('type') or '观测更新'
            lines.append(f"- {stamp(ev.get('observed_at',row['updated_at']))} — {summary}")
        lines+=['','## 人工复盘补充','',
                '自动档案不覆盖人工笔记。可在同名 `.notes.md` 中补充原因、权衡和下一次优化；未验证项保留为待验证。','']
        return '\n'.join(lines)


def user_objective(text):
    text=str(text or '')
    match=re.search(r'<objective>\s*(.*?)\s*</objective>',text,re.S)
    if match:return safe_text(match.group(1))
    text=re.sub(r'<environment_context>.*?</environment_context>','',text,flags=re.S)
    return safe_text(text.strip())


def content_text(content):
    if isinstance(content,str):return content
    if isinstance(content,list):return '\n'.join(str(part.get('text') or '') for part in content if isinstance(part,dict))
    return ''


def update_agent_task(store,session,**fields):
    key=session.get('task_key')
    if key:store.update(key,kind='maintenance',**fields)
    goal=session.get('goal_key')
    if goal and session.get('goal_status')=='active':
        shared={k:v for k,v in fields.items() if k not in {'title','objective','status','source'}}
        if 'changes' in shared:shared['changes']=sorted(set(store.get(goal).get('changes',[])+shared['changes']))
        if shared:store.update(goal,kind='goal',**shared)


def consume_codex_event(store,session,row):
    """Adapter for observed Codex rollout events; excludes reasoning and raw tool IO."""
    payload=row.get('payload') or {}
    if row.get('type')=='session_meta':
        session.update(thread_id=payload.get('id'),cwd=payload.get('cwd'))
        return
    if row.get('type')!='event_msg':return
    typ=payload.get('type');thread=session.get('thread_id')
    if not thread:return
    if typ=='thread_goal_updated':
        goal=payload.get('goal') or {};created=goal.get('createdAt')
        if created is None:return
        key=f'codex-goal:{thread}:{created}'
        session.update(goal_key=key,goal_status=goal.get('status'),goal_objective=user_objective(goal.get('objective')))
        previous=store.get(key)
        status='complete' if previous.get('status')=='complete' and goal.get('status')=='active' else goal.get('status')
        store.update(key,title=session['goal_objective'][:100],kind='goal',status=status,objective=session['goal_objective'],
                     source={'provider':'codex','thread_id':thread,'cwd':session.get('cwd')},tokens_used=goal.get('tokensUsed'),
                     time_used_seconds=goal.get('timeUsedSeconds'))
        return
    if typ=='task_started':
        turn=payload.get('turn_id');session['turn_id']=turn
        key=f'codex-turn:{thread}:{turn}'
        session['task_key']=key
        store.update(key,title=session.get('pending_prompt') or session.get('goal_objective') or 'Codex 维护任务',
                     kind='maintenance',status='active',parent_goal=session.get('goal_key'),
                     scope={'parent_goal':session.get('goal_key'),'goal_objective':session.get('goal_objective')},
                     source={'provider':'codex','thread_id':thread,'turn_id':turn,'cwd':session.get('cwd')},
                     event={'id':f'{turn}:start','type':'task_started','summary':'开始执行任务'})
        return
    key=session.get('task_key') or session.get('goal_key')
    if not key:return
    if typ=='task_complete':
        update_agent_task(store,session,status='turn_finished',result=safe_text(payload.get('last_agent_message')),
                     event={'id':f"{payload.get('turn_id')}:complete",'type':'task_complete','summary':'本轮回复结束；Goal 状态单独核对'})
        return
    if typ!='item_completed':return
    item=payload.get('item') or {};item_type=item.get('type');event_id=item.get('id')
    if item_type=='UserMessage':
        prompt=user_objective(content_text(item.get('content')));session['pending_prompt']=prompt
        current=store.get(key)
        objective=current.get('objective') or prompt
        update_agent_task(store,session,title=objective[:100],objective=objective,
                     event={'id':event_id,'type':'user_instruction','summary':prompt[:1600]})
    elif item_type=='FileChange':
        paths=list((item.get('changes') or {}).keys())
        current=store.get(key);changes=sorted(set(current.get('changes',[])+paths))
        update_agent_task(store,session,changes=changes,event={'id':event_id,'type':'file_change','summary':'修改文件：'+', '.join(paths)})
    elif item_type=='AgentMessage':
        text=content_text(item.get('content'))
        if item.get('phase') in {'commentary','final'} and text:
            update_agent_task(store,session,event={'id':event_id,'type':'agent_update','summary':safe_text(text,1800)})
    elif item_type=='CommandExecution':
        command=' '.join(str(x) for x in item.get('command',[]))
        category=next((x for x in ['pytest','unittest','git push','git commit','git diff --check','bash -n'] if x in command),'command')
        output=str(item.get('aggregated_output') or '')
        evidence=re.findall(r'(?m)^(?:Ran \d+ tests[^\n]*|\d+ passed[^\n]*|OK|FAILED[^\n]*)$',output[-20000:])
        if category=='command' and item.get('exit_code')==0 and not evidence:return
        summary=f"{category}：退出码 {item.get('exit_code')}"
        if evidence:summary+='；'+' / '.join(evidence)
        update_agent_task(store,session,event={'id':event_id,'type':'command_result','summary':summary})
