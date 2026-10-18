"""User-owned interval senders serviced by the existing 30-second dispatcher.

The sender inherits its dispatch lock for its entire lifetime. Business state,
Telegram sessions, cooldowns and reply recovery still belong to its own wrapper.
"""
from __future__ import annotations

import fcntl
import json
import math
import subprocess
from pathlib import Path

from command_confirmation import valid_receipt_identity
from flow_registry import active_entries, launchagent_label
from state_store import is_paused, load_json_dict, persist_json_atomic

DISPATCHER_LABEL = 'ai.hermes.xiuxian.return-deadline-dispatch'


def configured_jobs(registry):
    return {launchagent_label(e, 'send'): e for e in active_entries(registry)
            if e.get('interval_dispatch_seconds') is not None}


def reconcile_receipt_deadline(directory, path, state, *, account, flow):
    """Adopt each official result once; the sender still owns business state.

    Cooldown replies can bring the next run forward. A later success starts a
    fresh interval at the reply time, rather than at an earlier dispatch tick.
    Remembering the receipt prevents an unchanged overdue result from starting
    the same wrapper on every tick while its next result is still unavailable.
    """
    result = load_json_dict(directory / 'last_result.json')
    if not valid_receipt_identity(result, actor_id=int(account[2:]), flow=flow):
        return
    raw_due = result.get('next_due_ts')
    try:
        due = float(raw_due)
    except (TypeError, ValueError):  # missing (None) or non-numeric
        due = math.nan
    if isinstance(raw_due, bool) or not math.isfinite(due) or due <= 0:
        raise ValueError('invalid official result deadline')
    receipt = [result.get('sent_message_id') or result.get('message_id'),
               result.get('ack_reply_message_id') or result.get('reply_message_id'), due]
    if state.get('scheduled_receipt') != receipt:
        state.update(account=account, flow=flow, scheduler=DISPATCHER_LABEL,
                     scheduled_receipt=receipt, next_due_ts=due)
        persist_json_atomic(path, state)


def dispatch_intervals(project_root: Path, state_root: Path, registry: dict, *, now: float,
                       retry_labels=(), spawn=None) -> set[str]:
    """Start only due account-scoped wrappers; return labels handled here.

    Called every tick even with no pending reply. A retry keeps the original
    periodic deadline, matching an ordinary non-forced kickstart.
    """
    spawn = subprocess.Popen if spawn is None else spawn
    jobs = configured_jobs(registry)
    for label, entry in jobs.items():
        try:
            _dispatch_one(project_root, state_root, entry, label, now, retry_labels, spawn)
        except Exception as exc:
            print(f'interval dispatch failed label={label} error_type={type(exc).__name__}')
    return set(jobs)


def _dispatch_one(project_root, state_root, entry, label, now, retry_labels, spawn):
    account, flow = entry['account'], entry['flow']
    directory = state_root / 'accounts' / account / flow
    if is_paused(directory, flow)[0]:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'interval_dispatch.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # inherited by the still-running sender
        path = directory / 'interval_dispatch.json'
        # Malformed state fails closed instead of starting another sender.
        state = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(state, dict) or (state and
                (state.get('account') != account or state.get('flow') != flow)):
            raise ValueError('interval state account mismatch')
        reconcile_receipt_deadline(directory, path, state, account=account, flow=flow)
        due = float(state.get('next_due_ts', 0))
        if not math.isfinite(due) or due < 0:
            raise ValueError('invalid interval deadline')
        if due > now and label not in retry_labels:
            return
        wrapper = project_root / 'scripts' / f'send_{flow}_{account}.sh'
        if not wrapper.is_file() or wrapper.is_symlink():
            raise ValueError('interval sender wrapper missing or symlinked')
        previous = dict(state)
        state.update(account=account, flow=flow, scheduler=DISPATCHER_LABEL,
                     last_start_ts=now, next_due_ts=(now + entry['interval_dispatch_seconds']
                                                    if due <= now else due))
        persist_json_atomic(path, state)
        try:
            with (directory / 'send.launchd.log').open('ab', buffering=0) as log:
                child = spawn(['/bin/bash', str(wrapper)], cwd=str(project_root),
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True, pass_fds=(lock.fileno(),))
            state['pid'] = child.pid
            persist_json_atomic(path, state)
        except Exception:
            persist_json_atomic(path, previous)
            raise
        # Do not LOCK_UN: parent close leaves the inherited lock held by child.
