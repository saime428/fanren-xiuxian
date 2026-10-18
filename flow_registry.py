"""One active-registry schema and LaunchAgent mapping for operations consumers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from nanlonghou_trade import TRADE_FLOWS, expected_trade_flow
from xuangu_quiz import RETIRED_FLOWS as RETIRED_XUANGU_FLOWS

VALID_KINDS = {'send', 'watchdog', 'return_observer', 'qyz_monitor'}
# Explicitly removed by the user; stale bundle configuration must not restore it.
REMOVED_ACCOUNT_FLOWS = frozenset({('tg9100000006', 'yuanyingchuqiao')})
REMOVED_LAUNCHAGENTS = frozenset(
    f'ai.hermes.xiuxian.{account}.{flow}.{kind}'
    for account, flow in REMOVED_ACCOUNT_FLOWS
    for kind in ('send', 'watchdog', 'return_observer', 'return_observer.watchdog')
)
SHARED_LAUNCHAGENTS = {
    ('qyz_miniapp', 'qyz_monitor'): 'ai.hermes.xiuxian.qingyuanzi-six.monitor',
    ('doufa_demo_player_02', 'send'): 'ai.hermes.xiuxian.doufa_demo_player_02.rotation',
}


def registry_path(root: Any) -> Path:
    """This deployment's generated registry, else the tracked six-account demo one.

    ``scripts/xiuxian_accounts.py render`` writes config/accounts/active_flows.json
    from the accounts config; tests always read the demo registry.
    """
    generated = Path(root) / 'config/accounts/active_flows.json'
    if generated.is_file() and not os.environ.get('XIUXIAN_EXAMPLE_CONFIG'):
        return generated
    return Path(root) / 'config/active_flows.json'


def kickstart_argv(label: str) -> list[str]:
    """Start a scheduled job now without waiting: launchd, or systemd user units on Linux.

    Units written by ``scripts/xiuxian_accounts.py install --systemd`` set
    XIU_SCHEDULER=systemd for every process they start.
    """
    if os.environ.get('XIU_SCHEDULER') == 'systemd':
        return ['systemctl', '--user', 'start', '--no-block', f'{label}.service']
    return ['/bin/launchctl', 'kickstart', f'gui/{os.getuid()}/{label}']


def is_safe_component(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None


def active_entries(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get('flows'), list):
        raise ValueError('expected object with flows list')
    enabled = []
    for index, item in enumerate(payload['flows']):
        if not isinstance(item, dict):
            raise ValueError(f'entry {index}: expected object')
        if not isinstance(item.get('enabled', True), bool):
            raise ValueError(f'entry {index}: enabled must be boolean')
        if not isinstance(item.get('monitor_health_required', False), bool):
            raise ValueError(f'entry {index}: monitor_health_required must be boolean')
        if item.get('transport', 'telegram') not in {'telegram', 'miniapp'}:
            raise ValueError(f'entry {index}: unsupported command transport')
        if item.get('transport') == 'miniapp':
            from miniapp_commands.catalog import ACCOUNTS
            if item.get('account') not in ACCOUNTS.get(item.get('flow'), ()):
                raise ValueError(f'entry {index}: unsupported MiniApp account/flow')
        if not is_safe_component(item.get('account')) or not is_safe_component(item.get('flow')):
            raise ValueError(f'entry {index}: unsafe or missing account/flow')
        kinds = item.get('kinds')
        if (not isinstance(kinds, list) or not kinds
                or any(not isinstance(kind, str) or kind not in VALID_KINDS for kind in kinds)):
            raise ValueError(f'entry {index}: kinds must be non-empty subset of {sorted(VALID_KINDS)}')
        interval = item.get('interval_dispatch_seconds')
        if interval is not None and (type(interval) is not int or interval < 60 or kinds != ['send']):
            raise ValueError(f'entry {index}: interval dispatch requires send-only and interval >= 60')
        if item.get('enabled', True):
            if (item['account'], item['flow']) in REMOVED_ACCOUNT_FLOWS:
                raise ValueError(f"removed account flow cannot be enabled: {item['account']}/{item['flow']}")
            if item['flow'] in RETIRED_XUANGU_FLOWS:
                raise ValueError(f"retired fixed-answer flow cannot be enabled: {item['flow']}")
            enabled.append(item)
    trade_accounts = set()
    for item in enabled:
        if item['flow'] in TRADE_FLOWS and 'send' in item['kinds']:
            if item['account'] in trade_accounts:
                raise ValueError(f"duplicate Nanlonghou choice for {item['account']}")
            trade_accounts.add(item['account'])
            if item['flow'] != expected_trade_flow(item['account']):
                raise ValueError(f"Nanlonghou account choice mismatch for {item['account']}")
    helpers = payload.get('helpers', [])
    if not isinstance(helpers, list):
        raise ValueError('helpers must be a list')
    seen = set()
    for index, item in enumerate(helpers):
        if not isinstance(item, dict) or not isinstance(item.get('enabled', True), bool):
            raise ValueError(f'helper {index}: expected object with boolean enabled')
        label = item.get('label')
        if 'miniapp_commands' in item and (label != 'ai.hermes.xiuxian.sect-daily'
                                           or item['miniapp_commands'] != ['.宗门点卯']):
            raise ValueError(f'helper {index}: unsupported MiniApp daily commands')
        if (not isinstance(label, str)
                or not re.fullmatch(r'ai\.hermes\.xiuxian\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', label)):
            raise ValueError(f'helper {index}: invalid Xiuxian LaunchAgent label')
        if label in seen:
            raise ValueError(f'helper {index}: duplicate label')
        seen.add(label)
        if item.get('enabled', True):
            if label in REMOVED_LAUNCHAGENTS:
                raise ValueError(f'helper {index}: removed account scheduler cannot be enabled')
            if label in retired_xuangu_launchagent_labels():
                raise ValueError(f'helper {index}: retired fixed-answer scheduler cannot be enabled')
            enabled.append({**item, 'account': 'helpers',
                            'flow': label.removeprefix('ai.hermes.xiuxian.'), 'kinds': ['helper']})
    return enabled


def launchagent_label(entry: dict[str, Any], kind: str) -> str:
    if kind == 'helper':
        return entry['label']
    flow = entry['flow']
    shared = SHARED_LAUNCHAGENTS.get((flow, kind))
    if shared:
        return shared
    account_prefix = '' if entry['account'] == 'aggregate' else f"{entry['account']}."
    return f'ai.hermes.xiuxian.{account_prefix}{flow}.{kind}'


def launchagent_labels(entries: list[dict[str, Any]]) -> list[str]:
    return sorted({'ai.hermes.xiuxian.return-deadline-dispatch'
                   if entry.get('interval_dispatch_seconds') is not None else launchagent_label(entry, kind)
                   for entry in entries for kind in entry['kinds']})


def retired_xuangu_launchagent_labels() -> list[str]:
    return sorted(f'ai.hermes.xiuxian.{flow}.{kind}'
                  for flow in RETIRED_XUANGU_FLOWS for kind in ('send', 'watchdog'))
