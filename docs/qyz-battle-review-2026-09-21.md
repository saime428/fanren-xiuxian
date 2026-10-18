# 2026-09-21 青元子复盘：事件 132 整场未入场（0/6）、根因、v22 修复与 Surge/MTProto 排查

复盘对象：09-21 13:40 事件 132（入口 12436550，结算榜 12436634，参战 30 人，结果“天道败退”）。
所有时间 Asia/Shanghai。证据：宿主日志 `state/monitor.launchd.error.log`、六账号 `world_boss_state.json`、
只读拉取的官方公告原文、Surge CLI 只读查询与临时规则实测。认证材料未进入本文与代码。

## 一、成绩

| 项目 | 结果 |
| --- | --- |
| 入场 / 开战 / 结算 | 0/6 / 0/6 / 0/6，六账号 journal 里没有本场记录（最后一场仍是 09-20 的 12421535） |
| 官方前三 | @sample_peer_16 3760（7.42 亿亿）、@sample_peer_10 3760（7.38）、@sample_peer_15 3741（7.09） |
| 本方上榜 | 无 |
| 战报 | 当天没有发出；09-22 01:00 补发“本方未参战”战报（Hermes message 33864） |

v21 的三处时序修复（读池预热 / 冷读退让 / 提前量上限）没有得到任何一次线上执行，09-22 13:40 是它们的第一次实战。

## 二、根因：v21 把入口公告当成结算榜丢掉了（代码缺陷，不是网络）

v21（09-20 22:27 提交）在 `qingyuanzi_host.py::process_message` 开头加了结算榜捕获：
“消息文本含 `贡献榜` → 解析榜单 → `return False`”。而官方入口公告本身就写着
“.世界boss 可查看战况，.世界boss榜 可查看贡献榜。” 于是 13:40:00 的入口 12436550 在 60 秒入场期内
被六个账号的每一条路径（Telethon 推送、5 秒补扫、共享入口信号）一致地静默丢弃：

- 宿主 13:39:35–13:41:36 持续收到 `Got difference for channel 9400000004`（入口所在群），进程活着、连着；
- 没有任何 `QYZ protocol` / `queued` / `preparing_webview` 日志，没有 `/start`，共享入口文件仍指向 09-20；
- 当晚健康检查里 `source_checks.entry_found=true, message_id=12436550`：同一条消息通过同一条 MTProto 代理读到了，只是被判成榜单；
- 离线复现：对真实入口文本，HEAD（v21）`process_message` 返回 False，修复后返回 True。

单元测试当时之所以通过，是因为入口夹具的文本只有“世界通告 真仙试锋开启”，没有那句“可查看贡献榜”。

## 三、Surge / MTProto 代理与本次失败的关系：无

用户问题：“青元子的失败跟我 SURGE 设置了 MTPROTO 协议有关系吗？”

- 项目里 MTProto 只用于 Telethon 与 Telegram 的连接（`config/accounts/mtproto.env` → Surge `[MTProto]` 127.0.0.1:5753，
  `surge-cli dump profile` 已核对）。它不承载游戏 API：`asc.aiopenai.app` 走 Surge 规则
  `RULE-SET ai.conf → AI → 🇺🇸 美国节点`（`surge-cli rule explain` 结果）。
- 13:39–13:41 这条代理是通的：入口消息按时到达宿主（见上），之后的健康检查也读到了它。
  当天的代理重连集中在 04:00（Surge 记录“IPv4 and IPv6 both unavailable”，美国节点连 149.154.167.91 超时）、
  10:53、13:47:43（战斗结束之后）与 16:09，都不在入场窗口内。
- Surge 日志 13:30–13:50 没有任何错误；`GetHistoryRequest flood wait 3s`（13:40:58）是 Telegram 侧限流，且在入场期之后。

结论：不需要按 Surge skill 修改 MTProto 设置；本次失败纯粹是宿主代码缺陷。

顺带实测游戏 API 路由（临时规则 `DOMAIN,asc.aiopenai.app,<策略>`，测完已 `rule temp flush`；`/state` 空体 POST，热连接）：

| 策略 | 热请求中位 / P90（ms，5 轮 40 次） | 冷连接中位（ms） |
| --- | ---: | ---: |
| 🇺🇸 美国节点（现行） | 450 / 603 | 1417 |
| 🇭🇰 香港节点 | 358 / 625 | 1202 |
| DIRECT | 393 / 683 | 1006 |

09-20 实战中经美国节点的热 action RTT 中位为 308–335 ms，与今晚各策略的下限（273–293 ms）同量级；各策略差异在同一晚内
的波动范围之内，且 Turnstile 页面（challenges.cloudflare.com）仍走美国节点，改 API 路由会让页面与验证来源 IP 不一致。
**不改路由**；v21 的提前量上限会按实战热 RTT 自适应。

## 四、v22 修复（本地模块）

1. `qingyuanzi_host.process_message`：先提取 `qyz_` 深链；只有没有深链的消息才按 `贡献榜` 解析结算榜。
   新增两条回归测试使用真实入口正文与真实结算正文。
2. `qyz_battle_report`：结算榜比最近一次入场记录更新（宿主整场未入场）时，以榜单消息 id 为键发一条
   “本方未参战 0/6 + 前三名”战报（今天的 33864 就是这条路径发出的）；同一场的正常战报不受影响。
3. `qyz_state.event_needs_reconciliation`：失败记录超过事件寿命（30 分钟）后不再算“待核对”。
   之前 09-15 起的 `finish_submitted` / `begin_submitted` 失败记录让六账号健康状态永远是
   `unfinished_battle_requires_reconciliation`，今天整场缺席没有产生任何新的告警。进程重启恢复仍用无时限规则。
4. `scripts/xiuxian_healthcheck.py`：官方结算榜比所有 journal 记录都新 → `qyz_event_missed: board=<id>`（bad）。
5. `miniapp_shared/notify.py`：记录 `hermes send --json` 的真实错误，而不是 stderr 开头的
   “⚠ A previous `hermes update` … did not restart running gateways” 横幅（09-20/21 共 12 条星宫 / 藏娇阁通知失败
   全被这句横幅遮住；gateway.log 里对应时刻都是 api.telegram.org / 149.154.166.110 连接错误）。
6. `RUNTIME_VERSION` → `qyz-reference-v22`；宿主 01:18 重载（新 pid），六账号 `runtime_version=qyz-reference-v22`，
   `unresolved_event_count=0`。

## 五、附带核实：天机试炼“失败报告”不属实

- 六账号 09-21 01:31–01:41 全部 `completed 3/3`、`receipt_confirmed=true`（含 @demo_player_01 示例绑定角色），
  Hermes cron 24 次执行全部 ok；正确的 `【天机试炼】… 全部账号完成 6/6` 已于 09:39 送达（message 33807）。
- 用户看到的 7 条 `日终统计：完成 0/6 … 无记录（missing）`（09:41–09:48、20:56、21:10）来自
  `tianji_trial/test_tianji_trial.py`：它用临时目录跑真实 coordinator，coordinator 结束时调用真实 `notify_if_due`，
  03:05 之后临时目录里只有夹具“主魂 / 绑定角色”，于是把假的日终统计发到了 Telegram。
- 修复：测试注入记录型 notifier 并设 `MINIAPP_NOTIFY_DISABLED=1`；`report.notify_if_due` 对非生产根目录且未注入
  sender 的调用直接返回 `skipped_non_production_root`；`--dry-run` 改写 `dry_run.json`，不再覆盖当天的 `summary.json`
  （我今晚的一次 dry-run 曾把 demo_player_01 的 summary 覆盖成 `ready`，已按 cron 输出原样恢复）。
- 洞府入口轮换（09-21 15:30 换到游戏群）对天机的影响：天机与垂钓共用同一入口发现逻辑，dry-run 已用新入口成功认证
  （两身份 ready），09-22 01:30 正常运行。

## 六、09-22 验收标准（事件 133）

六账号 `queued → start_confirmed → begin_confirmed`；`window_reveal.error_count ≤ 2`；`request_lead_reviewed.lead_ms ≤ 220`；
六账号正式结算，PERFECT ≥ 14/16；战报含“贡献榜前三”与本方名次；健康检查不出现 `qyz_event_missed`。
只有拿到这些服务器回执才算 v21 时序修复与 v22 入场修复都成立。
