> Account data uses demo values. Read [the privacy workflow](docs/privacy.md) before deployment.

# xiuxian

## 快速开始：用自己的账号

账号和每个账号开哪些功能都写在一份配置里，定时任务由它生成。macOS 和 Linux 都能跑，
Windows 请用 WSL2。完整步骤见 [用自己的账号](docs/self-service-accounts.md)。

最简单的是本地网页：填 API id/hash 和手机号、输验证码就能加号，功能用勾选框选，最后点「保存并应用」。

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python3 scripts/xiuxian_web.py      # 打开 http://127.0.0.1:8765
```

同样的事也能用命令行做：

```sh
X=".venv/bin/python3 scripts/xiuxian_accounts.py"
$X game --group <游戏群> --chat-id <-100…> --topic-id <话题 id> --second-chat-id <-100…>
$X add tg<你的 Telegram 用户 id> --username <你的用户名>   # 然后在 config/accounts/tg<id>.env 填 API id/hash
$X login tg<id>
$X features
$X enable tg<id> shendubiguan tower journey fishing
$X render
$X install --systemd    # macOS 用 --launchd
```

下文是原项目的运维说明。其中的账号都是示例账号。

统一根目录：`~/.hermes/workspace/projects/xiuxian`。主流程和运维入口位于
`scripts/`，青元子、垂钓、天机试炼分别位于 `qingyuanzi-new-source/`、`fishing/`、
`tianji_trial/`，参考源码放在 `references/`，历史副本和迁移备份放在 `archive/`。
完整路径及兼容入口见 [目录说明](docs/DIRECTORY_LAYOUT.md)。

星宫灵圃（牵引天雷星 / 一键安抚 / 一键收集）、琉璃问心塔、游历（谨慎 ×8）三条
MiniApp 日常流程位于 `star_observatory/`、`tower_miniapp/`、`journey_miniapp/`，群指令日常 `sect_daily/`（点卯 / 军报 / 支援慕兰，见 [docs/sect-daily-2026-09-20.md](docs/sect-daily-2026-09-20.md)），
共用 `miniapp_shared/` 的洞府入口链；说明见
[MiniApp 日常流程](docs/miniapp-daily-flows-2026-09-19.md)。
每个自动化的了解档案与 0 点错峰时刻表见 [docs/automation-profiles/README.md](docs/automation-profiles/README.md)。

Use your own Telegram account, not a bot, to post a fixed message into a Telegram forum topic on a schedule.

## What this does

- logs in with your personal Telegram account using MTProto
- stores a local session file on this machine
- sends a message into a target group/topic
- can be triggered by cron at `00/15/30/45`

Current default target in `.env.example`:

- target: `@example_game_group`
- topic id: `7310786`
- message: `.闭关修炼`

## 1. Create Telegram API credentials

Go to <https://my.telegram.org> and create an API app.
You will receive:

- `api_id`
- `api_hash`

These are for your personal account session and are different from a bot token.

## 2. Set up Python env

```bash
cd ~/.hermes/workspace/projects/xiuxian
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and fill in:

- `TG_API_ID`
- `TG_API_HASH`

## 3. First login test

```bash
cd ~/.hermes/workspace/projects/xiuxian
source .venv/bin/activate
python send_topic_message.py
```

On first run, Telethon will ask for:

- your phone number
- login code from Telegram
- 2FA password if enabled

This creates a local session file under `state/`.

## 4. Dry-run option

To verify login without sending or deleting anything, set:

```env
TG_DRY_RUN=1
```

Then run:

```bash
python send_topic_message.py
```

## 5. Legacy single-flow cron (old layout)

This older direct `send_topic_message.py` crontab example is kept only as historical context.
For current live flows, use only enabled entries from `config/active_flows.json` and their account-aware wrappers under `scripts/`. Logs and journals live in `state/accounts/<account>/<flow>/`. Historical account/flow examples below do not authorize restoring retired jobs. The current receipt/retry contract is documented in [command confirmation](docs/command-receipt-retry-2026-09-13.md).

Legacy example:

```cron
0,15,30,45 * * * * cd /Users/operator/.hermes/workspace/projects/xiuxian && . .venv/bin/activate && /usr/bin/env python send_topic_message.py >> state/send.log 2>&1
```

## 6. Split flows by account + command

`send_topic_message.py` / `auto_reply_topic_event.py` now support a cleaner account-aware wrapper layout.

Recommended rule:

- one Telegram account slug = one stable `XIU_ACCOUNT_SLUG`
- one flow slug = one stable `XIU_FLOW_SLUG`
- runtime files live under `state/accounts/<account>/<flow>/`

Examples:

```bash
state/accounts/tg9100000002/shendubiguan/
state/accounts/tg9100000002/xuangu_zuoda/
```

Typical files per flow:

- `session`
- `send.lock`
- `last_success.json`
- `last_attempt.json`
- `cooldown_retry.json`
- `watchdog.log`

A shared helper is used by wrappers:

```bash
source ./scripts/lib/account_flow_paths.sh
```

It derives:

- `TG_STATE_DIR`
- `TG_SESSION_NAME`
- `TG_LOCK_PATH`
- `TG_LAST_SUCCESS_PATH`
- `TG_LAST_ATTEMPT_PATH`
- `TG_COOLDOWN_STATE_PATH`
- `TG_WATCHDOG_LOG_PATH`

Hardening notes:

- `send_topic_message.py` now writes JSON lock metadata (`pid`, `started_at_ts`, `flow_slug`) into the lock file while holding the OS lock.
- `TG_RUN_MAX_SECONDS` caps a single run so Telethon stalls cannot hold the flow forever.
- `watch_send_health.py` treats active cooldown waits and fresh active locks as non-fail states, but flags stale locks after `TG_WATCHDOG_LOCK_STALE_SECONDS`.
- Telethon wrappers now run `scripts/ensure_session_authorized.py` first. The helper loads `.env`, checks whether `TG_SESSION_NAME` is already authorized, and if not can auto-reseed it from a same-account fallback session via `TG_SESSION_FALLBACK_NAME` (set per account in `scripts/lib/account_flow_paths.sh`, for example `state/tg9100000002`; never in the account env file) after backing up the bad sqlite.

### Local Surge MTProto transport

All account-aware wrappers load the private `config/accounts/mtproto.env` file
through `scripts/lib/account_flow_paths.sh`. When `TG_MTPROTO_ENABLED=1`,
`transport.telegram_client()` requires a valid 32-hex secret and uses
Telethon's `ConnectionTcpMTProxyIntermediate` against the configured local
Surge listener. The Qing Yuanzi six-account host loads the same private file.

The production Mac endpoint is loopback-only (`127.0.0.1:5753`). Disabling
`TG_MTPROTO_ENABLED` is the explicit rollback; a failed listener does not
silently fall back to a direct Telegram connection.

Different account-aware flows no longer share cooldown/last-success state unless explicitly pointed to the same paths.

Live wrappers follow one naming scheme: `scripts/send_<flow>_tg<account>.sh` and
`scripts/watch_<flow>_tg<account>.sh`. Each wrapper exports `XIU_ACCOUNT_SLUG` /
`XIU_FLOW_SLUG`, sources `scripts/lib/account_flow_paths.sh` for the state paths and
then the shared flow body in `scripts/lib/run_<flow>.sh` (or execs the Python entry
directly). Every live flow is registered in `config/active_flows.json`, which
`flow_registry.py` validates. Production runs on a VPS under supervisor: its job
list is generated from `config/active_flows.json` and baked into the container
image, and it runs each job's script by file name. A new job is loaded only by a
rebuilt image (flows started by `return-deadline-dispatch` via
`interval_dispatch_seconds` read the registry live), and renaming an enabled
job's wrapper breaks that job (see
[group-command flows](docs/automation-profiles/group-command-flows.md)). The
`scripts/*.plist` files are macOS launchd templates kept as the reference
configuration for each job; production does not load them.

### Pausing an account or a flow

Pausing is a marker file; nothing is unloaded, killed or restarted. The
per-account flows check `state_store.is_paused()` before acting and skip
while a marker exists: the group-command senders (`send_topic_message.py`,
`send_shendubiguan.py`), the MiniApp command runner, the return observers,
the account-bound question/trade reply monitors, the 30-second deadline and
interval dispatchers (including the 天机试炼 retry deadlines), `tianxing_exploration`,
`treasure_hunt` and the watchdogs. A run already in flight
finishes normally. The fleet-wide MiniApp coordinators (垂钓, 星宫, 问心塔, 游历,
天机命脉, 宗门日常, 洞府侍妾, 青元子) keep their own schedules and do not read
these markers.

| Scope | Pause | Resume |
| --- | --- | --- |
| every flow of one account | `touch state/accounts/<account>/pause_all` | `rm state/accounts/<account>/pause_all` |
| one flow of one account | `touch state/accounts/<account>/<flow>/pause_<flow>` | `rm state/accounts/<account>/<flow>/pause_<flow>` |
| one return observer only | `touch state/accounts/<account>/<flow>/pause_<flow>_observer` | `rm` the same file |

For example, `touch state/accounts/tg9100000003/pause_all` stops the flows
listed above for that account at their next check; removing the file resumes
them at their next scheduled run (no reload needed). Watchdogs report
`Paused via <file>` instead of failing. The duel campaign's
`pause_until.json` is a different, quota-bound marker: clear it with
`scripts/resume_doufa_demo_player_02.sh <account>`.

Xuangu uses the six account-bound `xuangu_zuoda` LaunchAgents in
`config/active_flows.json`. Each monitor maps the current question to its answer
text and current option letter, replies to the original question within at most
300 seconds, and requires an attributed official result before recording success.
See [the Xuangu verification record](docs/xuangu-automation-review-2026-09-13.md).

All wrappers should use dedicated account + flow state roots so different flows and different Telegram accounts do not contend on the same sqlite session file or success/cooldown state.

## Notes

- `TG_TARGET` can be a public username like `@example_game_group` or a numeric chat id if needed.
- `TG_TOPIC_ID` is the forum topic id, here `7310786`.
- Session and secrets stay local and are ignored by git.
- This route is separate from OpenClaw's Telegram bot channel.

## Module layout (2026-07 refactor)

Shared modules, imported by every entry script:

- `runtime.py` — timestamped `log_print`, `env()` helper
- `reply_parsing.py` — all pure text parsing/classification: marker parsing, cooldown/duration parsing (anchored to `请在…后` so unrelated numbers cannot inflate the wait), `classify_reply_messages` + `ReplyOutcome`/`AckPolicy`, `decide_poll_action`, and the 深度闭关 status/action classifiers
- `state_store.py` — atomic JSON persistence (`tmp` + `os.replace`), `flow_lock` (flock with inode verification; unlinks before releasing so two runs can never both hold a flow), pause checks, cooldown-retry state helpers
- `transport.py` — Telethon connect/retry/collect helpers and the gated send wrapper
- `telegram_send_gate.py` — cross-account send spacing. Reads `TG_GLOBAL_SEND_GATE_STATE_PATH` (set by `scripts/lib/account_flow_paths.sh` to one shared path) and claims slots under an flock, so the 1 msg/sec limit really is global.

Entry scripts retain their account-specific paths. `send_topic_message.py` persists every unconfirmed command and retries at the allowed interval until a matching official business reply arrives, including after restart. `send_shendubiguan.py` applies the same rule to both the status probe and the action. Only confirmed success writes `last_success.json`; other official outcomes and their next due time use `last_result.json`. `watch_send_health.py` inspects the actual flock and receipt identity, reports waiting retries as WARN and stalled retries as BAD, and never terminates a sender or force-restarts it. See [the receipt contract and state ownership](docs/command-receipt-retry-2026-09-13.md).

Run tests with:

```bash
./.venv/bin/python -m pytest -q   # pytest.ini already excludes archive/, references/ and state/
```

## 深度闭关 return observer

`observe_return.py` 偵測到新嘅深度閉關總結後，會立即啟動對應 sender。六個帳號 wrapper 均設定 `TG_OBSERVER_PERSISTENT=1` 及 5 秒 poll interval；每個 LaunchAgent 會保留一個長駐 Python process，在 process 內完成 scan → sleep → scan，唔再每次掃描後退出並由 `KeepAlive` 重新啟動。每個 cycle 仍有原本的 Telegram timeout、observer flock 與 atomic state，network／temporary cycle failure 只會延後下一輪，唔會終止 loop。

Observer kick 會設定 `TG_OBSERVER_IMMEDIATE_KICK=1`。如果排程 sender 當時仍持有 `send.lock`，或者 status/action send 無 message ID、probe/action timeout、reply unknown 等未完成結果，`send_shendubiguan.py` 會以暫時失敗碼 `75` 結束。只有 `started`、`already_in_retreat` 或明確有效 countdown 類結果先會 clean exit；Observer 因此只喺有 proven healthy outcome 時寫入 `last_seen_event_id`，否則下一個 in-process cycle 重試同一個報告。

一般排程／watchdog 遇到同一個 lock busy 仍然係正常 skip（exit `0`），避免重複工作。

## Healthcheck performance and log retention

`scripts/xiuxian_healthcheck.py` is a fast-path active-flow snapshot. It reads
only bounded tails from the newest kind-specific log files and caches one log
signal per account/flow/kind for the duration of a run. A missing or malformed
active-flow registry is degraded, never healthy. Do not replace the bounded tail with
`Path.read_text().splitlines()`; several live launchd logs are hundreds of MB.

Benchmark the live snapshot with:

```bash
/usr/bin/time -lp ./.venv/bin/python3 scripts/xiuxian_healthcheck.py
```

The scheduled `ai.hermes.xiuxian.log-retention` job is **not** a dry run: every
Sunday at 06:30 host-local time it runs `scripts/xiuxian_log_retention_apply.py
--keep-mib 10`, which truncates each `state/accounts/**/*.log` larger than 10 MiB
in place to its last 10 MiB (same inode, symlinks skipped) and records the result
in `state/xiuxian-log-retention-apply.json`.

Separately, build a **dry-run only** retention plan with:

```bash
./.venv/bin/python3 scripts/xiuxian_log_retention.py
```

The planner writes `../state/xiuxian-log-retention-plan.json`. It does not
delete, truncate, compress, or rotate logs. It records visible symlinks plus
inode/mtime/open-detected snapshot data, and reports reclaim estimates only when
open-file detection succeeded. This is a best-effort pathname snapshot, not an
authoritative or race-free apply manifest. Estimates are not guarantees.
Any future apply path must revalidate root containment, non-symlink status,
inode, mtime, size, and open-file state using fd-based/O_NOFOLLOW operations
before changing a file. Negative retention CLI values are rejected.
