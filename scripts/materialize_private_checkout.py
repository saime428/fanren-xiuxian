#!/usr/bin/env python3
"""Render sanitized tracked files into a new private directory; never deploy."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def materialize(root: Path, mapping_path: Path, output: Path) -> int:
    mapping = json.loads(mapping_path.read_text())
    if not isinstance(mapping, dict) or not mapping or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in mapping.items()):
        raise ValueError('invalid_private_mapping')
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('ambiguous_private_mapping')
    output = output.resolve()
    root = root.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError('private_output_must_be_outside_checkout')
    reverse = sorted(((demo.encode(), real.encode()) for demo, real in mapping.items()), key=lambda pair: -len(pair[0]))
    def restore(data: bytes) -> bytes:
        for demo, real in reverse:
            data = data.replace(demo, real)
        return data
    rows = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-s', '-z']).split(b'\0')
    entries = []
    destinations = set()
    for row in rows:
        if not row:
            continue
        metadata, source = row.split(b'\t', 1)
        mode, sha, stage = metadata.split()
        if stage != b'0' or mode not in {b'100644', b'100755'}:
            raise ValueError('unsupported_index_entry')
        relative = Path(restore(source).decode())
        if relative.is_absolute() or '..' in relative.parts or relative in destinations:
            raise ValueError('unsafe_or_duplicate_private_path')
        destinations.add(relative)
        entries.append((relative, mode, sha))
    os.umask(0o077)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Prevent accidental git add/push from a materialized runtime copy.
    subprocess.run(['git', 'init', '--quiet', str(output)], check=True)
    (output / '.git' / 'hooks' / 'pre-commit').write_text('#!/bin/sh\necho "Private runtime checkout: commit from sanitized source instead." >&2\nexit 1\n')
    (output / '.git' / 'hooks' / 'pre-push').write_text('#!/bin/sh\necho "Private runtime checkout: pushing is disabled." >&2\nexit 1\n')
    for name in ('pre-commit', 'pre-push'):
        (output / '.git' / 'hooks' / name).chmod(0o700)
    for relative, mode, sha in entries:
        target = output / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = subprocess.check_output(['git', '-C', str(root), 'cat-file', 'blob', sha.decode()])
        target.write_bytes(restore(data))
        target.chmod(0o700 if mode == b'100755' else 0o600)
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-map', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    count = materialize(args.root, args.private_map, args.output)
    print(f'Created private source copy: {count} files. Credentials and runtime state must stay local.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
