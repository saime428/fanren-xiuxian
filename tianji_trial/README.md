# Tianji Trial automation

独立、账号隔离的天机试炼 MiniApp 自动化。核心解题与状态机按
`Waaiging/fanrenxiuxian_auto` commit
`04d72e9026383d49ce529c88da2c2ecb8a9aca09` 适配。

## 生产语义

- 不向群里发送 `.天机试炼`；通过已授权个人账号取得洞府 WebView。
- 洞府入口只从 `@example_game_group` 的已验证置顶/公开洞府入口解析。
- 协议链：`dwelling/start` → `dwelling/external(action=tianji_trial)` →
  `trial/start` → 每关 `trial/finish(trialProof)`。
- 支持并 fail-closed 于五种模式：Lights Out、Memory、Stargaze、Meridian、Planarity。
- 每次提交前真实等待 `durationMs + 500ms`。
- `/start` 可以只返回有效 `challenge` 而没有 `dailyProgress`，与官方游戏页一致；
  此时进度保持未知，不虚构 `0/3` 或写入完成状态。若提供进度，仍严格校验。
- 只以服务器明确的每日上限回执，或 `dailyProgress.completed == dailyProgress.limit`
  （兼容结算 `result.daily_progress/daily_limit`）认定每日完成。
- 每关 `/finish` 必须有非空结算、有效进度及匹配的 challengeId（若服务器返回）；
  已知进度必须逐关增加。缺失/损坏回执保留 pending，下一轮按服务器当前题目恢复。
- `/start` 授权过期最多刷新一次 WebView 与一次性 `trial_*` token；
  `/finish` 不刷新、不重放旧 proof。
- `initData`、洞府 token、trial token、完整 WebView URL 只存在内存，不写状态/日志。

## 账号与身份

六个业务账号：

- `tg9100000001`
- `tg9100000002`
- `tg9100000003`
- `tg9100000004`
- `tg9100000005`
- `tg9100000006`（凭据源 `tg9100000007`）

每次首次执行从服务器动态读取该账号可用 `identity.choices`，逐身份串行完成。
状态按业务账号和 `playerId` 写入 `tianji_trial/state/accounts/`。

## 调度

Hermes 使用六个无 LLM cron，各账号每 15 分钟检查一次；Asia/Shanghai 当日
01:30、01:32、01:34、01:36、01:38、01:40（2026-09-20 起，原 08:00 档；见 docs/automation-profiles/README.md 错峰时刻表）前分别返回 waiting，完成后整日只读本地完成标记。
失败会在下一轮重试；未知 `/finish` 结果不会盲目重放。
正常结果和失败通知均为 `local`，不向 Telegram 重复发送 Cron 运维警告。
失败仍返回非零退出码，完整错误和重试状态保留在本机；不能靠改成成功退出消音。

## 手动验证

```bash
./.venv/bin/python -m tianji_trial.coordinator --account all --dry-run
./.venv/bin/python -m tianji_trial.coordinator --account tg9100000001
./.venv/bin/python -m unittest -v tianji_trial.test_tianji_trial
```

`--dry-run` 只认证并列出身份，写 `state/accounts/<账号>/dry_run.json`，不碰当天的 `summary.json`。
单元测试用临时目录驱动真实 coordinator，必须注入 `notify=` 并设 `MINIAPP_NOTIFY_DISABLED=1`，
否则日终统计会真的发到运维 Telegram（2026-09-21 事故）；`report.notify_if_due` 对非生产根目录且未注入 sender 直接跳过。
