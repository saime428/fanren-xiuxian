"""Immutable, redacted per-entry snapshots beside each account's live journal."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def archive_battle(state_file: Path, payload: dict) -> Path:
    account = payload['account']
    message_id = payload['entry_message_id']
    if state_file.name != 'world_boss_state.json' or state_file.parent.name != account:
        raise ValueError('diagnostic_account_path_mismatch')
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise ValueError('diagnostic_entry_invalid')
    content = (json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()
    folder = state_file.parent / 'battle_diagnostics'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{message_id}.json'
    if target.exists():
        if target.read_bytes() == content:
            return target
        # A later reconciliation or another chat's same numeric message ID
        # gets its own revision; the first observation is never overwritten.
        target = folder / f'{message_id}-{hashlib.sha256(content).hexdigest()[:16]}.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ValueError('diagnostic_archive_collision')
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
