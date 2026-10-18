#!/usr/bin/env python3
"""Check the Git index without printing matched private values."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def violations(path: str, data: bytes, private_values: tuple[bytes, ...] = ()) -> set[str]:
    result: set[str] = set()
    lower = path.lower()
    parts = lower.split('/')
    if any(p in {'state', 'logs', 'session_copies', '.maintenance', '.private'} for p in parts):
        result.add('runtime-record')
    if lower.startswith('docs/reports/') or lower.endswith(('.session', '.sqlite', '.sqlite3', '.db', '.pem', '.key')):
        result.add('private-file')
    if (lower.endswith('.env') or '/.env.' in lower or lower.startswith('.env.')) and not lower.endswith('.example'):
        result.add('credential-file')
    combined = path.encode() + b'\n' + data
    for match in re.finditer(rb'(?i)tg(\d{9,12})(?!\d)', combined):
        if not re.fullmatch(rb'9[1234]\d{8}', match[1]):
            result.add('non-demo-account-id')
    patterns = (
        rb'\b\d{6,12}:[A-Za-z0-9_-]{25,}\b',
        rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
        rb'StringSession\([\x22\x27][A-Za-z0-9_=-]{100,}',
    )
    if any(re.search(pattern, combined) for pattern in patterns):
        result.add('credential-pattern')
    if any(value.lower() in combined.lower() for value in private_values):
        result.add('private-mapping-match')
    # The tracked accounts config is the demo one; real accounts live in the
    # gitignored config/accounts/accounts.json.
    if path == 'config/accounts.example.json':
        try:
            rows = json.loads(data)['accounts']
            if not rows or any(not str(row['username']).startswith('demo_player_') for row in rows):
                result.add('non-demo-account-map')
        except (ValueError, KeyError, TypeError):
            result.add('invalid-account-map')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-map', type=Path, default=os.environ.get('XIUXIAN_PRIVACY_MAP'))
    parser.add_argument('--all-history', action='store_true', help='Also check all reachable commits, paths, and refs.')
    args = parser.parse_args()
    root = args.root.resolve()
    def git(*options: str) -> bytes:
        return subprocess.check_output(['git', '-C', str(root), *options])
    map_path = args.private_map
    if map_path is None:
        candidate = Path(git('rev-parse', '--absolute-git-dir').decode().strip()) / 'privacy-original-values.json'
        if candidate.is_file():
            map_path = candidate
    values: tuple[bytes, ...] = ()
    if map_path is not None:
        mapping = json.loads(Path(map_path).read_text())
        values = tuple(str(value).encode() for value in mapping)
    failed = 0
    def check(path: str, data: bytes) -> None:
        nonlocal failed
        reasons = violations(path, data, values)
        if reasons:
            print('Privacy check blocked a tracked object: ' + ', '.join(sorted(reasons)))
            failed += 1
    for raw in git('ls-files', '-s', '-z').split(b'\0'):
        if not raw:
            continue
        meta, path_bytes = raw.split(b'\t', 1)
        path = path_bytes.decode()
        sha = meta.split()[1].decode()
        data = git('cat-file', 'blob', sha)
        check(path, data)
    if args.all_history:
        check('Git references', git('for-each-ref', '--format=%(refname)'))
        process = subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            for row in git('rev-list', '--objects', '--all').splitlines():
                sha, _, name = row.partition(b' ')
                process.stdin.write(sha + b'\n')
                process.stdin.flush()
                header = process.stdout.readline().split()
                data = process.stdout.read(int(header[2]))
                process.stdout.read(1)
                if header[1] == b'blob':
                    check(name.decode(), data)
                elif header[1] in {b'commit', b'tag'}:
                    check('Git metadata', data)
        finally:
            process.stdin.close()
            process.wait()
        if process.returncode:
            raise RuntimeError('history_scan_failed')
    print(f'Privacy check: {failed} file(s) blocked.')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
