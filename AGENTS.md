# XIUXIAN directory ownership

The canonical project root is `/Users/operator/.hermes/workspace/projects/xiuxian`.
Keep all XIUXIAN implementation scripts under this root. See
[docs/DIRECTORY_LAYOUT.md](docs/DIRECTORY_LAYOUT.md) for the directory map.

- Shared Python modules remain at the root; command wrappers, health checks,
  dispatchers, and maintenance tools live in `scripts/` with shared shell code
  in `scripts/lib/`.
- Qingyuanzi and fishing keep their independent module, test, and state trees in
  `qingyuanzi-new-source/` and `fishing/`.
- `references/` contains independent upstream repositories. `archive/` contains
  inactive copies and migration backups. Neither directory is production code;
  exclude both from production discovery, rewrites, and recursive test runs.
- External framework entry points may forward into this root. Keep business
  logic here; do not create a second implementation beside those entry points.
- Runtime state stays with its current primary writer. Directory organization
  must preserve sessions, account attribution, scheduler labels, and live tasks.
- Read the parent workspace's `OPERATIONS.md` for scheduling changes. Use offline
  checks for directory maintenance; starting a sender is not a path check.
