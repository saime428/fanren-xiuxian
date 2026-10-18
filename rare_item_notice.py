"""Account-attributed rare-item receipts and a durable Hermes notification outbox."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

from miniapp_shared.accounts import ACCOUNT_USERNAMES, GAME_CHAT_ID, GAME_GROUP, GAME_TOPIC_ID, NOTIFY

CHAT_ID = GAME_CHAT_ID
TOPIC_ID = GAME_TOPIC_ID
TARGET = NOTIFY.get('target', '')
ITEMS = ('庚金', '青元剑诀（上）', '青元剑诀（中）', '青元剑诀（下）')
ACTOR = re.compile(r'(?<![\w@])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])')
ITEM = re.compile(r'庚金(?![\u4e00-\u9fffA-Za-z_])|青元剑诀\s*\(\s*([上中下])\s*\)')
GAIN = re.compile(r'获得|获取|得到|领悟|收获|发现|寻得|拾得|赢得|购得|换得|奖励[：:]')
CONDITIONAL = re.compile(r'未(?:能)?|没有|无法|不能|尚未|可能|概率|几率|有机会|可(?:以)?|能够|将会|预计|如果|若|需要|消耗|售价|预览|奖励池')
TRANSLATE = str.maketrans({'劍':'剑', '訣':'诀', '獲':'获', '領':'领', '穫':'获', '發':'发', '現':'现', '尋':'寻', '獎':'奖', '勵':'励', '購':'购', '換':'换'})
BY_USERNAME = {name.casefold(): account for account, name in ACCOUNT_USERNAMES.items()}
BY_ACTOR_ID = {int(account[2:]): name for account, name in ACCOUNT_USERNAMES.items()}


@dataclass(frozen=True)
class ItemReward:
    account: str
    item: str
    quantity: int | None = None


def normalized(text):
    return unicodedata.normalize('NFKC', str(text or '')).translate(TRANSLATE)


def contains_watched_item(text):
    return ITEM.search(normalized(text)) is not None


def parse_gains(text: str, *, context_actors=()) -> tuple[ItemReward, ...]:
    """Only positive acquisition clauses, never inventories, costs or possible drops.

    Explicit recipients take priority over a reply-parent fallback. Ambiguous
    multi-player prose is left unattributed rather than crediting every mention.
    """
    text = normalized(text)
    all_actors = tuple(dict.fromkeys(x.casefold() for x in ACTOR.findall(text)))
    rewards = []
    for match in ITEM.finditer(text):
        if match.start() and re.match(r'[\u4e00-\u9fffA-Za-z_]', text[match.start()-1]):
            # A gain verb may touch the name directly; longer item names may not.
            if not GAIN.search(text[max(0,match.start()-4):match.start()]):
                continue
        start = max((m.end() for m in re.finditer(r'\n\n|[。!?]',text[:match.start()])),default=0)
        prefix = text[start:match.start()][-250:]
        gains = list(GAIN.finditer(prefix))
        if not gains:
            continue
        gain = gains[-1]
        before, after = prefix[:gain.start()], prefix[gain.end():]
        immediate = re.split(r'[\n,，;；:：]', before)[-1][-30:]
        if CONDITIONAL.search(immediate) or CONDITIONAL.search(after):
            continue
        if re.match(r'[】\]》\s]*(?:的)?(?:概率|几率|机会|需要|不足)', text[match.end():]):
            continue
        clause = re.split(r'[\n;；]', prefix)[-1]
        actors = tuple(dict.fromkeys(x.casefold() for x in ACTOR.findall(clause)))
        if len(actors) > 1 and not re.search(r'共同|双方|二人|一起', clause):
            # Simple list subjects are valid; attack/transfer prose is not.
            if not re.search(r'@\w+\s*(?:和|与|、|及)\s*@\w+\s*(?:共同)?\s*(?:获得|领悟|收获)', clause):
                continue
        if not actors:
            if re.search(r'共同|双方|二人', prefix):
                actors = all_actors
            elif len(all_actors) == 1:
                actors = all_actors
            elif not all_actors:
                actors = tuple(str(x).lstrip('@').casefold() for x in context_actors)
        name = '庚金' if match.group(1) is None else f'青元剑诀（{match.group(1)}）'
        suffix = text[match.end():match.end()+35]
        quantity = re.match(r'[】\]》\s]*(?:[xX×*]\s*(\d+)|(\d+)\s*(?:个|块|份|本|卷))', suffix)
        count = int(quantity.group(1) or quantity.group(2)) if quantity else None
        if count is not None and count <= 0:
            continue
        for actor in actors:
            if actor in BY_USERNAME:
                reward = ItemReward(BY_USERNAME[actor], name, count)
                if reward not in rewards:
                    rewards.append(reward)
    return tuple(rewards)


def structured_gains(payload, account):
    """Extract only reward fields supplied by a caller with a confirmed receipt."""
    if account not in ACCOUNT_USERNAMES or not isinstance(payload, dict):
        return ()
    rewards = []
    for key in ('text', 'message', 'resultText', 'rewardText', 'description'):
        if isinstance(payload.get(key), str):
            rewards.extend(parse_gains(payload[key], context_actors=(ACCOUNT_USERNAMES[account],)))
    for key in ('rewards', 'reward', 'bonusLoot', 'loot', 'drops'):
        value = payload.get(key)
        if isinstance(value, dict):
            rows = [value] if value.get('name') or value.get('itemName') else [
                {'name': name, 'quantity': count} for name, count in value.items()]
        else:
            rows = value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = normalized(row.get('name') or row.get('itemName'))
            match = ITEM.fullmatch(name)
            if match is None:
                continue
            item = '庚金' if match.group(1) is None else f'青元剑诀（{match.group(1)}）'
            raw = row.get('quantity', row.get('qty', row.get('count')))
            if isinstance(raw,bool):continue
            try:
                quantity = int(raw) if raw is not None and not isinstance(raw, bool) else None
            except (TypeError, ValueError):
                quantity = None
            if quantity is None or quantity > 0:
                rewards.append(ItemReward(account, item, quantity))
    return tuple(dict.fromkeys(rewards))


def spool_rewards(root: Path, rewards, *, source, source_id, link=''):
    """Immutable, receipt-scoped inbox files; no game state or secrets are copied."""
    if not rewards or os.environ.get('XIU_REWARD_REPORTING_DISABLED') == '1':
        return False
    rows = [{'account': r.account, 'item': r.item, 'quantity': r.quantity} for r in rewards]
    if any(r['account'] not in ACCOUNT_USERNAMES or r['item'] not in ITEMS for r in rows):
        raise ValueError('reward_identity_invalid')
    key = hashlib.sha256(f'{source}|{source_id}|{json.dumps(rows,sort_keys=True)}'.encode()).hexdigest()
    directory = Path(root) / 'state/rare_item_notice/inbox'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{key}.json'
    if path.exists():
        return False
    payload = {'source': source, 'source_id': str(source_id), 'link': link,
               'rewards': rows, 'observed_at': time.time()}
    temporary = directory / f'.{key}.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(payload,ensure_ascii=False)+'\n')
    temporary.chmod(0o600)
    os.replace(temporary,path)
    return True


def spool_command_receipt(state_dir: Path, payload):
    from command_confirmation import valid_receipt_identity
    directory = Path(state_dir).resolve()
    if directory.parent.parent.name != 'accounts' or directory.parent.parent.parent.name != 'state':
        return False
    account, flow = directory.parent.name, directory.name
    if account not in ACCOUNT_USERNAMES or not valid_receipt_identity(payload, actor_id=int(account[2:]), flow=flow):
        return False
    rewards = parse_gains(payload.get('ack_reply_text') or '', context_actors=(ACCOUNT_USERNAMES[account],))
    message_id = payload.get('ack_reply_message_id') or payload.get('reply_message_id')
    return spool_rewards(directory.parents[3], rewards, source='telegram', source_id=f'{CHAT_ID}:{message_id}',
                         link=f'https://t.me/{GAME_GROUP}/{TOPIC_ID}/{message_id}')


def trusted_message(message, trusted_sender_ids):
    reply = getattr(message, 'reply_to', None)
    return (getattr(message, 'sender_id', None) in trusted_sender_ids
            and getattr(message, 'chat_id', None) == CHAT_ID
            and getattr(message, 'fwd_from', None) is None
            and (getattr(reply, 'reply_to_top_id', None) or getattr(reply, 'reply_to_msg_id', None)) == TOPIC_ID)


async def telegram_rewards(message, *, trusted_sender_ids, lookup_parent=None):
    if not trusted_message(message, trusted_sender_ids):
        return ()
    text = str(getattr(message, 'raw_text', '') or getattr(message, 'message', '') or '')
    if not contains_watched_item(text):
        return ()
    actors = ()
    reply_id = getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)
    if not ACTOR.search(text) and lookup_parent is not None and reply_id and reply_id != TOPIC_ID:
        parent = await lookup_parent(reply_id)
        reply = getattr(parent, 'reply_to', None)
        if (getattr(parent, 'id', None) == reply_id and getattr(parent, 'chat_id', None) == CHAT_ID
                and getattr(parent, 'fwd_from', None) is None
                and (getattr(reply, 'reply_to_top_id', None) or getattr(reply, 'reply_to_msg_id', None)) == TOPIC_ID):
            actor = BY_ACTOR_ID.get(getattr(parent, 'sender_id', None))
            if actor:
                actors = (actor,)
    return parse_gains(text, context_actors=actors)


class RewardOutbox:
    """The notification observer owns this database; delivery receipts are durable."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The owning listener drains after every group message; keep one
        # connection for the process instead of opening SQLite each time.
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS rewards (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
                account TEXT NOT NULL, item TEXT NOT NULL, quantity INTEGER, link TEXT NOT NULL,
                observed_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, delivery_id INTEGER, delivered_at REAL, error_type TEXT)''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        """One committed (or rolled back) transaction on the shared connection."""
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def enqueue(self, rewards, *, source, source_id, link='', now=None):
        now = time.time() if now is None else now
        count = 0
        with self.connect() as db:
            for reward in rewards:
                if reward.account not in ACCOUNT_USERNAMES or reward.item not in ITEMS:
                    raise ValueError('reward_identity_invalid')
                key = hashlib.sha256(f'{source}|{source_id}|{reward.account}|{reward.item}'.encode()).hexdigest()
                cursor = db.execute('''INSERT OR IGNORE INTO rewards
                    (key,source,source_id,account,item,quantity,link,observed_at,status)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (key, source, str(source_id), reward.account, reward.item, reward.quantity, link, now, 'pending'))
                count += cursor.rowcount
        return count

    async def drain(self, deliver, *, now=None, limit=10):
        now = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute("SELECT * FROM rewards WHERE status != 'delivered' AND retry_at <= ? ORDER BY observed_at,key LIMIT ?", (now, limit)).fetchall()
        delivered = 0
        for row in rows:
            amount = f" ×{row['quantity']}" if row['quantity'] is not None else ''
            text = f"🎉 稀有物品获得\n账号：@{ACCOUNT_USERNAMES[row['account']]}\n物品：{row['item']}{amount}"
            if row['link']:
                text += '\n' + row['link']
            else:
                text += f"\n来源：{row['source']} / {row['source_id']}"
            with self.connect() as db:
                db.execute("UPDATE rewards SET status='sending', attempts=attempts+1, retry_at=? WHERE key=?", (now + 120, row['key']))
            try:
                receipt = await deliver(text)
                if (not isinstance(receipt, dict) or receipt.get('success') is not True
                        or receipt.get('target') != TARGET or type(receipt.get('message_id')) is not int
                        or receipt['message_id'] <= 0 or receipt.get('skipped') or receipt.get('error')):
                    raise ValueError('hermes_delivery_unconfirmed')
            except Exception as exc:
                delay = min(3600, 30 * (2 ** min(int(row['attempts']), 7)))
                with self.connect() as db:
                    db.execute("UPDATE rewards SET status='pending',retry_at=?,error_type=? WHERE key=?", (now + delay, type(exc).__name__, row['key']))
                continue
            with self.connect() as db:
                db.execute("UPDATE rewards SET status='delivered',delivery_id=?,delivered_at=?,error_type=NULL WHERE key=?", (receipt['message_id'], now, row['key']))
            delivered += 1
        return delivered

    def summary(self):
        with self.connect() as db:
            return {row['status']: row['count'] for row in db.execute('SELECT status,COUNT(*) AS count FROM rewards GROUP BY status')}

    def confirm_existing_notice(self, *, source, source_id, accounts, receipt):
        """A confirmed legacy bonus notice already reports these same rewards."""
        if (not isinstance(receipt,dict) or receipt.get('success') is not True or receipt.get('target')!=TARGET
                or type(receipt.get('message_id')) is not int or receipt['message_id']<=0):
            return
        with self.connect() as db:
            for account in accounts:
                if account not in ACCOUNT_USERNAMES:continue
                db.execute("UPDATE rewards SET status='delivered',delivery_id=?,delivered_at=?,error_type=NULL WHERE source=? AND source_id=? AND account=? AND status!='delivered'",
                           (receipt['message_id'],time.time(),source,str(source_id),account))

    def import_spool(self, root: Path):
        count = 0
        for path in sorted((Path(root) / 'state/rare_item_notice/inbox').glob('*.json')):
            try:
                value = json.loads(path.read_text())
                rewards = [ItemReward(**row) for row in value['rewards']]
                count += self.enqueue(rewards, source=value['source'], source_id=value['source_id'],
                                      link=value.get('link',''), now=value.get('observed_at'))
            except (OSError, ValueError, TypeError, KeyError):
                continue  # an unreadable file stays for inspection
            # The committed outbox row is now the durable copy and dedupes any
            # re-spool by key; do not re-read every handled file each minute.
            path.unlink(missing_ok=True)
        return count
