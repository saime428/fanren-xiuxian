# 2026-09-24 青元子复盘：v24 首战 6/6 开战，剩余失分全部来自同进程停顿；v25

复盘对象：09-24 13:40 事件（入口 12481545，13:40:10 发出；结算榜 12481652，参战 53 人，天道败退）。
时间均为 Asia/Shanghai。Turnstile token、initData、签名 URL 均未进入本文与代码。

## 一、成绩

| 账号 | 命中 / PERFECT | 伤害（亿亿） | 榜单 |
| --- | --- | ---: | --- |
| @demo_player_02 | 16 / 14 | 7.17 | 第 9 |
| @demo_player_03 | 16 / 14 | 7.08 | 第 10 |
| @demo_player_06 | 16 / 15 | 6.98 | — |
| @demo_player_01 | 15 / 13 | 6.88 | — |
| @demo_player_04 | 15 / 15 | 6.80 | — |
| @demo_player_05 | 15 / 13 | 6.73 | — |

- **开战 6/6、正式结算 6/6、命中 93/96、PERFECT 84**（09-23：4/6、64、56）。两人进前十。
- 蓄力目标 1100 生效：PERFECT 的服务器 hold 中位 1085–1105 ms；六账号 `damageMultiplier` 全部 1.0。
- 前 8 名全部 16/16（7.21–7.65）。按今天各窗 PERFECT 伤害中位数，本方一场 16/16 可打 **7.59**（第 2–3 名档），
  各账号实际少 0.41–0.86，全部是执行失分。

## 二、验证（v24 首战）

- 预解：4 个 token 在房间开放前备好（交付 0 秒），@demo_player_05 请求后 4 秒拿到，@demo_player_06 由串行路径在请求后 23 秒拿到。
- 慢在页面：入场 13:40:14 后才启动 Chrome 并加载官方页，用了 19 秒，第一个预解 13:40:33 才开始（首个 13 秒，之后每个 5.5–6.7 秒）。

## 三、失分逐项（都能在时间线上对上）

1. **13:41:23.7–26.7 宿主进程整体停顿约 3 秒**：六场战斗的 `/window` 读取几乎全停（3 秒内只有 5 条，平时每 360 ms 5 条）。
   同一时刻验证器正在为 @demo_player_06 解 widget，参考实现在点击前截一张整窗 PNG 并丢弃——本机 4K 屏上是 4.5 MB；
   接收它的线程与战斗线程共用解释器锁。损失：@demo_player_01 第 3 窗未发出（唤醒晚 2307 ms）、@demo_player_04 第 4 窗晚 767 ms 才揭示
   （`boss_window_expired`）、@demo_player_05 第 3 窗 hold 只剩 312 ms。
   离线复现（六个战斗式线程 + 本地 HTTP 服务）：整窗截图让计时器最多晚 270 ms（平时 10 ms）；1 像素裁剪截图无影响。
2. **13:42:38–40 四个账号在 1.6 秒内结算**：每个都要在宿主进程里对 157 KB 结果做脱敏、深拷贝、JSON、fsync。
   仍在打的 @demo_player_05 最后一窗（全场最值钱的一击）唤醒晚 897 ms，未发出。
3. 13:41:42 源频道一波更新（六个 Telethon 客户端各自解码），@demo_player_02 第 6 窗晚 226 ms → 非 PERFECT。
4. 上游尖峰（hit 往返 650–960 ms）6 次；charge 尖峰使 @demo_player_02 第 9 窗 hold 499 < 520；
   @demo_player_05 第 5 窗 delta 177 但 hold 1330（hit 往返 581 ms 叠加 1100 计划）。

## 四、v25 改动

1. **每日预热 + 入场前预解**（`SerialVerificationService._daily_warm`）：13:39:00 起浏览器与官方页常驻到 13:47:00；
   13:39:50 起为六个账号各解一个 token（token 不绑定事件，按账号保存、只用一次、240 秒过期）；入场时缺的仍按原路补。
   解完即 **停靠到 about:blank**（官方页空闲动画占约 20% 单核，about:blank 0.3%），真需要 widget 时再加载。
   演练（真实页面，不提交）：预热 3.9–8.6 秒，六个 token 13:39:50 后约 20 秒全部备好，房间开放时六个请求 **0.0 秒**交付；
   停靠后空闲 Chrome 占用 0.05%。
2. **截图裁成 1 像素**（`PreparedNativeTurnstileBrowser.command`）：点击前那一帧仍会渲染，但不再传 4.5 MB。
3. **结算证据延后**（`qingyuanzi_host.light_protocol_result` / `wait_for_battles_to_finish`）：结算时立刻落一份轻量结果
   （计数、`server_result`、倍率、失败摘要，战报与健康检查只读这些），完整结果与归档等本进程所有战斗结束（最多 150 秒）再写；
   开战前的可重试失败照旧立即记录。
4. **解释器切换间隔 5 ms → 0.5 ms**（`BATTLE_SWITCH_INTERVAL_SECONDS`）：同一 1.1 秒 CPU 突发下六个计时器最坏延迟
   349 → 51 ms（1 ms 时 137 ms），覆盖 Telethon 解码等无法搬走的突发。
5. **蓄力目标 1100 → 1150**：今天普通命中的服务器 hold 相对计划最多 +50 ms（p95 +24）；hold 每 +100 ms 伤害 +11%；
   在 1150 下今天 84 次 PERFECT 只有 1 次会越过 1250。
6. `RUNTIME_VERSION` → `qyz-reference-v25`；宿主 2026-09-24 23:32 重启。

## 五、09-25 验收

- 宿主日志：13:39 `QYZ verifier page warm`，13:39:50 后六条 `presolved ... outcome=stored`，随后 `parked`；
  六账号 `/begin` 交付等待均 < 1 秒，开战时间相差 < 3 秒。
- 各账号唤醒延迟 p99 < 60 ms，无 > 300 ms 的停顿；`local_window_missed` 与 `boss_window_expired` 为 0。
- PERFECT 的服务器 hold 中位 1130–1170；越过 1250 的 PERFECT 损失合计 ≤ 1；`damageMultiplier` 全部 1.0；本方最好 ≥ 7.4 亿亿。
- 结算后各账号 journal 先出现轻量结果，最后一场结束后才出现完整结果与 `battle_diagnostics/<入口>.json`。
