# 2026-09-23 青元子复盘：两个账号没进战斗的根因，以及 v24

复盘对象：09-23 13:40 事件 134（入口 12466890，13:40:15 发出；结算榜 12467015，参战 38 人，天道败退），
并回看 09-15 以来全部归档（六账号 `battle_diagnostics/*.json`，384 次逐击回执）。时间均为 Asia/Shanghai。
Turnstile token、initData、签名 URL 均未进入本文与代码。

## 一、成绩

| 账号 | 结果 | 命中 / PERFECT | 伤害（亿亿） | 榜单 |
| --- | --- | --- | ---: | --- |
| @demo_player_06 | 正式结算 | 16 / 15 | 6.78 | 第 4 |
| @demo_player_04 | 正式结算 | 16 / 14 | 6.63 | 第 9 |
| @demo_player_03 | 正式结算 | 16 / 14 | 6.59 | — |
| @demo_player_05 | 正式结算 | 16 / 13 | 6.35 | — |
| @demo_player_01 | **未开战** `world_boss_turnstile_timeout` | 0 | 0 | — |
| @demo_player_02 | **未开战** `world_boss_turnstile_timeout` | 0 | 0 | — |

前三 7.21 / 6.90 / 6.88。v23 的时序修复生效：唤醒延迟 p90 7–32 ms（09-22 为 129–209），非 PERFECT 每账号 1–3 次。

## 二、两个账号为什么没进战斗（最不应该出现的问题）

时间线（六账号相同）：13:40:21 起 `/start` 每 5 秒一次，房间 `joining`（倒计时 48 秒）→ 13:41:11 房间变 `battle`、
挑战出现 → 无 token 的 `/begin` 探测 403 `turnstile_failed` → 排队等浏览器 token，截止 13:44:11–15（180 秒）。

- 六个账号各自的 Chrome 加一个共享兜底 Chrome，**七个 widget 在 13:41 同时渲染，第一、二轮全部 55 秒超时**
  （`turnstile_browser_timeout` / `browser_connection_timeout`）。
- 13:43:16 各账号分片用完两次尝试停下后，只剩兜底浏览器单独工作：13:43:45、13:43:54、13:44:02、13:44:10 连续拿到 4 个
  token（每个 8–9 秒）。排在最后的 @demo_player_01（13:44:11 截止）、@demo_player_02（13:44:15 截止）没轮到。
- 这不是今天的偶发：v19（09-16 晚）改成六个并行 profile 之后，**每一场的第一轮验证都超时**，token 都挤在 13:43–13:44：
  09-17 4/6 超时、09-18 4/6、09-19 6/6、09-20 迟到、09-22 最后一个 13:44:06（离截止 7 秒）、09-23 2/6。
  对照 v18 单一热浏览器串行（09-15、09-16）：12/12 token，每个 8–13 秒。
- 离线复现（20:00 以后，profile 复制到 /tmp，不提交任何 token）：串行 2–18 秒、七路并发 17–23 秒、宿主原路径 9–10 秒，
  全部成功。profile 本身没有问题；13:41 的七路同时求解（本机 8 GB 内存、swap 已用 4.6 GB，同一出口 IP 同时七个挑战）才会卡死。
- 官方页面的 Turnstile 配置是静态的（`action=qyz_world_boss_begin`，无 cData），token 不绑定挑战，有效期 300 秒；
  而入口后房间固定有约 48 秒 `joining` 等待。

## 三、v24 修复（`qyz_verification.SerialVerificationService`，参考脚本受保护函数未动）

1. **单一热浏览器串行验证**：只用已被证明的共享 profile `state/browser-profile`，一次只渲染一个 widget；
   真实验证请求永远优先，按账号声明顺序；每次 widget 预算 35 秒（原 55 秒），每个请求最多 3 次。
   六个账号 profile 与兜底并行器不再启动（目录保留在磁盘上）。
2. **入场等待期预解 token**：账号通过来源/身份/成员/政策检查后 `prepare_event(..., account=)` 登记需求；
   浏览器空闲时按账号顺序为每个已入场账号解一个 token，仅存在宿主内存（`PresolveTurnstileBroker`），
   按 (事件, 账号) 绑定、只交付一次、240 秒过期。403 之后该账号的第一个验证请求创建即被满足；
   若预解 token 被拒，参考流程的第二次 handoff 仍会请求新 token。
3. 离线端到端（真实页面、真实 widget，不提交）：页面 11 秒就绪，六个 token 预解用时 6.5/2.9/2.4/2.4/2.9/2.9 秒，
   请求创建时 **0.0 秒**拿到；入场晚到时（请求先于预解）串行补齐六个用时 28 秒，远小于 180 秒。
4. `RUNTIME_VERSION` → `qyz-reference-v24`；`--check` 报告 `verification_parallelism=1`、预解策略、widget 超时。

## 四、历史参数分析与第二项改动：蓄力目标 1000 → 1100 ms

- 384 次回执里 55 次非 PERFECT **全部是晚到**（delta > 210）；其中 32 次同时伴随 hit 往返 700–1000 ms
  （正常 300–450），连接全部复用、请求体 30–80 ms 内发出，服务器迟迟才收到——是上游链路（Surge 美国节点 /
  Cloudflare / 源站）的尖峰，且常在同一秒内同时打到两三个账号。客户端无法消除。
- 普通命中的到达方向（早/晚）用现有数据无法可靠判定（对称链路假设下存在镜像解），因此**不移动打击中心**。
- **蓄力时长是最大的可控项**：300 次 PERFECT（按窗口序号与 |delta| 校正）相对 1000–1049 ms：
  1050–1099 +5.2%，1100–1149 +8.5%，1150–1199 +8.6%，1200+ +11.5%；七个比赛日每天都是 ≥1100 组最高。
  v23 之后普通命中的服务器 hold 相对计划 p95 仅 +54..+68 ms（最大 +137）。计划提到 1100：普通命中最坏约 1237 < 1250；
  历史上只有 10/300 次 PERFECT 会越界（−29% × 3.3%），换来全部命中约 +8.5%，净期望约 +7–8%。
  实现为适配层 `BoundMonitor._planned_hold_ms`（`qyz_timing.TARGET_SERVER_HOLD_MS = 1100`），保留参考的 hold 偏差学习与上限夹紧。
- **风控约束**：服务器 `automation_risk` 已有三种标记——“完美命中比例异常”“命中精度异常稳定”（09-23 @demo_player_03
  `preciseTimingRatio=0.75`，风险分 26）“整轮近乎无损完成”。09-14 @demo_player_06 16/16 PERFECT、|delta| 全部 ≤42、
  hold 968–1055 被判 `review=true`、逐击倍率 0.18/0.55。所以目标不是更“准”，而是少晚到、hold 更长；
  本次改动不收窄 delta 或 hold 的离散度。

## 五、09-24 验收（只认服务器回执）

- 验证：六账号 `begin_confirmed`，宿主日志出现六条 `QYZ verifier presolved ... outcome=stored`，
  每个 `/begin` handoff 等待 < 5 秒；不再出现 `world_boss_turnstile_timeout`。
- 成绩：PERFECT 命中的服务器 hold 中位 1080–1120；越过 1250 的 PERFECT 损失每账号 ≤ 1；
  `automation_risk.damageMultiplier` 全部 1.0；本方最好成绩 ≥ 7.0 亿亿。
- 若预解 token 被 `/begin` 拒绝（日志里第二次 `requires browser verification`），说明服务器要求挑战出现后生成的 token，
  把预解需求改到 `room_status=battle` 之后即可，串行验证本身保留。
- 未做（需用户决定）：比较美国节点 / 香港 / DIRECT 在 13:40 的尾部延迟（hit 往返 > 600 ms 的比例）；改路由会让
  Turnstile 页面与 API 出口 IP 不一致，需要先确认。
