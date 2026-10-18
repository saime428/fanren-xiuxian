# XIUXIAN 自动化档案总览与每日错峰时刻表（2026-09-20）

2026-10-15：[天星宗探索](tianxing-exploration.md)：示例修士一每天 00:00 深入游历、按冷却裂缝、每日节点；命星优先级、回执补命、Mini App 优先及 Hermes 批次报告。

2026-10-10：[洞府寻宝自动化](treasure-hunt.md)：六账号每日 04:00 后运行，方向线索与收益评估，正式结算后汇总报告。

2026-10-09：[群点命令迁入天机阁](miniapp-command-migration.md)：24 种当前自动化指令中，
9 种改由 MiniApp 完成，覆盖 7 类流程、21 个账号／流程组合；原账号与日程沿用。

用户要求：所有自动化任务以 **Asia/Shanghai 0 点** 为每日重置点开始新一轮，各部分错峰执行，
避免同一时刻大量 Telegram / MiniApp API 请求触发限流；每个自动化写一份档案便于迭代。

本目录每个文件对应一个自动化（或一组同类账号流程）。档案统一包含：目的与游戏规则、
入口与代码位置、调度与错峰槽位、状态与成功判据、通知、已知限制与迭代提示。

## 一、每日时刻表（Asia/Shanghai）

| 槽位 | 自动化 | 账号节奏 | 说明 |
| --- | --- | --- | --- |
| 00:00 | [垂钓日重置](fishing.md) `miniapp-fishing.daily-reset` | 本地文件重置，无网络 | 只清本地配额，不登录 |
| **00:00 – 00:10** | [灵溪垂钓](fishing.md) `miniapp-fishing.send` | 首竿每 2 分钟一个账号（00:00/02/04/06/08/10），之后按服务器冷却 | **用户指定优先**，五个号每号 5 竿；@demo_player_01 打三窝、钓到服务器上限（约 16 竿，持续到 01:00 前后，与后续批次重叠，只用垂钓接口） |
| 00:15 – 00:30 | [游历·谨慎](journey-miniapp.md) `journey-miniapp.daily` | 6 账号每 3 分钟一个（00:15/18/21/24/27/30） | 每账号 8 次，服务器冷却期间自动等待 |
| 01:00 – 01:15 | [宗门日常](sect-daily.md) `sect-daily` | 6 账号每 3 分钟一个，串行 | 点卯 / 军报 / 支援慕兰（群指令） |
| 01:30 – 01:40 | [天机试炼](tianji-trial.md) Hermes cron ×6 | 每 2 分钟一个账号；02:30–02:55 二次补跑 | 原 08:00 档，09-20 移入凌晨 |
| **02:00 – 02:10** | [琉璃问心塔](tower-miniapp.md) `tower-miniapp.daily` | 6 账号每 2 分钟一个（02:00…02:10） | 每身份一次挑战；**用户 09-23 指定 2 点开始**（原 00:35）。与 02:00 Hermes 本地备份无网络冲突；失败 15 分钟后重试可能与天机 02:30 补跑相邻，两者均串行且天机当天已完成时只读 |
| 03:00 – 约 05:45 | [天机命脉 + 噬金虫进化](fate-cards.md) `fate-cards.daily` | 6 账号串行（准入 03:00…03:15，每 3 分钟一个） | 每身份一次：启牌→命择→任务→验命；随后把当天剩余的噬金虫局数全部打完（**用户 09-26 要求打满**，每天 5 局、每局约 5 分钟，每号约 27 分钟）；13:36–13:52 前不开新局 |
| 按冷却 | [藏娇阁 双侍妾循环](pavilion-loop.md) `pavilion-loop.send` | 10 分钟 tick，**只按各侍妾的服务器计时器**（航程 4–12h / 冷却 480–720 min）唤醒，不占固定槽位 | 远航归来→代卜→寻图→心劫→拼图→再远航，全部 APP 内 |
| 全天 | [星宫灵圃](star-observatory.md) `star-observatory.send` | 5 分钟 tick，按服务器星辰计时器 | 无日重置，约 36 小时一批 |
| 全天 | [群指令冷却类流程](group-command-flows.md)（深度闭关 / 元婴出窍 / 探寻裂缝 / 问道 / 血洗山林 / 召唤魔影 / 抚摸法宝 / 双修温养） | 各自按官方冷却（2h–12h）滚动 | 无固定日槽位；回复中的冷却时间决定下一次 |
| 全天 / 归位推送 | [第二元神修炼 / 镇魔](demo_player_01-yuanshen.md)（@demo_player_01） | **独立 `yuanshen.return_observer`**，推送即触发、60 秒历史补查；不寄宿深度闭关 / 元婴监听 | 修炼 24h 计时只作兜底；无剩余时间 60 秒查状态，未确认发送 120 秒核对；镇魔按 6h 冷却和低魔染复查、每日最多 2 次 |
| 事件驱动 | [事件应答类流程](event-reply-flows.md)（玄骨作答 / 极阴祖师选择 / 南陇侯交换 / 神迹布道 / 配方通知） | 官方消息出现时才发 | 无调度冲突 |
| 13:39:30 起 | [青元子世界 Boss](qingyuanzi.md) `qingyuanzi-six.monitor` | 六账号常驻监听，入场后并行验证 | 官方约 13:40 开启，与凌晨批次无关 |
| 持续 | [维护与看门狗](maintenance.md)（return-deadline-dispatch / healthcheck / log-retention / watchdogs / 战报） | 30 秒–7 天 | 只读或本地 |

设计原则：

1. **日重置 = 0 点**：所有“每日 N 次”流程的判定日键都是 `miniapp_shared.state.day_key()`（Asia/Shanghai），
   0 点后视为新一轮；服务器自身也在 0 点刷新（游历 `server_reset_at`、慕兰“上海时间刷新”）。
2. **错峰**：灵溪垂钓固定占 0 点首槽（用户 09-21 指定），琉璃问心塔固定 02:00（用户 09-23 指定），其余日批次顺延，每个批次占 10–15 分钟槽位，槽位之间留 5 分钟空档；批次内部按账号
   2–3 分钟准入偏移，且串行执行（同一时刻最多一个账号在登录 MiniApp）。
3. **限流保护**：所有群指令共用 `state/global_telegram_send_gate.json`（≥1 秒/条）；Telethon 连接
   共用 `state/telegram_connection_gate.lock`（≤4 并发握手）；MiniApp 流程遇 `external_action_rate_limited`
   按 5/15/60 分钟退避；未确认的操作只重试同一请求，不新建。
4. **青元子最高优先级（2026-09-20 用户规则）**：青元子战斗以成绩为准，不受错峰与限流约束。
   其他所有访问游戏 API 的流程在 `miniapp_shared.priority.yield_to_qyz` 判定的窗口内（每天 13:36–13:52，
   或宿主写入入口后 12 分钟内）直接跳过本次 tick，返回 `yielding_to_qyz`；青元子宿主本身不经过任何共享发送闸，
   其验证并发限制（Chrome 2 / 页面 3）是为拿 token 的稳定性而设，不是限流让步。
5. **准入门**：每个日批次都有 `admitted(account, now)`；未到槽位只返回 `waiting_admission`，
   不打开任何会话。补跑由各自的 `StartInterval` tick 负责，直到当天完成或 23:45 日终统计。

## 二、验证方式

```bash
cd /Users/operator/.hermes/workspace/projects/xiuxian
bash scripts/verify_miniapp_daily_flows.sh          # 离线检查 + 单元测试 + LaunchAgent 载入状态
./.venv/bin/python3 -m journey_miniapp.run_cycle --check
./.venv/bin/python3 -m tower_miniapp.run_cycle --check
./.venv/bin/python3 -m sect_daily.run_cycle --check
hermes cron list | grep -A2 Tianji
```

次日凌晨核对：`state/journey_miniapp/summary.json`、`state/tower_miniapp/summary.json`、
`fishing/state/fishing_coordinator.json`、`state/sect_daily/summary.json`、
`tianji_trial/state/accounts/<账号>/summary.json`，以及 Hermes 送达的四条完成汇总。
