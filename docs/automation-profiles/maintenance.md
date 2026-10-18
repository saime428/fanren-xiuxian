# 档案：维护与看门狗

| 项目 | 入口 | 节奏 | 说明 |
| --- | --- | --- | --- |
| return-deadline-dispatch | `scripts/return_deadline_dispatch.py`（LaunchAgent 同名） | 30 秒 | 深度闭关 / 元婴出窍的官方倒计时或待确认指令到期时非强制叫起发送器；三组抚摸法宝、五组探寻裂缝、问道、血洗山林、召唤魔影按官方回执期限恢复冷却重试及成功后的周期，不受 supervisor 重启重置间隔影响。也处理第二元神已保存的归位事件、60 秒状态补查、120 秒未确认重试。只读业务状态，不承担 Telegram 监听，尊重已有暂停机制及专属调度归属 |
| watchdogs（`*.watchdog`, `*.return_observer.watchdog`） | 各 `scripts/watch_*.sh` | 5–10 分钟 | 发送器 watchdog 看锁与官方回执的新鲜度，卡住时只做非强制 `launchctl kickstart`（不杀进程）；`*.return_observer.watchdog` 读观察器自己写的 `observer_health.json`，最近一次验证扫描超过 30 分钟、缺失或归属不符即报 BAD，只报告、不重启。账号 `pause_all` 与流程暂停标记都会让它们跳过 |
| staging.healthcheck | `scripts/xiuxian_staging_healthcheck.sh` | 15 分钟 | 离线语法与跟踪文件卫生 |
| Xiuxian Active Healthcheck（Hermes cron） | `scripts/xiuxian_active_healthcheck.sh` → `scripts/xiuxian_healthcheck.py` | 30 分钟 | 注册表 vs LaunchAgent vs 状态证据；有界日志尾读；结果 `~/.hermes/workspace/state/xiuxian-healthcheck.json` |
| log-retention | `scripts/xiuxian_log_retention_apply.py --keep-mib 10`（**实际执行**，不是 dry-run） | 每周日 06:30（主机本地时区，`StartCalendarInterval`） | 原地截断 `state/accounts/**/*.log`，每个超过 10 MiB 的文件只保留最后 10 MiB（同一 inode，跳过软链接）；结果写 `state/xiuxian-log-retention-apply.json`。只读计划工具 `scripts/xiuxian_log_retention.py` 不在调度里 |
| qyz-battle-report | `qingyuanzi-new-source/qyz_battle_report.py` | 60 秒只读 | 每场一条战报 |
| Hermes Quick Backup | `hermes_quick_backup.sh` | 02:00 | 与凌晨批次错开（最后一个业务槽位 02:55 是天机二次补跑，只读为主） |

**限流相关共享件**：`telegram_send_gate.py`（全局 ≥1 秒/条），`transport._connect_with_gate`（≤4 并发握手，
`state/telegram_connection_gate.lock`），`transport.with_telegram_retry`（FloodWait 按服务器秒数等待，429 冷却 30 秒起指数退避）。

2026-10-05 增补：同一 30 秒 dispatcher 也读取当天未完成的天机试炼重试期限，复用运行镜像
清单中的原六账号 cron label；无记录、已完成、昨日记录或禁用任务不触发，详见 [天机试炼](tianji-trial.md)。

**迭代提示**：新增任何日批次先在 `docs/automation-profiles/README.md` 的时刻表登记槽位，再实现 `admitted()` 准入门与
日历触发；健康检查把新 helper 标签加入 `config/active_flows.json.helpers` 即自动纳入。
