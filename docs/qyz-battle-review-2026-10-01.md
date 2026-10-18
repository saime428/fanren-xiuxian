# 青元子复盘：2026-10-01（Asia/Shanghai）

## @demo_player_03：已核实的参战结果

账号 `tg9100000003`，请求身份为主魂；入口消息 `12575291`，服务端 event `142`，运行记录 `qyz-reference-v28`。

- 13:40:15.824 首次 `/start` 确认。
- 13:41:12.336 `/begin` 确认；13:41:12.362 验证接受（验证请求 13:41:11 提交/消费）。
- 13:42:42.989 `/finish` 确认；host 在 13:42:43.087 记录 `settled_partial`。
- 16 个窗口全部取得，reveal 错误 0；有效命中 **15**，PERFECT **13**，失败 **1**；本地漏窗口 0。
- 服务端结算：甲等，score 100，存活、HP **84**，伤害 **671,755,747 亿 = 6.71755747 亿亿**。
- 伤害倍率 **1.0**，`automation_review=false`，风险记录 score 10、flags 空；没有伤害折扣或进入审查的证据。
- 因而不是没有入场、验证码没过、死亡或没有提交结算。

## 本账号可见的损失

| 窗口 | 证据 | 结果 |
| --- | --- | --- |
| 3 | `/hit` 1295 ms，HTTP 409 `boss_hit_outside_window`；本地 wake lateness 3 ms | 一击被拒绝 |
| 6 | `/hit` 1238 ms；服务端 delta 630.6 ms、hold 1718.1 ms；本地 wake lateness 3 ms | 普通命中，非 PERFECT |
| 12 | `/hit` 597 ms；服务端 delta 142.5 ms、hold 1276.3 ms；本地 wake lateness 3 ms | 服务端判为普通命中 |
| 2 | `/window` 905 ms，取得窗口时已晚于计划蓄力点；蓄力启动晚约 420 ms | 虽 PERFECT，但实际 server hold 727.1 ms |
| 16 | `/window` 1058 ms，取得窗口时已晚于计划蓄力点；蓄力启动晚约 425 ms | 虽 PERFECT，但实际 server hold 801.7 ms |

全场 `/hit` 请求耗时中位数 350.5 ms；上述两次 1.2–1.3 秒响应明显偏慢。
本地出手唤醒中位数 3 ms、最大 33 ms，未见这一账号因本地调度阻塞而漏窗。
失败的第 3 击与第 6 击均复用已建立的 HTTP 连接；第 3 击 body 已在请求起点后约 43 ms 发完，但到约 1288 ms 才收到响应头；第 6 击分别约 20 ms 与 1155 ms。
主要异常位于请求发出后的响应等待链路，不是这两次请求临时做 TLS 握手。仅凭客户端证据**不能进一步认定是代理、网络传输还是服务端排队**。

对比上一日（2026-09-30，入口 `12562443`）：16 命中 / 15 PERFECT，伤害 773,858,067 亿；本日少 **102,102,320 亿（13.19%）**。这是结果对比，不将全部差额硬分摊给单一因素。

## 官方榜单与全队对照

已直接读取官方消息 [12575315](https://t.me/example_game_group/7310786/12575315) 核实：**伐仙功成，53 人参战，贡献榜只公布前 10 名**。Telegram 原始发布时间为 **13:44:06**；本地 shared board 的 `published_at=13:44:45` 与其不同，应作为本地记录时间看待，不拿它替代原消息时间。

| 账号 | 官方排名 | 榜单贡献分 | 有效命中 | PERFECT | 伤害（亿亿） |
| --- | --- | --- | --- | --- | --- |
| @demo_player_06 | 1 | 3760 | 16/16 | 15 | 7.81 |
| @demo_player_04 | 3 | 3760 | 16/16 | 15 | 7.24 |
| @demo_player_02 | 4 | 3760 | 16/16 | 14 | 7.21 |
| @demo_player_05 | 7 | 3723 | 16/16 | 15 | 6.98 |
| @demo_player_01 | 9 | 3710 | 16/16 | 12 | 6.90 |
| @demo_player_03 | 前十之外，实际名次未知 | 未公布 | 15/16 | 13 | 6.71755747（个人结算） |

第 10 名为 **@sample_peer_14：3705 贡献分、强攻 16、伤害 6.87 亿亿**。002 的伤害较第十名公布的四舍五入数字低约 **0.1524 亿亿（2.22%）**。榜单列的是贡献分，个人 `/finish` 的甲等 `score=100` 不是同一个分数；没有 002 的官方贡献分或全量排名，不能凭伤害推断其恰好第 11 名。

**结论：002 有入场、有结算，只是未进入本次公告展示的前十；不是漏参战。** 其余五账号都有 `finish_confirmed=true`、零 missed window、最低伤害倍率 1.0。全队 6/6 正式结算，95/96 有效命中、84 PERFECT，本场唯一被拒绝的一击属于 002。

战报通知也正常：本地投递记录显示 13:42:53 战报 `34594`（6/6 开战与结算）及 13:44:57 榜单跟进 `34595` 均 `success=true`，未见漏发或将 002 当成没参战。

## 后续判断

优先核查的是复用 HTTP 连接上的响应长尾（窗口读取与 `/hit`），而不是重复修改已通过的入场验证或把本地唤醒再压低几毫秒。现有证据不足以把责任单独归到 Surge、某条代理线路或游戏服务端；不据此盲改路由、提前量或其他生产参数。

## 证据与边界

- 官方榜单：Telegram 消息 `12575315`（已只读回查原文）；本地快照 `qingyuanzi-new-source/state/world_boss_shared_board.json`。
- 全队通知：`qingyuanzi-new-source/state/qyz_battle_report/notifications.json:163–184`；其余五账号的 `world_boss_state.json` 按同一事件 fingerprint 核对。
- 本账号完整证据：`qingyuanzi-new-source/state/accounts/tg9100000003/battle_diagnostics/12575291.json`，关键位置 `record.protocol_result.server_result`、`reference_outcome.diagnostics.hits`、`window_reveal.log`、`transport_diagnostics`。
- 前日对照：同目录 `12562443.json`。
- host 交叉确认：`qingyuanzi-new-source/state/monitor.launchd.error.log:773045–773046,774517`。
- 本次仅复盘已有结果；未改生产代码、参数、网络路由，未重启服务，也未发游戏指令。
