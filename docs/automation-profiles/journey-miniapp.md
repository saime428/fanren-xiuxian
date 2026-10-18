# 档案：游历·谨慎（journey_miniapp）

**目的**：每个业务账号每天在洞府 MiniApp 完成 8 次「野外历练」`wild_experience`，模式固定 `cautious`（谨慎）。
@demo_player_01 只用绑定角色 示例绑定角色（playerId 9200000001），主魂 示例修士一 手动。

**入口**：`journey_miniapp/run_cycle.py`（`--check` / `--dry-run` / `--force` / `--account`）。
LaunchAgent `ai.hermes.xiuxian.journey-miniapp.daily`（`scripts/…journey-miniapp.daily.plist`）。
协议：`miniapp_shared/dwelling.py` 洞府入口 → `dwelling/start` → `dwelling/details` → journey action；
`journey_miniapp/protocol.py` 解析 `dailyCount / dailyLimit / readyAt / remainingSeconds`。

**调度与错峰**：槽位 00:15–00:30（09-21 起顺延，让灵溪垂钓先行）。`runner.admitted()` 以 00:15 为基准按账号 0/3/6/9/12/15 分钟准入，
日历触发 00:15/18/21/24/27/30 + 5 分钟 tick。批次内串行（`coordinator.py` 账号间 3 秒）。
每次动作后服务器给出冷却，`next_due_at` 写入状态，tick 只在到期后再登录。
0 点后 45 分钟内看到满计数且无 `resetAt` 视为昨日残留（`POST_MIDNIGHT_GUARD_SECONDS`，覆盖整个 00:15–00:30 批次），2 分钟后复查。

**状态**：`state/accounts/<账号>/journey_miniapp/state.json` 与 `identities/<playerId>.json`；
汇总 `state/journey_miniapp/summary.json`。成功判据：服务器 `dailyCount ≥ 8`（`daily_complete`）。

**通知**：`miniapp_shared.summaries.journey_notification` → Hermes `【游历·谨慎】`，全部完成一条；23:45 后未完成发日终统计。

**已知限制 / 迭代提示**：
- 凌晨偶发 `Connection reset by peer`（Telegram 入口阶段），已由重试覆盖，注意 `runner.launchd.error.log`。
- 若要改模式或范围，只改 `protocol.MODE` 与 `state.IDENTITY_SCOPE`，并同步测试 `test_runner.py`。
- 服务器 `dailyLimit` 若低于 8，以服务器为准。
