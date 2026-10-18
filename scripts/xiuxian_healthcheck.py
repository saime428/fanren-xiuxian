#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from xiuxian_runtime_paths import PROJECT_ROOT as XIUXIAN_ROOT, STATE_ROOT as RUNTIME_STATE_ROOT, WORKSPACE_ROOT as WORKSPACE
except ModuleNotFoundError:
    # Support importlib-based tests that load this file from the project root.
    from scripts.xiuxian_runtime_paths import PROJECT_ROOT as XIUXIAN_ROOT, STATE_ROOT as RUNTIME_STATE_ROOT, WORKSPACE_ROOT as WORKSPACE

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qyz_state import (FAILED_EVENT_STATUSES, RUNTIME_VERSION as QYZ_RUNTIME_VERSION,
                       SOURCE_CHAT_TOPICS as QYZ_SOURCE_TOPICS, event_needs_reconciliation, record_updated_ts,
                       server_damage_multiplier_min)
from flow_registry import active_entries, is_safe_component, registry_path
from command_confirmation import CANONICAL_CHAT_ID, CANONICAL_TARGET, CANONICAL_TOPIC_ID, valid_receipt_identity
from miniapp_shared.accounts import GAME_SECOND_CHAT_ID
from state_store import persist_json_atomic
from nanlonghou_trade import TRADE_FLOW_COMMANDS, TRADE_MAX_AGE_SECONDS
from event_monitor_runtime import (OBSERVER_FLOWS, SEND_FLOWS, QUESTION_FLOWS, question_monitor_version,
                                   RUNTIME_VERSION as EVENT_MONITOR_VERSION, health_filename)

STATE_ROOT = RUNTIME_STATE_ROOT / 'accounts'
CONFIG_PATH = registry_path(XIUXIAN_ROOT)
OUTPUT_PATH = Path(os.environ.get('XIU_HEALTHCHECK_OUTPUT', str(WORKSPACE / 'state' / 'xiuxian-healthcheck.json')))

PROCESS_PATTERNS = {
    'return_observer': re.compile(r'(observe_return\.py|recipe_notice_monitor\.py|run_.*return_observer)'),
    'send': re.compile(r'(send_[^\s]+\.sh|send_topic_message\.py|treasure_hunt\.run_cycle|tianxing_exploration\.run_cycle)'),
    'watchdog': re.compile(r'(watch_[^\s]+\.sh|watch_send_health\.py)'),
}

GOOD_STATUSES = {'acknowledged', 'cooldown_pending', 'countdown_pending'}
WARN_STATUSES = {'resent_pending_ack', 'failure_resent_pending_ack', 'sent_no_ack'}
BAD_LOG_PATTERNS = (
    'FAIL ',
    'ModuleNotFoundError',
    'Traceback',
    'EOFError',
    'Permission denied',
    'No such file or directory',
    'not authorized',
    'Target session is not authorized',
    'Event monitor failed:',
    'Auto-reply reconciliation failed:',
    'Observer reconciliation failed:',
    'send_topic_message.py exceeded TG_RUN_MAX_SECONDS=',
)
GOOD_LOG_PATTERNS = (
    'No new return-summary event found',
    'Disabled flow registry skip:',
    '] OK ',
    'Entry message',
    ' is expired:',
    'Session already authorized',
    'Status probe cache says',
    'Status probe detected',
    'Status probe shows',
    'Previous success was',
    'Recent same-flow activity detected',
    'Cooldown retry pending',
    '炼焰冷却中',
    'Accepted ack reply',
    'Immediate resend completed',
    'deep retreat still running',
    '【探寻成功】',
    '【探尋成功】',
    '满载而归',
    '滿載而歸',
    '意外之喜',
    '召唤成功，镇压成功',
    '召喚成功，鎮壓成功',
    '魔影被你成功击溃',
    '魔影被你成功擊潰',
    'shuangxiu_wenyang cooldown remaining=',
    'Event monitor verified:',
)
NOISY_BUT_TOLERABLE_PATTERNS = (
    'Broken pipe',
    'Connection reset by peer',
    'HTTP code 429',
    '0 bytes read on a total of 8 expected bytes',
    '0 bytes read on a total of 4 expected bytes',
)

MAX_LOG_FILES_PER_FLOW = 5
TAIL_BLOCK_SIZE = 8192
MAX_TAIL_BYTES = 256 * 1024
MAX_STATE_JSON_BYTES = 1024 * 1024
# The QYZ primary writer keeps 20 complete battle journals, including the
# per-window server receipts and transport diagnostics. Bound that larger
# format separately; ordinary flow state keeps the smaller read budget.
MAX_QYZ_STATE_JSON_BYTES = 8 * 1024 * 1024
MAX_CONFIG_JSON_BYTES = 1024 * 1024
O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', None)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
STRUCTURED_STATE_MAX_AGE_SECONDS = 12 * 60 * 60


@dataclass
class FlowHealth:
    account: str
    flow: str
    kind: str
    status: str
    severity: str
    note: str
    active: bool
    ignored_legacy: bool
    last_attempt_status: str | None
    last_attempt_age_seconds: int | None
    last_success_status: str | None
    last_success_age_seconds: int | None
    cooldown_until_ts: float | None
    process_running: bool
    recent_log_error: str | None


def load_json(path: Path) -> dict[str, Any] | None:
    payload, state = load_json_state(path)
    return payload if state == 'valid' else None


def load_json_state(path: Path, *, max_bytes: int = MAX_STATE_JSON_BYTES) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        return {}, 'malformed'
    if not path.exists():
        return {}, 'missing'
    if O_NOFOLLOW is None:
        return {}, 'unsupported'
    fd: int | None = None
    try:
        flags = os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            os.close(fd)
            fd = None
            return {}, 'malformed'
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return {}, 'malformed'
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        if fd is not None:
            os.close(fd)
        return {}, 'malformed'
    if not isinstance(payload, dict):
        return {}, 'malformed'
    return payload, 'valid'


def path_has_symlink_from_trusted_root(path: Path, trusted_root: Path) -> bool:
    try:
        relative = path.absolute().relative_to(trusted_root.absolute())
    except ValueError:
        # Unit/injected roots may live outside the production project root.
        # Their own leaf/containment checks still apply; the project-boundary
        # ancestor guard is meaningful only for paths lexically under it.
        return False
    current = trusted_root.absolute()
    if current.is_symlink():
        return True
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def load_active_config() -> tuple[list[dict[str, Any]], str | None]:
    if path_has_symlink_from_trusted_root(CONFIG_PATH, XIUXIAN_ROOT):
        return [], f'active config path rejected: {CONFIG_PATH}'
    payload, state = load_json_state(CONFIG_PATH, max_bytes=MAX_CONFIG_JSON_BYTES)
    if state != 'valid':
        return [], f'active config unavailable: {state}: {CONFIG_PATH}'
    try:
        return active_entries(payload), None
    except ValueError as exc:
        return [], f'active config invalid: {exc}'


def safe_float(value: Any) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None or not math.isfinite(parsed) or abs(parsed) > 10_000_000_000:
        return None
    return parsed


def safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def age_seconds(ts: float | int | None) -> int | None:
    parsed = safe_float(ts)
    if parsed is None or parsed > time.time() + 60:
        return None
    return max(0, int(time.time() - parsed))


def tail_lines(path: Path, line_count: int = 80) -> list[str]:
    if not path.exists() or path.is_symlink():
        return []
    if O_NOFOLLOW is None:
        return []
    fd: int | None = None
    try:
        flags = os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            fd = None
            return []
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            bytes_read = 0
            while position > 0 and newline_count <= line_count and bytes_read < MAX_TAIL_BYTES:
                read_size = min(TAIL_BLOCK_SIZE, position, MAX_TAIL_BYTES - bytes_read)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                bytes_read += len(chunk)
                newline_count += chunk.count(b'\n')
        text = b''.join(reversed(chunks)).decode('utf-8', errors='ignore')
        return text.splitlines()[-line_count:]
    except Exception:
        if fd is not None:
            os.close(fd)
        return []


def file_age_seconds(path: Path) -> int | None:
    if not path.exists() or path.is_symlink():
        return None
    try:
        mtime = path.stat().st_mtime
        if mtime > time.time():
            return None
        return max(0, int(time.time() - mtime))
    except Exception:
        return None


def log_name_matches_kind(path: Path, kind: str) -> bool:
    name = path.name
    if kind == 'return_observer':
        return name == 'observer.launchd.log'
    if kind == 'watchdog':
        return 'watchdog' in name
    if kind == 'send':
        return name.startswith('all-run-') or name in {'send.log', 'send.launchd.log'}
    return True


def flow_state_dir(account: str, flow: str) -> Path:
    if account == 'aggregate':
        return STATE_ROOT.parent / flow
    return STATE_ROOT / account / flow


def find_recent_log_signal(flow_dir: Path, kind: str | None = None) -> tuple[str | None, str | None]:
    candidates_with_mtime: list[tuple[float, Path]] = []
    now = time.time()
    for path in flow_dir.glob('*.log'):
        try:
            if path.is_symlink():
                continue
            if kind is not None and not log_name_matches_kind(path, kind):
                continue
            mtime = path.stat().st_mtime
            if mtime > now:
                continue
            candidates_with_mtime.append((mtime, path))
        except OSError:
            continue
    candidates = [
        path
        for _, path in sorted(candidates_with_mtime, key=lambda item: item[0], reverse=True)[
            :MAX_LOG_FILES_PER_FLOW
        ]
    ]

    for path in candidates:
        lines = tail_lines(path, 80)
        # A log may retain an old traceback below a newer successful/idle line.
        # Classify the newest recognized terminal signal, not "any error in the
        # tail wins forever". This keeps real latest tracebacks bad while allowing
        # a later recovery marker to clear transient transport failures.
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            terminal_error = re.match(r'^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*(?:Error|Exception))(?::|$)', text)
            if terminal_error:
                # Report the exception type instead of the traceback heading;
                # never include request/credential values from exception text.
                return 'bad', terminal_error.group(1)
            if kind == 'send' and text == 'SystemExit: 75':
                # Historical hard-wait exits mean unconfirmed delivery. Their
                # structured pending state remains a warning, not a traceback.
                return None, None
            if any(marker in text for marker in BAD_LOG_PATTERNS):
                # Launchd may retain historical pre-fix permission errors in a
                # log after the wrapper has been corrected.  A current running
                # process or a newer successful/idle marker is authoritative;
                # stale historical lines must not keep a recovered flow red.
                if 'Permission denied' in text:
                    continue
                return 'bad', text
            if any(marker in text for marker in GOOD_LOG_PATTERNS):
                return None, None
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            if any(marker in text for marker in NOISY_BUT_TOLERABLE_PATTERNS):
                return 'warn', text
    return None, None


def has_recent_watchdog_ok_signal(flow_dir: Path) -> bool:
    for path in (flow_dir / 'watchdog.launchd.log', flow_dir / 'watchdog.log', flow_dir / 'observer.watchdog.launchd.log'):
        age = file_age_seconds(path)
        if age is None or age > 3600:
            continue
        for line in reversed(tail_lines(path, 80)):
            text = line.strip()
            if not text:
                continue
            if any(marker in text for marker in BAD_LOG_PATTERNS):
                return False
            if (
                'Healthy waiting state:' in text
                or 'watchdog is only enforcing stale-run health' in text
                or 'Watchdog lease check complete; business confirmation is owned by the event monitor' in text
            ):
                return True
    return False


def run_cached(cache: dict[tuple[Path, str], Any], flow_dir: Path, key: str, load: Callable[[], Any]) -> Any:
    """Read each flow-directory input once per healthcheck run, whatever kinds ask."""
    if (flow_dir, key) not in cache:
        cache[(flow_dir, key)] = load()
    return cache[(flow_dir, key)]


def cached_log_signal(
    flow_dir: Path,
    kind: str,
    cache: dict[tuple[Path, str], Any],
) -> tuple[str | None, str | None]:
    return run_cached(cache, flow_dir, kind, lambda: find_recent_log_signal(flow_dir, kind))


def process_snapshot() -> str:
    try:
        return subprocess.check_output(['ps', 'aux'], text=True)
    except Exception:
        return ''


def launchctl_snapshot(label: str) -> str:
    try:
        return subprocess.check_output(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ''


def flow_has_process(proc_text: str, account: str, flow: str, kind: str) -> bool:
    pattern = PROCESS_PATTERNS.get(kind)
    if not pattern:
        return False
    flow_dir_hint = f'state/accounts/{account}/{flow}'
    account_token = re.compile(rf'(?<![A-Za-z0-9_-]){re.escape(account)}(?![A-Za-z0-9_-])')
    flow_token = re.compile(rf'(?<![A-Za-z0-9_-]){re.escape(flow)}(?![A-Za-z0-9_-])')
    for line in proc_text.splitlines():
        if kind == 'return_observer':
            if (
                ('observe_return.py' in line or 'recipe_notice_monitor.py' in line)
                and (flow_dir_hint in line or (account_token.search(line) and flow_token.search(line)))
            ):
                return True
        if flow_dir_hint not in line and not (account_token.search(line) and flow_token.search(line)):
            continue
        if pattern.search(line):
            return True
    return False


QYZ_MISSED_EVENT_WINDOW_SECONDS = 30 * 60
QYZ_MISSED_EVENT_MAX_AGE_SECONDS = 24 * 3600


def qyz_missed_event_board(events: list[Any]) -> int | None:
    """Message id of an official settlement board no journal record belongs to."""
    path = XIUXIAN_ROOT / 'qingyuanzi-new-source' / 'state' / 'world_boss_shared_board.json'
    if path_has_symlink_from_trusted_root(path, XIUXIAN_ROOT):
        return None
    board, state = load_json_state(path, max_bytes=MAX_STATE_JSON_BYTES)
    if state != 'valid' or not isinstance(board.get('rows'), list) or not board['rows']:
        return None
    published = board.get('published_at')
    try:
        board_ts = datetime.fromisoformat(str(published)).timestamp() if published else None
    except ValueError:
        board_ts = None
    if board_ts is None or not 0 <= time.time() - board_ts <= QYZ_MISSED_EVENT_MAX_AGE_SECONDS:
        return None
    for event in events:
        updated = record_updated_ts(event)
        if updated is not None and -QYZ_MISSED_EVENT_WINDOW_SECONDS <= board_ts - updated <= QYZ_MISSED_EVENT_WINDOW_SECONDS:
            return None
    return safe_int(board.get('message_id'))


def classify_qyz(account: str, flow: str, proc_text: str, active: bool) -> FlowHealth:
    """Read the host-owned account state; process liveness alone cannot pass."""
    path = XIUXIAN_ROOT / 'qingyuanzi-new-source' / 'state' / 'accounts' / account / 'world_boss_state.json'
    if (not is_safe_component(account) or flow != 'qyz_miniapp'
            or path_has_symlink_from_trusted_root(path, XIUXIAN_ROOT)):
        payload, state = {}, 'invalid_path'
    else:
        payload, state = load_json_state(path, max_bytes=MAX_QYZ_STATE_JSON_BYTES)
    health = payload.get('world_boss_health')
    health = health if isinstance(health, dict) else {}
    events = payload.get('world_boss_events', [])
    invalid_events = not isinstance(events, list) or any(
        not isinstance(event, dict) or not isinstance(event.get('status'), str)
        or not re.fullmatch(r'[a-z0-9_-]{1,80}', event['status'])
        for event in events
    )
    events = events if isinstance(events, list) else []
    latest_event = events[-1] if events and isinstance(events[-1], dict) else {}
    latest_event_status = latest_event.get('status')
    latest_event_status = (latest_event_status if isinstance(latest_event_status, str)
                           and re.fullmatch(r'[a-z0-9_-]{1,80}', latest_event_status) else None)
    latest_event_error = latest_event.get('error')
    latest_event_error = (latest_event_error if isinstance(latest_event_error, str)
                          and re.fullmatch(r'[a-z0-9_-]{1,80}', latest_event_error) else None)
    age = age_seconds(health.get('ts'))
    pid = safe_int(health.get('pid'))
    running = any(len(parts := line.split()) > 2 and parts[1] == str(pid)
                  and 'qingyuanzi_host.py' in line for line in proc_text.splitlines()) if pid else False
    raw_status = health.get('status')
    status = raw_status if isinstance(raw_status, str) and re.fullmatch(r'[a-z0-9_-]{1,80}', raw_status) else 'unknown'
    reason = None
    if state != 'valid' or not health:
        reason = 'qyz_health_missing_or_invalid'
    elif health.get('account') != account or health.get('flow') != flow:
        reason = 'qyz_identity_mismatch'
    elif health.get('execution_owner') != 'official_protocol' or health.get('runtime_version') != QYZ_RUNTIME_VERSION:
        reason = 'qyz_runtime_mismatch'
    elif age is None or age > 150:
        reason = 'qyz_health_stale'
    elif not running or health.get('monitor_active') is not True or health.get('connected') is not True:
        reason = 'qyz_monitor_inactive'
    elif set(health.get('monitored_chat_ids') or []) != set(QYZ_SOURCE_TOPICS):
        reason = 'qyz_source_monitor_incomplete'
    elif any(health.get(key) is not True for key in ('identity_verified', 'membership_verified', 'source_verified')):
        reason = 'qyz_source_or_identity_unverified'
    elif status != 'battle_in_progress' and any((age_seconds(health.get(key)) is None or age_seconds(health.get(key)) > 240)
             for key in ('source_checked_at', 'policy_checked_at')):
        reason = 'qyz_preflight_stale'
    elif (not isinstance(health.get('verification_required'), bool)
          or health.get('verification_available') is not True
          or (health.get('verification_required') and health.get('verification_method') != 'reference_native_browser')):
        reason = 'qyz_official_verification_unavailable'
    elif invalid_events:
        reason = 'qyz_event_journal_invalid'
    elif (missed_board := qyz_missed_event_board(events)) is not None:
        # The host captured an official settlement newer than any journal
        # record: an entry was published and nobody was admitted (2026-09-21).
        reason = f'qyz_event_missed: board={missed_board}'
    elif (multiplier := server_damage_multiplier_min(latest_event)) is not None and multiplier < 1:
        reason = f'qyz_server_damage_reduced: min_multiplier={multiplier:g}'
    elif status == 'settled_partial' and latest_event_status == 'settled_partial':
        expected = safe_int(latest_event.get('expected_windows')) or 0
        accepted = safe_int(latest_event.get('accepted_hits')) or 0
        perfects = safe_int(latest_event.get('server_perfects')) or 0
        reason = f'qyz_partial_settlement: accepted={accepted}/{expected}, perfect={perfects}/{expected}'
    elif health.get('blockers'):
        reason = (f'qyz_latest_event_error={latest_event_error}'
                  if latest_event_error and latest_event_status in FAILED_EVENT_STATUSES
                  else 'qyz_preflight_blocked')
    elif status not in {'idle_waiting_event', 'battle_in_progress', 'settled_perfect'}:
        reason = 'qyz_business_not_confirmed'
    elif status == 'settled_perfect' and (
        health.get('finish_confirmed') is not True
        or health.get('missed_windows') != 0
        or health.get('local_missed_windows') != 0
        or health.get('local_dead') is not False
        or (safe_int(health.get('expected_windows')) or 0) <= 0
        or health.get('expected_windows') != health.get('accepted_hits')
        or health.get('expected_windows') != health.get('server_perfects')
    ):
        reason = 'qyz_settlement_evidence_incomplete'
    if reason is None and events:
        # Only this process's active event may be unfinished without an alarm.
        # An older unresolved event survives later heartbeats and settlements.
        active_fingerprint = health.get('event_fingerprint') if status == 'battle_in_progress' else None
        if any(event_needs_reconciliation(event) and (
            not active_fingerprint or event.get('fingerprint') != active_fingerprint
            or event.get('status') in FAILED_EVENT_STATUSES
        ) for event in events):
            reason = 'qyz_unfinished_mutation'
        elif latest_event_status in FAILED_EVENT_STATUSES:
            reason = 'qyz_latest_event_unresolved'
    return FlowHealth(account, flow, 'qyz_monitor', status if health else (reason or 'unknown'),
                      'bad' if reason else 'ok', reason or 'fresh account-bound QYZ evidence',
                      active, not active, status, age, None, None, None, running, None)


def confirmed_wenyang_receipt(account: str, attempt: dict[str, Any], success: dict[str, Any]) -> bool:
    """Accept this producer's success only with its matching, attributed receipt."""
    fields = ('status', 'ts', 'initiator_account', 'initiator_username', 'partner_account',
              'partner_username', 'target', 'topic_id', 'anchor_message_id', 'command_message_id',
              'reply_message_id', 'reply_sender_id', 'reply_to_msg_id', 'reply_text', 'next_due_ts')
    if (attempt.get('status') != 'success'
            or any(attempt.get(key) is None or attempt.get(key) != success.get(key) for key in fields)
            or attempt.get('initiator_account') != account
            or not re.fullmatch(r'tg[0-9]+', str(attempt.get('partner_account') or ''))
            or attempt.get('partner_account') == account
            or not isinstance(attempt.get('reply_text'), str) or not attempt['reply_text'].strip()):
        return False
    return valid_wenyang_business_reply(account, attempt, 'success')


def valid_wenyang_business_reply(account: str, attempt: dict[str, Any], expected_status: str) -> bool:
    from shuangxiu_wenyang_reply import (INITIATOR_ACCOUNT, INITIATOR_USERNAME, PARTNER_ACCOUNT,
                                        PARTNER_USERNAME, TRUSTED_REPLY_SENDER_IDS, classify_partner_reply)
    if (account != INITIATOR_ACCOUNT or attempt.get('initiator_account') != INITIATOR_ACCOUNT
            or attempt.get('initiator_username') != INITIATOR_USERNAME
            or attempt.get('partner_account') != PARTNER_ACCOUNT
            or attempt.get('partner_username') != PARTNER_USERNAME
            or attempt.get('target') not in {CANONICAL_TARGET, str(CANONICAL_CHAT_ID)}
            or safe_int(attempt.get('topic_id')) != CANONICAL_TOPIC_ID
            or attempt.get('status') != expected_status
            or classify_partner_reply(str(attempt.get('reply_text') or '')) != expected_status):
        return False
    anchor = safe_int(attempt.get('anchor_message_id')) or 0
    command = safe_int(attempt.get('command_message_id')) or 0
    reply = safe_int(attempt.get('reply_message_id')) or 0
    ts = safe_float(attempt.get('ts'))
    due = safe_float(attempt.get('next_due_ts'))
    return (0 < anchor < command < reply
            and safe_int(attempt.get('reply_to_msg_id')) == command
            and safe_int(attempt.get('reply_sender_id')) in TRUSTED_REPLY_SENDER_IDS
            and ts is not None and ts > 0 and due is not None and due >= ts + 3600)


def is_persistent_event_monitor(flow: str, kind: str) -> bool:
    return ((kind == 'send' and flow in SEND_FLOWS)
            or (kind == 'return_observer' and (flow in OBSERVER_FLOWS or flow == 'recipe_notice')))


def classify_event_monitor(account: str, flow: str, kind: str, flow_dir: Path,
                           proc_text: str, active: bool,
                           log_signal_cache: dict) -> FlowHealth:
    payload, file_state = load_json_state(flow_dir / health_filename(kind))
    status, severity, note = 'monitor_health_missing', 'bad', 'no verified monitor state'
    pid = safe_int(payload.get('pid'))
    script = ('recipe_notice_monitor.py' if flow == 'recipe_notice'
              else 'auto_reply_topic_event.py' if kind == 'send' else 'observe_return.py')
    running = bool(pid) and any(len(parts := line.split()) > 2 and parts[1] == str(pid)
                               and script in line for line in proc_text.splitlines())
    success_ts = safe_float(payload.get('last_success_ts'))
    success_age = age_seconds(success_ts)
    verified_age = age_seconds(payload.get('listener_verified_ts'))
    state_age = age_seconds(payload.get('ts'))
    started_age = age_seconds(payload.get('started_at_ts'))
    reconcile = safe_float(payload.get('reconcile_seconds')) or 0
    max_age = max(180, min(300, 3 * reconcile))
    targets = payload.get('source_targets')
    expected_targets = ({str(CANONICAL_CHAT_ID)} if flow == 'recipe_notice'
                        else {str(CANONICAL_CHAT_ID), str(GAME_SECOND_CHAT_ID)})
    valid_targets = (isinstance(targets, list) and all(isinstance(x, str) for x in targets)
                     and {x.replace(CANONICAL_TARGET, str(CANONICAL_CHAT_ID)) for x in targets}
                     == expected_targets)
    raw_status = payload.get('status')
    log_severity, log_signal = cached_log_signal(flow_dir, kind, log_signal_cache)
    log_path = flow_dir / ('send.launchd.log' if kind == 'send' else 'observer.launchd.log')
    log_age = file_age_seconds(log_path)
    try:
        log_mtime = log_path.stat().st_mtime if log_age is not None else None
    except OSError:
        log_mtime = None

    if file_state != 'valid':
        status = 'monitor_health_missing' if file_state == 'missing' else 'monitor_health_invalid'
    elif (payload.get('account') != account or payload.get('flow') != flow
          or payload.get('kind') != kind or payload.get('runtime_version') != EVENT_MONITOR_VERSION
          or not valid_targets or not 5 <= reconcile <= 3600
          or any((value := safe_float(payload.get(key))) is not None and value > time.time()
                 for key in ('ts', 'started_at_ts', 'last_success_ts', 'listener_verified_ts'))):
        status, note = 'monitor_health_mismatch', 'monitor identity, sources or runtime mismatch'
    elif not running:
        status, note = 'monitor_not_running', 'verified monitor PID is no longer running'
    elif state_age is None or state_age > max_age:
        status, note = 'monitor_health_stale', 'monitor has stopped reporting its scan outcome'
    elif flow in QUESTION_FLOWS and payload.get('question_monitor_version') != question_monitor_version(flow):
        status, note = 'monitor_question_handler_outdated', 'account-bound question push handler has not been loaded'
    elif flow in TRADE_FLOW_COMMANDS and payload.get('trade_policy') != {
            'reply_text': TRADE_FLOW_COMMANDS[flow], 'max_event_age_seconds': TRADE_MAX_AGE_SECONDS,
            'official_receipt_required': True}:
        status, note = 'monitor_trade_policy_mismatch', 'trade choice, deadline or official receipt requirement differs from policy'
    elif flow == 'xuangu_zuoda' and payload.get('answer_policy') != {
            'question_bound_answers': True, 'max_event_age_seconds': 300,
            'official_receipt_required': True, 'reply_to_original_question': True}:
        status, note = 'monitor_answer_policy_mismatch', 'question mapping, deadline or official answer confirmation differs from policy'
    elif payload.get('business_issue'):
        issue = payload['business_issue']
        code = issue.get('code') if isinstance(issue, dict) else None
        message_id = safe_int(issue.get('message_id')) if isinstance(issue, dict) else None
        status = 'monitor_question_unresolved'
        note = (f'{code}: message_id={message_id}' if isinstance(code, str)
                and re.fullmatch(r'[a-z_]{1,60}', code) and message_id and message_id > 0
                else 'invalid question failure metadata')
    elif raw_status == 'failed' or payload.get('error_type'):
        error = str(payload.get('error_type') or 'unknown')
        stage = str(payload.get('error_stage') or 'unknown')
        status = 'monitor_failed'
        note = f'{stage}: {error}' if re.fullmatch(r'[A-Za-z0-9_]{1,80}', error) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', stage) else 'invalid monitor failure metadata'
    elif (raw_status in {'starting', 'checking'} and success_ts is None
          and started_age is not None and started_age <= max_age):
        status, severity, note = 'monitor_starting', 'warn', 'waiting for first verified source scan'
    elif (raw_status not in {'healthy', 'checking'} or success_age is None or success_age > max_age
          or verified_age is None or verified_age > max_age
          or payload.get('listener_connected') is not True):
        status, note = 'monitor_recovery_unverified', 'listener and required source scan have not both recovered'
    elif (log_severity == 'bad' and log_mtime is not None and success_ts is not None
          and log_mtime > success_ts):
        status, note = 'monitor_new_log_error', log_signal or 'new error after verified scan'
    elif payload.get('pending_command'):
        pending = payload['pending_command']
        retry_at = safe_float(pending.get('retry_at_ts')) if isinstance(pending, dict) else None
        overdue = retry_at is None or time.time() > retry_at + 300
        status, severity = ('command_retry_stalled', 'bad') if overdue else ('awaiting_official_reply', 'warn')
        note = 'command remains unconfirmed; durable retry overdue' if overdue else 'command remains unconfirmed; retry scheduled'
    else:
        status, severity, note = 'monitor_healthy', 'ok', 'account-bound listener and required source scan verified'
    return FlowHealth(account, flow, kind, status, severity, note, active, not active,
                      None, None, None, success_age, None, bool(running),
                      log_signal if severity == 'bad' else None)


def classify_kind(
    account: str,
    flow: str,
    kind: str,
    proc_text: str,
    active: bool,
    log_signal_cache: dict[tuple[Path, str], Any],
    require_monitor_health: bool = False,
    command_transport: str = "telegram",
) -> FlowHealth:
    if kind == 'qyz_monitor':
        return classify_qyz(account, flow, proc_text, active)
    if command_transport == "miniapp" and flow == "shenji_budao" and kind == "watchdog":
        # Event-driven work can be healthy before the next event exists. Use
        # the actual listener/scan/issue evidence, not a fabricated API result.
        watched = classify_kind(account, flow, "send", proc_text, active, log_signal_cache,
                                require_monitor_health=True, command_transport="miniapp")
        watched.kind = "watchdog"
        return watched
    flow_dir = flow_state_dir(account, flow)
    try:
        if STATE_ROOT.is_symlink() or path_has_symlink_from_trusted_root(STATE_ROOT, XIUXIAN_ROOT) or flow_dir.is_symlink() or flow_dir.parent.is_symlink():
            raise ValueError('symlink state path')
        if account == 'aggregate':
            flow_dir.resolve(strict=False).relative_to(STATE_ROOT.parent.resolve(strict=False))
        else:
            flow_dir.resolve(strict=False).relative_to(STATE_ROOT.resolve(strict=False))
    except (OSError, ValueError):
        return FlowHealth(account, flow, kind, 'invalid_state_path', 'bad',
                          'state path rejected: symlink or outside STATE_ROOT',
                          active, not active, None, None, None, None, None, False, None)
    if flow == 'treasure_hunt' and kind == 'send':
        from treasure_hunt.health import health as hunt_health
        running = flow_has_process(proc_text, account, flow, kind)
        severity, status, note, due = hunt_health(flow_dir, account, time.time(), running=running)
        return FlowHealth(account, flow, kind, status, severity, note, active, not active,
                          None, None, None, None, due, running, None)
    if flow == 'tianxing_exploration' and kind == 'send':
        from tianxing_exploration.health import health as exploration_health
        running = flow_has_process(proc_text, account, flow, kind)
        severity, status, note, due = exploration_health(flow_dir, account, time.time(), running=running)
        return FlowHealth(account, flow, kind, status, severity, note, active, not active,
                          None, None, None, None, due, running, None)
    if require_monitor_health or (is_persistent_event_monitor(flow, kind)
                                  and (flow_dir / health_filename(kind)).exists()):
        if command_transport == "miniapp" and flow == "shenji_budao" and kind == "send":
            from miniapp_commands.catalog import VERSION
            marker, _ = load_json_state(flow_dir/health_filename(kind))
            if (marker.get("command_transport") != "miniapp"
                    or marker.get("command_transport_version") != VERSION):
                return FlowHealth(account, flow, kind, "miniapp_handler_not_loaded", "bad",
                                  "The event monitor has not loaded the MiniApp route", active, not active,
                                  None, None, None, None, None, False, None)
        return classify_event_monitor(account, flow, kind, flow_dir, proc_text, active, log_signal_cache)
    from miniapp_commands.catalog import STATE_FILE
    if kind in {"send", "watchdog"} and command_transport == "miniapp":
        from miniapp_commands.receipts import health
        severity, status, note, due = health(flow_dir, account, flow, time.time())
        state, _ = load_json_state(flow_dir/STATE_FILE)
        return FlowHealth(account, flow, kind, status, severity, note, active, not active,
                          state.get("status"), age_seconds(state.get("updated_at")),
                          "miniapp_confirmed" if state.get("last_success_at") else None,
                          age_seconds(state.get("last_success_at")), due,
                          flow_has_process(proc_text, account, flow, kind), None)
    # send, watchdog and observer kinds of one flow share these files; read
    # them (and the watchdog log verdict) once per run, not once per kind.
    def state(name: str) -> tuple[dict[str, Any], str]:
        return run_cached(log_signal_cache, flow_dir, name, lambda: load_json_state(flow_dir / name))

    def watchdog_ok() -> bool:
        return run_cached(log_signal_cache, flow_dir, 'watchdog_ok', lambda: has_recent_watchdog_ok_signal(flow_dir))

    last_attempt, attempt_state = state('last_attempt.json')
    last_success, success_state = state('last_success.json')
    last_result, result_state = state('last_result.json')
    status_probe, probe_state = state('status_probe.json')
    command_pending, pending_state = state('command_pending.json')
    if not command_pending.get('pending'):
        probe_pending, probe_pending_state = state('status_probe_pending.json')
        if probe_pending.get('pending'):
            command_pending, pending_state = probe_pending, probe_pending_state
    cooldown_state, cooldown_file_state = state('cooldown_retry.json')
    log_severity, log_signal = cached_log_signal(flow_dir, kind, log_signal_cache)

    cooldown_until_ts = None
    target = last_attempt.get('target')
    topic = safe_int(last_attempt.get('topic_id'))
    message = last_attempt.get('message')
    if (isinstance(target, (str, int)) and not isinstance(target, bool)
            and topic is not None and isinstance(message, str) and message):
        # One flow file may retain older entries for other groups or commands.
        # Read the same scoped key as the sender, never the first dictionary row.
        item = cooldown_state.get(f'{target}|{topic}|{message}')
        if (isinstance(item, dict) and str(item.get('target')) == str(target)
                and safe_int(item.get('topic_id')) == topic and item.get('message') == message):
            cooldown_until_ts = safe_float(item.get('not_before_ts'))

    raw_attempt_status = last_attempt.get('status') if isinstance(last_attempt, dict) else None
    raw_success_status = last_success.get('status') if isinstance(last_success, dict) else None
    malformed_attempt_status = raw_attempt_status is not None and not isinstance(raw_attempt_status, str)
    malformed_success_status = raw_success_status is not None and not isinstance(raw_success_status, str)
    attempt_status = raw_attempt_status if isinstance(raw_attempt_status, str) else None
    success_status = raw_success_status if isinstance(raw_success_status, str) else None
    attempt_age = age_seconds(last_attempt.get('ts')) if isinstance(last_attempt, dict) else None
    success_age = age_seconds(last_success.get('ts')) if isinstance(last_success, dict) else None
    process_running = flow_has_process(proc_text, account, flow, kind)
    observer_state, observer_file_state = state('observer_last_seen.json')
    observer_state_ts = safe_float(observer_state.get('ts')) if isinstance(observer_state, dict) else None
    observer_log_path = flow_dir / 'observer.launchd.log'
    try:
        observer_log_mtime = observer_log_path.stat().st_mtime if not observer_log_path.is_symlink() else None
    except OSError:
        observer_log_mtime = None
    observer_recovered_after_log = (
        kind == 'return_observer'
        and process_running
        and isinstance(observer_state, dict)
        and observer_state.get('status') == 'no_new_event'
        and observer_state_ts is not None
        and observer_state_ts <= time.time()
        and age_seconds(observer_state_ts) is not None
        and age_seconds(observer_state_ts) <= 3600
        and (observer_log_mtime is None or observer_state_ts >= observer_log_mtime)
    )

    severity = 'ok'
    status = 'healthy'
    note = 'normal'

    active_not_before_ts = None
    if isinstance(last_attempt, dict):
        active_not_before_ts = safe_float(last_attempt.get('not_before_ts'))

    malformed_file_state = (
        attempt_state in {'malformed', 'unsupported'}
        or success_state in {'malformed', 'unsupported'}
        or cooldown_file_state in {'malformed', 'unsupported'}
        or result_state in {'malformed', 'unsupported'}
        or pending_state in {'malformed', 'unsupported'}
        or (flow in OBSERVER_FLOWS and probe_state in {'malformed', 'unsupported'})
        or (kind == 'return_observer' and observer_file_state in {'malformed', 'unsupported'})
    )

    if kind == 'return_observer' and process_running:
        status = 'running'
        note = 'observer active'
    elif malformed_file_state or malformed_attempt_status or malformed_success_status:
        severity = 'warn'
        status = 'malformed_state'
        note = 'structured state is malformed or unreadable'
    elif command_pending.get('pending') and kind in {'send', 'watchdog'}:
        identity_ok = (command_pending.get('flow_slug') == flow
                       and safe_int(command_pending.get('actor_id')) == safe_int(account.removeprefix('tg'))
                       and str(command_pending.get('target')) in {CANONICAL_TARGET, str(CANONICAL_CHAT_ID)}
                       and safe_int(command_pending.get('topic_id')) == CANONICAL_TOPIC_ID)
        retry_at = safe_float(command_pending.get('next_retry_ts'))
        if not identity_ok:
            status, severity, note = 'pending_identity_mismatch', 'bad', 'unconfirmed command does not match account/flow/topic'
        elif retry_at is None or time.time() > retry_at + 300:
            status, severity, note = 'command_retry_stalled', 'bad', 'unconfirmed command retry is overdue'
        else:
            status, severity, note = 'awaiting_official_reply', 'warn', 'unconfirmed command retained; retry scheduled'
    elif kind == 'return_observer':
        observer_status = observer_state.get('status') if isinstance(observer_state, dict) else None
        observer_state_age = age_seconds(observer_state_ts)
        label = f'ai.hermes.xiuxian.{account}.{flow}.return_observer'
        launchd_text = launchctl_snapshot(label)
        launchd_loaded = bool(launchd_text)
        launchd_running = 'state = running' in launchd_text
        launchd_scheduled = 'state = spawn scheduled' in launchd_text
        observer_recent = (observer_state_age is not None and observer_state_age <= 3600
                           and observer_state_ts is not None and observer_state_ts <= time.time() + 60)
        observer_no_event_ok = isinstance(observer_state, dict) and observer_state.get('status') == 'no_new_event'

        if launchd_running:
            status = 'running'
            note = 'observer active'
        elif launchd_loaded and launchd_scheduled and observer_recent and observer_no_event_ok and log_severity != 'bad':
            status = 'scheduled_idle'
            note = 'observer launchd scheduled with recent no-new-event state'
        elif launchd_loaded and observer_recent and observer_status == 'no_new_event' and log_severity != 'bad':
            status = 'idle_recent'
            note = 'observer loaded with recent observer state'
        else:
            severity = 'warn'
            status = 'not_running'
            note = 'observer process not seen and no recent healthy observer state'
    elif (flow == 'shuangxiu_wenyang' and kind == 'send'
          and attempt_age is not None and attempt_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and confirmed_wenyang_receipt(account, last_attempt, last_success)):
        status = 'success'
        note = 'confirmed account-attributed 温养双修 settlement'
        next_due_ts = safe_float(last_attempt.get('next_due_ts'))
        if next_due_ts is not None and next_due_ts > time.time():
            cooldown_until_ts = next_due_ts
    elif (flow == 'shuangxiu_wenyang' and kind == 'send' and attempt_status == 'cooldown'
          and attempt_age is not None and attempt_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and valid_wenyang_business_reply(account, last_attempt, 'cooldown')
          and (safe_float(last_attempt.get('next_due_ts')) or 0) > time.time()):
        status, severity, note = 'cooldown', 'ok', 'account-attributed server cooldown; next attempt scheduled'
        cooldown_until_ts = safe_float(last_attempt.get('next_due_ts'))
    elif (flow == 'shuangxiu_wenyang' and kind == 'send' and attempt_status in {
            'anchor_send_submitted', 'anchor_send_unknown', 'anchor_sent', 'command_send_submitted',
            'command_send_unknown', 'command_sent_waiting_reply', 'sent_unconfirmed_assume_consumed', 'reply_retry_pending'}):
        retry_at = safe_float(last_attempt.get('retry_not_before_ts'))
        in_progress = (retry_at is not None and time.time() <= retry_at + 300)
        status = 'wenyang_waiting_receipt' if in_progress else 'wenyang_outcome_unknown'
        severity = 'warn' if in_progress else 'bad'
        note = 'waiting for an account-attributed business receipt; retry on the same anchor after reconciliation'
    elif (valid_receipt_identity(last_result, actor_id=safe_int(account.removeprefix('tg')), flow=flow)
          and not last_result.get('failed')
          and (safe_float(last_result.get('next_due_ts')) or safe_float(last_result.get('not_before_ts')) or 0) > time.time()):
        status, severity, note = 'verified_result_waiting', 'ok', 'official business result establishes the next due time'
        cooldown_until_ts = safe_float(last_result.get('next_due_ts')) or safe_float(last_result.get('not_before_ts'))
    elif (flow in OBSERVER_FLOWS and kind in {'send', 'watchdog'}
          and isinstance(status_probe.get('status'), str)
          and status_probe.get('status') in {'countdown_pending', 'already_in_retreat', 'cached_countdown_pending', 'active_without_countdown'}
          and valid_receipt_identity(status_probe, actor_id=safe_int(account.removeprefix('tg')), flow=flow)
          and status_probe.get('message') in ({'.查看闭关', '.深度闭关'} if flow == 'shendubiguan' else {'.元婴状态'})
          and not status_probe.get('failed')
          and (probe_age := age_seconds(status_probe.get('ts'))) is not None
          and probe_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and (safe_float(status_probe.get('ts')) or 0) >= (safe_float(last_attempt.get('ts')) or 0)
          and ((flow == 'yuanyingchuqiao' and status_probe.get('status') == 'active_without_countdown')
               or (safe_float(status_probe.get('not_before_ts')) or 0) > time.time())):
        status, severity, note = 'verified_status_waiting', 'ok', 'account-matched official status confirms the ongoing task'
        cooldown_until_ts = safe_float(status_probe.get('not_before_ts'))
    elif (attempt_status == 'started' and attempt_age is not None and attempt_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and active_not_before_ts and active_not_before_ts > time.time()
          and valid_receipt_identity(last_attempt, actor_id=safe_int(account.removeprefix('tg')), flow=flow)
          and not last_attempt.get('failed')):
        status = 'started'
        note = f"active cooldown until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(active_not_before_ts))}"
    elif (attempt_status in GOOD_STATUSES and attempt_age is not None and attempt_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and valid_receipt_identity(last_attempt, actor_id=safe_int(account.removeprefix('tg')), flow=flow)
          and not last_attempt.get('failed')):
        status = attempt_status
        note = 'recent acknowledged/cooldown state'
        if active_not_before_ts and active_not_before_ts > time.time():
            note = f"active cooldown until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(active_not_before_ts))}"
    elif attempt_status in WARN_STATUSES:
        severity = 'warn'
        status = attempt_status
        note = 'waiting for ack confirmation'
    elif (attempt_status == 'skip_duplicate_guard' and attempt_age is not None and attempt_age <= STRUCTURED_STATE_MAX_AGE_SECONDS
          and valid_receipt_identity(last_attempt, actor_id=safe_int(account.removeprefix('tg')), flow=flow)
          and not last_attempt.get('failed')):
        status = attempt_status
        severity = 'ok'
        note = 'recent duplicate-guard skip is healthy'
    elif kind == 'watchdog' and flow in SEND_FLOWS and watchdog_ok():
        status, severity, note = 'watchdog_idle', 'ok', 'recent watchdog lease check; event monitor owns business confirmation'
    elif attempt_status:
        severity = 'warn'
        status = attempt_status
        note = 'non-ideal last attempt state'
    elif process_running:
        status = 'running'
        severity = 'ok'
        note = 'process active; structured state not written yet'
    elif kind == 'watchdog' and watchdog_ok():
        status = 'watchdog_idle'
        severity = 'ok'
        note = 'watchdog recently reported healthy idle/stale-run-only state'
    else:
        severity = 'warn'
        status = 'unknown'
        note = 'no process and no structured attempt state'

    if log_severity == 'bad' and not observer_recovered_after_log:
        severity = 'bad'
        note = log_signal or note
    elif log_severity == 'warn' and severity == 'ok' and not observer_recovered_after_log:
        severity = 'warn'
        note = log_signal or note

    if attempt_status == 'cooldown_pending' and cooldown_until_ts:
        note = f'cooldown until {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cooldown_until_ts))}'

    return FlowHealth(
        account=account,
        flow=flow,
        kind=kind,
        status=status,
        severity=severity,
        note=note,
        active=active,
        ignored_legacy=not active,
        last_attempt_status=attempt_status,
        last_attempt_age_seconds=attempt_age,
        last_success_status=success_status,
        last_success_age_seconds=success_age,
        cooldown_until_ts=cooldown_until_ts,
        process_running=process_running,
        recent_log_error=log_signal,
    )


def disabled_launchagent_labels() -> set[str] | None:
    try:
        output = subprocess.check_output(['launchctl', 'print-disabled', f'gui/{os.getuid()}'],
                                         text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return set(re.findall(r'"([^"\n]+)"\s*=>\s*(?:true|disabled)\b', output))


def yuanshen_listener_health() -> tuple[str, str, str]:
    from yuanshen_return import ACCOUNT, INBOX_RELATIVE, LISTENER_STALE_SECONDS, OBSERVER_LABEL, valid_state

    path = XIUXIAN_ROOT / INBOX_RELATIVE
    if path_has_symlink_from_trusted_root(path, XIUXIAN_ROOT):
        return 'return_listener_invalid', 'bad', 'second-soul return inbox path rejected'
    inbox, loaded = load_json_state(path)
    if loaded != 'valid' or not valid_state(inbox) or inbox.get('listener_account') != ACCOUNT:
        return 'return_listener_missing', 'bad', 'second-soul return listener has no account-bound evidence'
    if inbox.get('mode') != 'standalone' or inbox.get('listener_label') != OBSERVER_LABEL:
        return 'return_listener_owner_mismatch', 'bad', 'second-soul return listener is not independently owned'
    try:
        scan_at = float(inbox.get('last_scan_at', 0))
        if (inbox.get('status') != 'healthy' or inbox.get('listener_connected') is not True
                or not math.isfinite(scan_at) or not 0 <= time.time() - scan_at <= LISTENER_STALE_SECONDS):
            return 'return_listener_stale', 'bad', 'second-soul return push/history listener is unhealthy or stale'
    except (TypeError, ValueError):
        return 'return_listener_invalid', 'bad', 'second-soul return listener has malformed scan evidence'
    return 'listening', 'ok', f'independent second-soul listener; last scan {scan_at:.0f}'


def yuanshen_helper_health(label: str) -> tuple[str, str, str]:
    from command_confirmation import OFFICIAL_REPLY_SENDER_IDS
    from yuanshen_automation import (ACCOUNT, ACTOR_ID, STATE_RELATIVE, STATUS_COMMAND, SOUL_STATUSES,
                                    TRAIN_COMMAND, DEMON_COMMAND, PURGE_COMMAND, parse_reply)

    flow = 'train' if label.endswith('.yuanshen-xiulian') else 'purge'
    path = XIUXIAN_ROOT / STATE_RELATIVE
    if path_has_symlink_from_trusted_root(path, XIUXIAN_ROOT):
        return 'invalid_state', 'bad', 'yuanshen state path rejected'
    state, loaded = load_json_state(path)
    if (loaded != 'valid' or state.get('account') != ACCOUNT
            or state.get('actor_id') != ACTOR_ID or state.get('version') != 1):
        return 'invalid_state', 'bad', 'yuanshen state missing, malformed or misattributed'
    try:
        if flow == 'train':
            from yuanshen_return import INBOX_RELATIVE, current_return
            listener = yuanshen_listener_health()
            if listener[1] != 'ok':
                return listener
            inbox, _ = load_json_state(XIUXIAN_ROOT / INBOX_RELATIVE)
            ready_return = current_return(state, inbox, time.time())
            hard_due = max(float(state.get('train_not_before', 0)), float(state.get('transport_not_before', 0)))
            if ready_return and hard_due <= time.time():
                from miniapp_shared.priority import qyz_battle_window
                if not qyz_battle_window(time.time(), XIUXIAN_ROOT)['active']:
                    if inbox.get('last_kick_status') == 'failed':
                        return 'return_dispatch_failed', 'bad', 'second-soul completion saved but sender kick failed'
                    if time.time() - ready_return['at'] > 120:
                        return 'return_dispatch_overdue', 'bad', 'second-soul completion has not resumed within two minutes'
        legacy_commands = {TRAIN_COMMAND, STATUS_COMMAND} if flow == 'train' else {DEMON_COMMAND, PURGE_COMMAND}
        commands = legacy_commands | {STATUS_COMMAND}

        def belongs(row):
            if 'flow' in row:
                return row['flow'] == flow and row.get('command') in commands
            # Before flow attribution, only the training path used 第二元神.
            return row.get('command') in legacy_commands

        outcome = state.get('flow_status', {}).get(flow, {})
        status = outcome.get('status', '')
        if not outcome and state.get('status') in {'failed', 'unconfirmed', 'unknown'}:
            status = state['status']
        good = {'training_started', 'training', 'injured', 'cooldown', 'purged', 'returned',
                'moran_below_threshold', 'purge_cooldown', 'yielding_to_qyz'}
        if status and status not in good:
            return status, 'bad', f'yuanshen {flow}: unresolved outcome {status}'
        pending = state.get('pending') or {}
        if belongs(pending):
            return 'unconfirmed', 'bad', f'yuanshen {flow}: pending official reply'
        attempts = [row for row in state.get('attempts', []) if belongs(row)]
        if not attempts:
            return 'unverified', 'bad', f'yuanshen {flow}: no command evidence'
        latest = attempts[-1]
        receipt = latest.get('reply') or {}
        if (latest.get('status') != 'confirmed' or receipt.get('sender_id') not in OFFICIAL_REPLY_SENDER_IDS
                or int(receipt.get('id', 0)) <= int(latest.get('sent_id', 0))
                or int(latest.get('sent_id', 0)) <= 0):
            return 'unconfirmed', 'bad', f'yuanshen {flow}: no attributed official reply'
        parsed = parse_reply(latest['command'], receipt.get('text', ''))
        valid = {'training_started', 'training', 'injured', 'cooldown'} if flow == 'train' else {
            'demon_panel', 'purged', 'cooldown'}
        if flow == 'purge' and latest['command'] == STATUS_COMMAND and parsed.get('moran') is not None:
            valid |= SOUL_STATUSES
        if parsed['status'] not in valid:
            return parsed['status'], 'bad', f'yuanshen {flow}: official reply needs attention'
        if status == 'returned':
            from yuanshen_return import valid_return
            if flow != 'train' or not valid_return(state.get('last_training_return', {}), time.time()):
                return 'return_unverified', 'bad', 'second-soul completion lacks an attributed official push'
        due = max(float(state.get('due', {}).get(flow, 0)), float(state.get('transport_not_before', 0)),
                  float(state.get('train_not_before', 0)) if flow == 'train' else 0)
        if not math.isfinite(due) or due <= 0:
            return 'invalid_schedule', 'bad', f'yuanshen {flow}: missing next due time'
        if time.time() > due + 900:
            from miniapp_shared.priority import qyz_battle_window
            now = time.time()
            if now <= due + 3600 and any(qyz_battle_window(ts, XIUXIAN_ROOT)['active']
                                        for ts in (now, now - 300)):
                return 'yielding_to_qyz', 'ok', f'official reply #{receipt["id"]}; battle priority deferral'
            return 'overdue', 'bad', f'yuanshen {flow}: no result within 15 minutes of due time'
        return status or parsed['status'], 'ok', f'official reply #{receipt["id"]}; next due {due:.0f}'
    except (AttributeError, TypeError, ValueError, KeyError):
        return 'invalid_state', 'bad', 'yuanshen malformed business state'


def classify_helper(entry: dict[str, Any], disabled: set[str] | None) -> FlowHealth:
    label = entry['label']
    launchd = launchctl_snapshot(label)
    running = bool(re.search(r'^\s*state = running\s*$', launchd, re.M))
    exit_match = re.search(r'^\s*last exit code = (-?\d+)\s*$', launchd, re.M)
    status, severity, note = 'scheduled_idle', 'ok', f'loaded helper: {label}'
    if not launchd:
        status, severity, note = 'helper_not_loaded', 'bad', label
    elif disabled is None:
        status, severity, note = 'helper_scheduler_unverified', 'bad', label
    elif label in disabled:
        status, severity, note = 'helper_disabled', 'bad', label
    elif running:
        status = 'running'
    elif exit_match and int(exit_match[1]) != 0:
        status, severity, note = 'helper_last_run_failed', 'bad', f'{label}: exit {exit_match[1]}'
    elif exit_match and re.search(r'^\s*state = spawn scheduled\s*$', launchd, re.M):
        # These existing KeepAlive watchdogs run a single check, exit zero and
        # wait for launchd's next scheduled spawn; they are not daemons.
        status = 'scheduled_idle'
    elif re.search(r'^\s*properties = .*\bkeepalive\b', launchd, re.M):
        status, severity, note = 'helper_not_running', 'bad', label
    if severity == 'ok' and label in {
        'ai.hermes.xiuxian.tg9100000001.yuanshen-zhenmo',
        'ai.hermes.xiuxian.tg9100000001.yuanshen-xiulian',
    }:
        status, severity, note = yuanshen_helper_health(label)
    if severity == 'ok' and label == 'ai.hermes.xiuxian.tg9100000001.yuanshen.return_observer':
        status, severity, note = yuanshen_listener_health()
    return FlowHealth(entry['account'], entry['flow'], 'helper', status, severity, note,
                      True, False, None, None, None, None, None, running, None)


def collect_active_items(
    proc_text: str,
    active_flows: list[dict[str, Any]],
    log_signal_cache: dict[tuple[Path, str], Any],
) -> list[FlowHealth]:
    items: list[FlowHealth] = []
    disabled = disabled_launchagent_labels() if any('helper' in entry['kinds'] for entry in active_flows) else set()
    for entry in active_flows:
        account = entry['account']
        flow = entry['flow']
        kinds = entry.get('kinds') or ['send']
        for kind in kinds:
            if kind == 'helper':
                items.append(classify_helper(entry, disabled))
            else:
                items.append(classify_kind(account, flow, kind, proc_text, active=True,
                                           log_signal_cache=log_signal_cache,
                                           command_transport=entry.get('transport', 'telegram'),
                                           require_monitor_health=(entry.get('monitor_health_required', False)
                                                                   and is_persistent_event_monitor(flow, kind))))
    return items


def collect_legacy_items(
    proc_text: str,
    active_flows: list[dict[str, Any]],
    log_signal_cache: dict[tuple[Path, str], Any],
) -> tuple[list[FlowHealth], str | None]:
    if STATE_ROOT.is_symlink() or path_has_symlink_from_trusted_root(STATE_ROOT, XIUXIAN_ROOT):
        return [], f'state root symlink rejected: {STATE_ROOT}'
    active_keys = {(item['account'], item['flow']) for item in active_flows}
    legacy: list[FlowHealth] = []
    try:
        account_dirs = sorted(STATE_ROOT.iterdir())
    except OSError as exc:
        return [], f'state root unavailable: {type(exc).__name__}: {exc}'
    for account_dir in account_dirs:
        if account_dir.is_symlink():
            return legacy, f'legacy account symlink rejected: {account_dir}'
        if not account_dir.is_dir():
            continue
        try:
            flow_dirs = sorted(account_dir.iterdir())
        except OSError as exc:
            return legacy, f'legacy account state unavailable: {account_dir}: {type(exc).__name__}: {exc}'
        for flow_dir in flow_dirs:
            if flow_dir.is_symlink():
                return legacy, f'legacy flow symlink rejected: {flow_dir}'
            if not flow_dir.is_dir():
                continue
            key = (account_dir.name, flow_dir.name)
            if key in active_keys:
                continue
            legacy.append(
                classify_kind(
                    account_dir.name,
                    flow_dir.name,
                    'send',
                    proc_text,
                    active=False,
                    log_signal_cache=log_signal_cache,
                )
            )
    return legacy, None


def summarize(
    active_items: list[FlowHealth],
    legacy_items: list[FlowHealth],
    *,
    config_error: str | None = None,
    state_error: str | None = None,
) -> dict[str, Any]:
    counts = {'ok': 0, 'warn': 0, 'bad': 0}
    for item in active_items:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    if config_error:
        counts['bad'] += 1
    if state_error:
        counts['bad'] += 1
    overall = 'healthy'
    if counts['bad']:
        overall = 'degraded'
    elif counts['warn']:
        overall = 'warning'
    top_issues: list[dict[str, Any]] = []
    if config_error:
        top_issues.append({'kind': 'active_config', 'severity': 'bad', 'note': config_error})
    if state_error:
        top_issues.append({'kind': 'state_root', 'severity': 'bad', 'note': state_error})
    top_issues.extend(asdict(item) for item in active_items if item.severity != 'ok')
    top_issues = top_issues[:8]
    return {
        'overall_status': overall,
        'counts': counts,
        'checked_at': time.strftime('%Y-%m-%d %H:%M:%S%z'),
        'top_issues': top_issues,
        'active_flow_count': len(active_items),
        'ignored_legacy_count': len(legacy_items),
    }


def main() -> int:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    proc_text = process_snapshot()
    active_flows, config_error = load_active_config()
    log_signal_cache: dict[tuple[Path, str], Any] = {}
    active_items = collect_active_items(proc_text, active_flows, log_signal_cache)
    legacy_items, state_error = collect_legacy_items(proc_text, active_flows, log_signal_cache)

    payload = {
        'summary': summarize(active_items, legacy_items, config_error=config_error, state_error=state_error),
        'active_items': [asdict(item) for item in active_items],
        'ignored_legacy_items': [asdict(item) for item in legacy_items],
    }
    persist_json_atomic(OUTPUT_PATH, payload, mode=0o600)
    print(json.dumps(payload['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
