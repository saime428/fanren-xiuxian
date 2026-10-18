# 档案：天机试炼（tianji_trial）

**目的**：每个业务账号的每个身份每天完成服务器要求次数的天机试炼（`trial/start` → 每个 challenge 一次 `trial/finish`），
只认服务器 `completed >= limit`。绝不向群发送 `.天机试炼`。

**入口**：`tianji_trial/coordinator.py --account <账号>`，包装 `scripts/run_tianji_trial_account.sh`；
调度是 Hermes cron 六条 no-agent 任务（`~/.hermes/scripts/xiuxian_tianji_trial_<账号>.sh`）。

**调度与错峰**：槽位 01:30–01:40（09-20 从 08:00 迁入凌晨）。`coordinator.START_HOUR/START_MINUTE = 1, 30`，
账号偏移 0/2/4/6/8/10 分钟；cron 表达式 `30,45 1,2 * * *`（依账号 +2 分钟）：01:30 首跑、01:45 补跑、02:30/02:45 二次补跑。
未到槽位返回 `waiting`，当天完成后只读本地标记。

2026-10-05 起，当天已经开始但未完成的 `summary.status=retry` 另由现有
`return-deadline-dispatch` 每 30 秒检查 `retry_not_before_ts`（120 秒），到期非强制叫起
运行镜像清单中的原账号 cron label。最后一个每日补跑时段后仍能恢复未完成任务。
只处理当天该账号的未完成记录；无记录、昨日记录、已完成、暂停或禁用任务不触发。
旧 retry 记录按 `updated_at_ts + 120` 兼容。账号锁、青元子避让、官方次数核对和
不确定 finish 的原有恢复规则仍由 coordinator 负责，不重放旧 proof。

**状态**：`tianji_trial/state/accounts/<账号>/summary.json`（`receipt_confirmed`、`identities`）；`--dry-run` 只写 `dry_run.json`，
绝不覆盖当天 summary（09-21 曾因此把已完成账号改成 `ready`）。

**通知**：`tianji_trial/report.py` 每天一条 `【天机试炼】`（六账号全部确认完成时立即发；03:05 后未齐则发日终统计），由每个账号 cron 跑完后触发，键在 `state/tianji_trial/notifications.json`。
`notify_if_due` 对非生产根目录且未注入 sender 的调用返回 `skipped_non_production_root`：09-21 `test_tianji_trial.py` 用临时目录跑真实
coordinator，把 7 条假的“日终统计：完成 0/6 … 无记录”发到了 Telegram；真实结果是 6/6 全部完成（message 33807）。
测试必须注入 notifier 并设 `MINIAPP_NOTIFY_DISABLED=1`。

**迭代提示**：五种 proof 模式对照上游固定版本；`/finish` 不确定回包后绝不重放旧 proof。改时间只改 `START_HOUR/START_MINUTE`
与 cron 表达式，并同步 `tianji_trial/README.md`、维护 SKILL。
