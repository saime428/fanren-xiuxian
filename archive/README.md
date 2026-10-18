# Historical XIUXIAN material

- `openclaw-xiuxian/`: former OpenClaw project, including its historical state.
- `documents-xiuxian/`: the old `Documents/New project 3/xiuxian` copy.
- `migrations/hermes-cutover/`: Hermes migration records and old scheduler files.
- `migrations/HERMES_STAGING_NOT_ACTIVE.txt`: superseded staging marker.
- `backups/hermes-migration/`: pre-migration source and configuration snapshots.
- `backups/qyz-before-reference-20260909/`: Qingyuanzi reference migration backup.

Unique historical files retain their original contents. Duplicate copies and
generated caches may be removed after verifying a retained copy. Cleanup
manifests are stored under `state/cleanup-commit/`. The former locations are symlinks
into this archive, except for the superseded staging marker. They are not an
alternate production root and must not be included in fleet restore or source
rewrites. Local snapshots may contain sessions and private configuration, so the
archive contents are excluded from the production repository's Git index.

On 2026-09-10, the duplicate top-level `disabled/` tree and
`docs/LEGACY_CLEANUP_PLAN.md` were removed; their complete copies remain in
`openclaw-xiuxian/`. The duplicated
`backups/hermes-migration/qyz-vendor-before-head-sync-20260810-203138/qyz_upstream_original/`
was also removed; its contents are retained in
`backups/hermes-migration/pre-native-rewrite-20260809-164914/tree/qyz_upstream_original/`.
