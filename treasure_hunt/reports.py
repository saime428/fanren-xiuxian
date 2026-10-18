"""One daily reward report to the operator, backed by confirmed settlements."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import time

from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for
from miniapp_shared.notify import Notifier
from miniapp_shared.state import LockBusy, day_key, exclusive_lock, read_json, write_json

FLOW = 'treasure_hunt'
ALL_ACCOUNTS = accounts_for(FLOW)
REPORT_LIMIT = 3400  # below the notifier's own 3500-character cut


def build(rows: list[dict], day: str) -> dict:
    by_account = {r.get('account'): r for r in rows if r.get('day') == day}
    complete = len(by_account) == len(ALL_ACCOUNTS) and all(
        by_account[a].get('status') in ('daily_complete', 'daily_complete_with_unknown', 'no_dwelling') for a in ALL_ACCOUNTS)
    finished = len(by_account) == len(ALL_ACCOUNTS) and all(by_account[a].get('status') in (
        'daily_complete','daily_complete_with_unknown','no_dwelling','unconfirmed_start','unconfirmed_settle') for a in ALL_ACCOUNTS)
    lines = [f'【洞府寻宝】{day}', '六账号今日次数已核对' if complete else '六账号进度与已确认收益']
    total = Counter()
    contribution = settled = found = preexisting = unconfirmed = 0
    for account in ALL_ACCOUNTS:
        row = by_account.get(account)
        label = '@'+ACCOUNT_USERNAMES[account]
        if not row:
            lines.append(f'{label}：尚待运行'); continue
        count = row.get('settled_runs', 0)
        # A quota count or speculative run bag alone never becomes a reward.
        confirmed = row.get('confirmed') is True and count > 0 and len(row.get('receipt_keys', [])) == count
        if confirmed:
            gains = row.get('rewards', {})
            total.update(gains)
            contribution += row.get('contribution', 0)
            settled += count
            found += row.get('main_found', 0)
            goods = '、'.join(f'{name}×{amount}' for name, amount in sorted(gains.items())) or '无物品'
            grades = '、'.join(f'{key}×{value}' for key,value in sorted(Counter(row.get('grades',[])).items()))
            lines.append(f'{label}：结算 {count} 局，{grades}；贡献 +{row.get("contribution",0)}；{goods}')
        elif row.get('status') == 'no_dwelling':
            lines.append(f'{label}：服务器确认尚无专属洞府')
        else:
            lines.append(f'{label}：已确认结算 0 局')
        prior = row.get('preexisting_used', 0)
        if prior:
            preexisting += prior
            lines.append(f'  接入前已使用 {prior} 次；这些局的收益没有回执，未计入统计。')
        missing = row.get('unconfirmed_runs', 0)
        if missing:
            unconfirmed += missing
            lines.append(f'  {missing} 局被服务器判为会话失效/关闭，未取得结算回执；未计收益。')
        if row.get('status') not in ('daily_complete', 'daily_complete_with_unknown', 'no_dwelling'):
            lines.append(f'  待完成：{row.get("error") or row.get("status") or "pending"}')
    lines += [f'合计：确认结算 {settled} 局，发现主宝匣 {found} 次，宗门贡献 +{contribution}。',
              '物品合计：'+('、'.join(f'{n}×{v}' for n,v in sorted(total.items())) or '无已确认物品'),
              '策略：线索定位＋收益评估，四中心作开局候选；每日自动运行，结算回执核对后报告。']
    return {'day': day, 'complete': complete, 'finished': finished, 'accounts': rows, 'settled_runs': settled,
            'unconfirmed_runs': unconfirmed,
            'main_found': found, 'preexisting_used': preexisting, 'contribution': contribution,
            'rewards': dict(total), 'text': '\n'.join(lines)}


def report(root: Path, *, now=None, force=False, notifier=None) -> dict:
    now = time.time() if now is None else now
    root = Path(root); day = day_key(now)
    try:
        with exclusive_lock(root/'state'/FLOW/'report.lock'):
            rows = [read_json(root/'state/accounts'/a/FLOW/'last_result.json') for a in ALL_ACCOUNTS]
            value = build(rows, day)
            path = root/'state'/FLOW/'reports'/f'{day}.json'
            write_json(path, value)
            path.with_suffix('.md').write_text(value['text']+'\n', encoding='utf-8')
            if value['finished'] or force:
                key = f'{day}:complete' if value['complete'] else f'{day}:partial'
                text = value['text']
                if len(text) > REPORT_LIMIT:
                    # Send the head rather than nothing; the full text stays in reports/<day>.md.
                    note = f'\n…（已截断，全文见 reports/{day}.md）'
                    text = text[:REPORT_LIMIT - len(note)] + note
                    value['truncated'] = True
                value['notification'] = (notifier or Notifier(root, FLOW)).send_once(key, text)
                write_json(path, value)
            return {'status': 'ready' if value['complete'] else 'pending',
                    'settled_runs': value['settled_runs'], 'notification': value.get('notification')}
    except LockBusy:
        return {'status': 'report_lock_busy'}
