# 档案：宗门日常（sect_daily）

2026-10-09：点卯改用天机阁 `command-center`，其余军报／支援步骤继续走官方现有群入口。
MiniApp 失败不会降级为群点卯；新回执与每日步骤共同保存。见[迁移说明](miniapp-command-migration.md)。

**目的**：群文本指令 `.宗门点卯` → `.边境军功` → `.搜集军报` → `.公开军报 <真报编号>` → `.支援慕兰 <路线>`，六账号每天一次。
规则来源与真报表见 `docs/sect-daily-2026-09-20.md`。

**入口**：`sect_daily/run_cycle.py`；LaunchAgent `ai.hermes.xiuxian.sect-daily`。发送经 `transport.send_with_global_gate`
（全局 1 秒/条），回复只认 `command_confirmation.OFFICIAL_REPLY_SENDER_IDS` 对本消息的直接回复。

**调度与错峰**：槽位 01:00–01:15（用户指定凌晨 1 点），账号偏移 0/3/6/9/12/15 分钟，串行；日历触发 01:00、01:16 + 10 分钟 tick；
未确认步骤 15 分钟后只重发该步。

**决策**：已公开真报的路线 > 军议密令 > 粮道通畅 > 天象；险棋窗口路线只在真报助力时选；无信号回退护阵。
面板已支援 / 今日已点卯 / 已支援过 → 视为完成，不再发送。

**状态**：`state/accounts/<账号>/sect_daily/state.json`（每步 `sent_message_id / reply_message_id / status`），`state/sect_daily/summary.json`。

**通知**：`sect_notification` → `【宗门日常】`。

**迭代提示**：新增真报只从 `【慕兰谍影·真报】` 回复反推并写入 `protocol.TRUE_REPORTS`；军议字段变化改 `parse_panel`；
若要加 `.辨报`，在 `runner` 的 collect 与 publish 之间插入一步并复用 `GroupCommander.command`。

**重发保护（2026-09-22 事故）**：@demo_player_04 当天把 `.公开军报 3` 发了 40 次——01:14 发出后读取回复时 Telethon 报错，
运行中止时该步尚未记账，之后每一轮都当作没发过；旧解析器又把“今日已有军报公开”当成未知回复。现在每次发送先记账
（`sent_message_id`、`send_count`）再等回复；下一轮先按 `min_id` 正向查找该消息的官方回复，找不到才重发，同一步每天最多发 2 次
（`sect_daily.runner.MAX_STEP_SENDS`），超过则保持 `unconfirmed` 进日终统计。LaunchAgent 若被卸载，用
`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.hermes.xiuxian.sect-daily.plist` 重载。
