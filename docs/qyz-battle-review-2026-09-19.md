# 2026-09-19 青元子复盘：三天 1/18 开战，v19 并行验证退化与 v20 修复

复盘对象是 09-17、09-18、09-19 三场（事件 128、129、130）。所有时间为 Asia/Shanghai。
证据只取六个账号各自的 `battle_diagnostics/<入口ID>.json` 与 `world_boss_state.json`；
认证材料、Turnstile token、签名 URL 均未进入本文与代码。

## 一、结论

| 场次 | 入口 | 开战 | 正式 finish | 命中 / PERFECT | 失败原因 |
| --- | --- | ---: | ---: | --- | --- |
| 09-17 事件 128 | 12373121 | 1/6 | 1/6 | @demo_player_01 16/16 命中、14 PERFECT、6.48 亿亿、甲等 | 其余 5 账号 `world_boss_turnstile_timeout` |
| 09-18 事件 129 | 12389522 | 0/6 | 0/6 | 无 | 六账号全部 `world_boss_turnstile_timeout` |
| 09-19 事件 130 | 12406242 | 0/6 | 0/6 | 无 | 六账号全部 `world_boss_turnstile_timeout` |

对照 09-16 事件 126（v18，单一共享 Chrome profile）：6/6 拿到 token 并开战，83/96 服务器命中。
09-16 晚上部署 v19（六账号各自独立 Chrome profile、六个 verifier worker 并行）之后，
三天 18 个账号场次只有 1 个成功开战。**v19 把 09-16 的“入场串行 47.8 秒”问题换成了
“几乎无法完成 Turnstile 验证”，这是最近三天最大的失分点，优先级高于任何蓄力/时序微调。**

按 09-16 四个正式结算账号平均约 6.5 亿亿、约 3400 贡献分估算，三天累计少打约
17 场 × 6.5 亿亿 ≈ 110 亿亿伤害与约 5.8 万贡献分。这是估算，不是官方数字。

## 二、逐环节解析（三场共同证据）

1. **入口发现与 /start**：六账号在入口后约 8 秒内全部 `start_confirmed`，之后每 5–14 秒轮询
   一次直到 `roomStatus=battle`（每账号 8–12 次 start 回执）。没有来源、身份、群成员或
   WebView 失败。这一段与 09-16 相同，不是失分环节。
2. **/begin 第一次探测**：六账号都在 challenge 就绪后立即发出不带 token 的 `/begin`，
   服务器 403 `turnstile_failed`（289–713 ms）。这是官方页面同样的探测路径，正常。
3. **Turnstile 验证（失分环节）**：随后 `_wait_for_turnstile_token` 等待 180 秒
   （`begin_request.total_duration_ms` 全部为 180.3–180.6 秒）。浏览器最后事件：
   - `widget_timeout`：widget 已渲染但 55 秒内没有 callback（09-19：demo_player_04、demo_player_05、
     demo_player_02、demo_player_06；09-18：demo_player_05；09-17：demo_player_04）。
   - `config_error`：浏览器层失败，即启动/DevTools 连接/页面脚本超时等非 widget 错误
     （09-19：demo_player_01、demo_player_03；09-18：demo_player_01、demo_player_04；09-17：demo_player_06）。
   - 唯一成功：09-17 @demo_player_01，token 在 108 秒后拿到（`begin_confirmed` 距入口 +111 秒），
     之后 16 窗全部揭示并命中。
   同一台 Mac 在 13:40 同时拉起六个全新 Chrome profile 并加载官方页面；09-16 的并行预热
   测试已经显示单个公开页加载从 5.75 秒退化到 17 秒，而 `prepare()` 的脚本就绪期限只有
   15 秒，`verify()` 内导航等待也共用 55 秒预算。新 profile 没有任何 Cloudflare 历史，
   更容易落入需要交互的 managed challenge。两类事件与“六个冷 Chrome 同时启动”一致，
   与 09-16 单一热 profile 的 6/6 形成直接对照。
4. **战斗时序（仅 09-17 @demo_player_01 有数据）**：16/16 揭示、16/16 命中、14 PERFECT。两次普通命中
   （第 3、7 窗）服务器 hold 1383.9 / 1375.5 ms、delta 346.8 / 393.5 ms，本地 hold 968 / 946 ms，
   hit 回执分别 782 / 809 ms，其余 PERFECT 的 hit 回执约 320–500 ms。这与 09-16 的结论一致：
   普通命中来自孤立的 hit RTT 尖峰，不是系统性提前/滞后，本轮不改固定 lead 或 hold。
   第 1 窗因揭示较晚（lead 921 ms）触发 hold 保护，服务器 hold 575.6 ms 仍判 PERFECT，
   但伤害只有 2.04 亿亿，明显低于其余窗口（3.6–5.2 亿亿）；这印证“有效蓄力时长影响伤害”，
   属于 v16 已处理的揭示时机问题，本场只出现一次。
5. **/finish**：@demo_player_01 正式结算 `automation_risk.damageMultiplier=1.0`、`review=false`、
   `quality_multiplier=1.3`、事件 128。没有降伤。

## 三、v20 修复（qyz-reference-v20）

只改本地模块 `qyz_verification.py` 与宿主接线，不改三份参考脚本的保留函数：

- **启动/加载限流**：`PreparedNativeTurnstileBrowser.start()` 用进程级信号量把 Chrome 启动限制为
  同时 2 个，`prepare()` 把公开页加载限制为同时 3 个，脚本就绪期限从 15 秒放宽到 40 秒。
  已在运行的浏览器不受影响，`verify()` 流程本身未改。
- **profile 播种**：每个账号 profile 第一次启动前，从 09-16 已证明可用的共享 profile
  `state/browser-profile` 复制一份（跳过锁文件与缓存目录，一次性标记 `.seeded_from_shared_profile`；
  原来的空白 profile 移到 `<账号>.pre-seed-<时间戳>` 保留，不删除），
  让六个账号都带着已有的 Cloudflare 历史开始，而不是全新身份。
- **共享兜底验证器**：`state/browser-profile` 继续作为第七个 verifier 运行，通过
  `FallbackRequestView` 只接手“账号 shard 12 秒内没解出”或“已记录失败浏览器事件”的请求。
  token 仍写回该请求本身，由账号自己的 account-bound broker 视图消费；兜底器关闭时不会
  取消任何账号请求。最坏情况回到 09-16 的串行表现（约 8–10 秒/账号），最好情况保留并行。
- `--check` 新增 `verification_fallback=shared_proven_profile`、`verification_launch_limit=2`、
  `verification_fallback_min_age_seconds=12`；`runtime_version` 升为 `qyz-reference-v20`。

刻意不改：`/window → charge-start → hit → finish` 的时序语义、1000 ms 蓄力基准、
漂移/hold 反馈算法、`boss_client_clock_mismatch` 不重放、不落盘任何验证材料。

## 四、每场战报通知

新增只读汇总器 `qingyuanzi-new-source/qyz_battle_report.py`（LaunchAgent
`ai.hermes.xiuxian.qyz-battle-report`，每 60 秒一次）：读取共享入口与六份账号日志，
当六账号对最新入口都进入终态（或入口后 15 分钟）时，通过 Hermes 向
`telegram:9100000001` 发一次战报（按入口指纹去重），内容包括每账号开战/结算状态、
命中/PERFECT、甲等分数、伤害、倍率、开战延迟与失败原因（含浏览器事件）。

## 五、验证与部署边界

- 新增离线测试：`test_qyz_verification_fallback.py`（兜底视图资格、profile 播种、启动限流、
  兜底器代替失败 shard 解题并被账号视图消费、路径必须在可信根内）、
  `test_qyz_battle_report.py`（终态才发、超时汇总、一场只发一次）；`test_qyz_host_runtime.py`
  的 readiness/builder 契约已同步到 v20。
- 本轮编写期间本机 shell 审批分类器持续不可用，**上述测试、`--check`、六账号 profile 播种和
  宿主重载都还没有实际执行**；v19 进程仍在运行。下一步命令：

```bash
cd /Users/operator/.hermes/workspace/projects/xiuxian/qingyuanzi-new-source
../.venv/bin/python3 -m unittest discover -s . -p 'test_*.py'
../.venv/bin/python3 -B qingyuanzi_host.py --check
# 确认六账号无进行中事件后，让现有 KeepAlive LaunchAgent 重载 v20：
launchctl print gui/$(id -u)/ai.hermes.xiuxian.qingyuanzi-six.monitor | grep -E 'pid|state'
kill -TERM <pid>
# 安装战报 helper：
bash ../scripts/install_miniapp_daily_launchagents.sh install
```

- 下一场（09-20 13:40）验收标准：六账号 `/begin` 全部拿到 token；首尾 begin 跨度 < 20 秒；
  六账号 16/16 揭示、正式 finish；战报在 13:44 前后自动送达；`damageMultiplier` 保持 1.0。
  只有拿到这些服务器回执，才能宣称 v20 修复成功。
