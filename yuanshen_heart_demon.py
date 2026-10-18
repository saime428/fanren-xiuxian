"""Account-bound heart-demon choices on the existing second-soul listener.

Only an attributed official warning authorizes a choice. A terminal edit is
bound to that warning (or our choice), never to a guessed active account.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from account_lifecycle import AccountPausedError, assert_account_allowed
from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TOPIC_ID, OFFICIAL_REPLY_SENDER_IDS
from flow_registry import registry_path
from miniapp_shared.priority import yield_to_qyz
from miniapp_shared.state import exclusive_lock, read_json, write_json
from reply_parsing import get_message_top_id, parse_cooldown_retry_delay_seconds
from transport import send_with_global_gate
from yuanshen_automation import ACCOUNT, ACTOR_ID, USERNAME, STATE_RELATIVE as TRAIN_STATE

CHOICE_COMMAND = '.抉择 稳固道心'
POLICY_VERSION = 'heart-demon-stable-v1'
# Recognize old or manual choices for deduplication; only CHOICE_COMMAND is sent.
KNOWN_CHOICE_COMMANDS = {CHOICE_COMMAND, '.抉择 强行突破'}
OBSERVER_LABEL = f'ai.hermes.xiuxian.{ACCOUNT}.yuanshen.return_observer'
STATE_RELATIVE = TRAIN_STATE.with_name('heart_demon_events.json')
# A conservative client expiry from upstream 9ced80b, not a claimed server limit.
MAX_WARNING_AGE = 3600
RESULT_MAX_AGE = 6 * 3600
TERMINAL = {'break_success', 'break_failed', 'stable_success'}
INACTIVE = TERMINAL | {'expired'}
_REGISTRY_CACHE: dict[Path, tuple[tuple[int, int, int], dict]] = {}


def registry(root: Path) -> dict:
    """The flow registry, parsed again only after the file changes.

    The second-soul listener consults it two or three times per group
    message. ponytail: keyed on (mtime_ns, size, inode); a same-size rewrite
    within one filesystem timestamp tick reusing the inode is missed until
    the next change.
    """
    path = registry_path(root)
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return {}
    key = (meta.st_mtime_ns, meta.st_size, meta.st_ino)
    cached = _REGISTRY_CACHE.get(path)
    if cached is None or cached[0] != key:
        cached = _REGISTRY_CACHE[path] = (key, read_json(path))
    return cached[1]


def policy_enabled(root: Path) -> bool:
    return any(row.get('label') == OBSERVER_LABEL and row.get('enabled') is True
               and row.get('heart_demon_choice') == CHOICE_COMMAND
               for row in registry(root).get('helpers', []))


def official(message: Any) -> bool:
    return (getattr(message, 'chat_id', None) == CANONICAL_CHAT_ID
            and get_message_top_id(message) == CANONICAL_TOPIC_ID
            and getattr(message, 'sender_id', None) in OFFICIAL_REPLY_SENDER_IDS
            and getattr(message, 'fwd_from', None) is None)


def warning(message: Any, now: float) -> dict | None:
    if not official(message):
        return None
    text = str(getattr(message, 'message', '') or '')
    match = re.search(r'道友\s+@([A-Za-z0-9_]+)\s+的第二元神在修炼中遭遇心魔[，,]\s*道心动摇', text)
    date = getattr(message, 'date', None)
    if (not text.startswith('【天道警示·心魔试炼】') or not match or match[1].lower() != USERNAME
            or not re.search(r'回复本消息\s*[.。]抉择\s+稳固道心(?:\s|[（(]|$)', text)
            or date is None or not 0 <= now - date.timestamp() < MAX_WARNING_AGE
            or type(getattr(message, 'id', None)) is not int or message.id <= 0):
        return None
    return {'id': message.id, 'at': date.timestamp(), 'sender_id': message.sender_id,
            'chat_id': CANONICAL_CHAT_ID, 'topic_id': CANONICAL_TOPIC_ID,
            'username': USERNAME, 'status': 'observed'}


def result_kind(text: str) -> str | None:
    for title, status in [('【破而后立·成功】', 'break_success'), ('【破而后立·失败】', 'break_failed'),
                          ('【稳扎稳打·成功】', 'stable_success')]:
        if text.startswith(title):
            return status
    return None


def current_resolution(root: Path, state: dict, now: float) -> dict | None:
    data = read_json(root / STATE_RELATIVE)
    if (data.get('version') != 1 or data.get('account') != ACCOUNT or data.get('actor_id') != ACTOR_ID
            or not isinstance(data.get('events'), dict)):
        return None
    used = int(state.get('last_heart_demon', {}).get('id', 0))
    candidates = []
    for row in data['events'].values():
        at = row.get('resolved_at', 0)
        if (row.get('status') in TERMINAL and row.get('id', 0) > used
                and row.get('chat_id') == CANONICAL_CHAT_ID and row.get('topic_id') == CANONICAL_TOPIC_ID
                and row.get('sender_id') in OFFICIAL_REPLY_SENDER_IDS and row.get('username') == USERNAME
                and 0 <= now - at <= RESULT_MAX_AGE
                and row.get('training_generation') == state.get('last_training_started_at', 0)):
            candidates.append(row)
    return max(candidates, key=lambda row: row['id']) if candidates else None


class HeartDemonResponder:
    def __init__(self, root: Path, *, clock=time.time, send_gate=send_with_global_gate):
        self.root, self.clock, self.send_gate = Path(root), clock, send_gate
        self.path = self.root / STATE_RELATIVE
        self.client = None
        self.lock = asyncio.Lock()

    def load(self) -> dict:
        data = read_json(self.path)
        if data and (data.get('version') != 1 or data.get('account') != ACCOUNT
                     or data.get('actor_id') != ACTOR_ID or not isinstance(data.get('events'), dict)):
            raise ValueError('invalid_heart_demon_inbox')
        return data or {'version': 1, 'account': ACCOUNT, 'actor_id': ACTOR_ID, 'events': {}}

    def save(self, data: dict) -> None:
        data.update(updated_at=self.clock(), policy_version=POLICY_VERSION)
        write_json(self.path, data)

    def observe(self, data: dict, message: Any) -> None:
        if not official(message):
            return
        text = str(getattr(message, 'message', '') or '')
        kind = result_kind(text)
        if kind:
            parent = (getattr(message, 'reply_to_msg_id', None)
                      or getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None))
            ids = {getattr(message, 'id', 0), parent} - {None, 0, CANONICAL_TOPIC_ID}
            matches = [row for row in data['events'].values()
                       if ids & {row['id'], row.get('sent_id', 0)} and row['sender_id'] == message.sender_id]
            date = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
            if len(matches) != 1 or date is None:
                return
            row = matches[0]
            at = date.timestamp()
            if row['status'] in TERMINAL or not row['at'] <= at <= self.clock():
                return
            row.update(status=kind, resolved_at=at, result_message_id=message.id,
                       wait_seconds=(parse_cooldown_retry_delay_seconds(text, []) or 86400)
                       if kind == 'break_failed' else 0)
            self.save(data)
            return
        event = warning(message, self.clock())
        if event is None or str(event['id']) in data['events']:
            return
        state = read_json(self.root / TRAIN_STATE)
        if (state.get('version') != 1 or state.get('account') != ACCOUNT or state.get('actor_id') != ACTOR_ID
                or event['at'] < state.get('last_training_started_at', 0)):
            return
        event['training_generation'] = state.get('last_training_started_at', 0)
        data['events'][str(event['id'])] = event
        # Keep pending choices plus the newest 50 resolved observations.
        terminal = sorted((row for row in data['events'].values() if row['status'] in INACTIVE),
                          key=lambda row: row['id'])
        for old in terminal[:-50]:
            data['events'].pop(str(old['id']))
        self.save(data)

    def guard(self, row: dict) -> bool:
        if not policy_enabled(self.root) or self.client is None:
            return False
        if not 0 <= self.clock() - row['at'] < MAX_WARNING_AGE:
            return False
        try:
            assert_account_allowed(ACCOUNT, path=self.root / 'state/account_lifecycle.json')
        except AccountPausedError:
            return False
        state = read_json(self.root / TRAIN_STATE)
        return (state.get('account') == ACCOUNT and state.get('actor_id') == ACTOR_ID
                and state.get('last_training_started_at', 0) == row['training_generation']
                and float(state.get('transport_not_before', 0)) <= self.clock()
                and not yield_to_qyz('yuanshen_heart_demon', self.clock(), self.root))

    async def advance(self, data: dict, row: dict) -> None:
        if self.clock() - row['at'] >= MAX_WARNING_AGE and row['status'] == 'observed':
            row['status'] = 'expired'
            self.save(data)
            return
        if row['status'] != 'observed' or self.clock() < row.get('retry_at', 0) or not self.guard(row):
            return
        # Check the current anchor before claiming a global send slot. It may
        # have been resolved manually or edited since the push arrived. The
        # slot itself covers only the local re-check and the send.
        try:
            current = await asyncio.wait_for(self.client.get_messages(CANONICAL_CHAT_ID, ids=row['id']), 15)
        except Exception as exc:
            row['error_type'] = type(exc).__name__
            self.save(data)
            return
        self.observe(data, current)
        fresh = warning(current, self.clock())
        if (row['status'] != 'observed' or fresh is None
                or any(fresh[key] != row[key] for key in ('id', 'sender_id', 'at'))):
            return

        async def action():
            if row['status'] != 'observed' or not self.guard(row):
                return None
            row.update(status='send_intent', attempted_at=self.clock(), attempted_command=CHOICE_COMMAND)
            self.save(data)  # Survives a crash between Telegram accepting and returning the ID.
            sent = await asyncio.wait_for(self.client.send_message(
                CANONICAL_CHAT_ID, CHOICE_COMMAND, reply_to=row['id']), timeout=30)
            if type(getattr(sent, 'id', None)) is not int or sent.id <= 0:
                raise ValueError('choice_delivery_unconfirmed')
            row.update(status='sent', sent_id=sent.id, sent_at=self.clock(), sent_command=CHOICE_COMMAND)
            self.save(data)
            return sent

        try:
            await self.send_gate(action, label='yuanshen heart-demon choice', state_dir=self.path.parent)
        except Exception as exc:
            if row['status'] == 'send_intent':
                # An explicit FloodWait means Telegram rejected this RPC.
                seconds = getattr(exc, 'seconds', None)
                if type(seconds) in (int, float) and seconds > 0 and type(exc).__name__ == 'FloodWaitError':
                    row.update(status='observed', retry_at=self.clock() + seconds + 1)
                else:
                    row['status'] = 'delivery_unconfirmed'
            row['error_type'] = type(exc).__name__
            self.save(data)

    async def process(self, messages: list[Any], *, reconcile: bool = False) -> None:
        if self.client is None or not policy_enabled(self.root):
            return
        async with self.lock:
            with exclusive_lock(self.path.with_suffix('.lock')):
                data = self.load()
                for message in messages:
                    self.observe(data, message)
                pending = [row for row in data['events'].values() if row['status'] not in INACTIVE]
                if reconcile:
                    # Saved anchors survive scrolling out of the recent-message scan.
                    for row in pending[-3:]:
                        current = await self.client.get_messages(CANONICAL_CHAT_ID, ids=row['id'])
                        self.observe(data, current)
                        if row['status'] in {'observed', 'send_intent', 'delivery_unconfirmed'}:
                            for message in messages:
                                parent = (getattr(message, 'reply_to_msg_id', None)
                                          or getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None))
                                if (getattr(message, 'chat_id', None) == CANONICAL_CHAT_ID
                                        and getattr(message, 'sender_id', None) == ACTOR_ID
                                        and getattr(message, 'fwd_from', None) is None and parent == row['id']
                                        and type(getattr(message, 'id', None)) is int and message.id > row['id']
                                        and getattr(message, 'message', '') in KNOWN_CHOICE_COMMANDS):
                                    row.update(status='sent', sent_id=message.id,
                                               sent_command=message.message)
                                    self.save(data)
                                    break
                for row in pending:
                    await self.advance(data, row)
