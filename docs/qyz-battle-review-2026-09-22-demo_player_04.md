# 2026-09-22 青元子复盘：@demo_player_04 已开战，未正式结算，被误报为未开战

## 范围与结论

- 单一事件：官方入口消息 `12451184`，发布时间 `2026-09-22 13:40:09 Asia/Shanghai`。
- 目标账号：`tg9100000004` / `@demo_player_04`；运行版本 `qyz-reference-v22`。
- 证据来自同一入口的六账号不可变 battle_diagnostics、目标账号 journal、monitor.launchd.error.log 和现行源码；没有发起新的游戏请求或 Telegram 消息。
- **不是未入场或未开战。** 目标账号 `/start` 和 `/begin` 均成功，服务器确认 5 次命中、0 PERFECT；处理到第 12 窗后本地判死，`/finish` 返回 HTTP 400 `boss_client_clock_mismatch`，没有正式结算成功证据。
- 自动战报存在独立错误：异常路径没有产生汇总 accepted_hits，报表将缺失值变成 0，并将 0 命中等同于未开战，忽略了 begin_confirmed 与已保存的逐击回执。

## 1. 时间线（北京时间）

- 13:40:09：入口发布。
- 13:40:17.010：目标账号首次 `start_confirmed`，已进入本场流程。
- 13:41:13.383：日志记录请求官方浏览器验证。
- 13:42:24.338、13:43:05.141：两次 `browser_connection_timeout`。
- 13:43:36.673：日志记录浏览器提交验证材料；没有在本复盘保存任何验证值。
- 13:43:39.261：服务器 `/begin` 成功，记录 `begin_confirmed`。
- 13:43:39.298：记录 `verification_accepted`。先前验证延迟不是本场最终未开战的原因，因为后来已成功开战。
- 13:43:39.646 左右：由各窗口 `observed_at - observed_elapsed_ms / 1000` 反推的本地战斗时钟零点；这是本地推导值，不是服务端 serverStartedAtMs。
- 13:44:48.451：最后一个已处理窗口（第 12 窗）的观测记录。
- 13:44:49：账号记录进入 `runtime_error`；日志精确终态时间 13:44:49.397。唯一 `/finish` 请求 HTTP 400，耗时 424.254 ms，原始错误 `boss_client_clock_mismatch`。

## 2. 实际逐窗结果

动态目标为 16 窗；记录里只有 12 个已处理窗口：

- 第 1、3、4、5、10、12 窗：`local_window_missed`，共 6 窗，没有发出 `/hit`。
- 第 2 窗：发出 `/hit`，服务端拒绝 `boss_hit_outside_window`。
- 第 6、7、8、9、11 窗：5 条不同 window_fingerprint 的服务器成功命中回执，均 `server_perfect=false`。
- 第 13–16 窗：没有处理记录；不应记成已揭示或已尝试。
- 目标账号最终 `local_player_hp=0`、`local_dead=true`。这是客户端账本，不是服务端死亡结算。

服务器成功命中回执总伤害为 99,416,593 亿（逐击之和），不代表已获得正式结算或奖励。

## 3. 失败链：证实部分与待证部分

### 已证实：窗口处理启动时已普遍落后于蓄力计划

12 窗实际 `charge_sent_ms - charge_target_ms` 全为正：最小 554 ms，中位 826.5 ms，最大 1441 ms。

第 1 窗计划在 1069 ms 开始蓄力，实际在 2510 ms 提交；中心是 2238 ms。计划进入等待阶段时已到 2235 ms，只距中心 3 ms，远不够完成计划的 1000 ms 蓄力。

该现象并非简单等于每个 HTTP 请求特别慢：目标账号 `/window` RTT 中位 356.4 ms、`/charge-start` RTT 中位 344.1 ms，与同场正常账号接近。落后在进入 charge 阶段之前已经出现。

### 强线索：本地战斗时钟与请求提前量可能失配

- 目标账号两次 `/begin` 传输耗时约 2483 ms（403）和 2337 ms（200）。
- 源码以 begin RTT 的一半修正 battle_start；再由另一个函数单独把每窗 request_lead 限到热连接 RTT 的一半。
- 目标账号反推的本地零点只在 begin_confirmed 后约 385 ms；同场其他账号约 1215–1494 ms。
- 目标账号首窗 request_lead 已被限到 169 ms，但这不会回改已经建立的 battle_start。
- 这与“时钟补偿过大、而打击提前量另外被收紧，导致收到窗口时本地已显得很晚”相符。

**证据限制：** 目标失败分支没有保存完整 `clock_sync`、`begin_response`、`challenge`、`window_reveal` 和最终 bossProof。传输 RTT 不等于上层 trace RTT，不能把其他账号的参数当作本账号原始值。因此尚不能宣布具体时钟偏移值或最初延迟来源已被完全证明，更不能武断归因某代理节点、CPU 或服务器。

相关源码：`qyz_reference/world_boss_features.py:3065–3102,3191–3200`；`qyz_timing.py:45–74,77–109`；`qyz_protocol.py:409,443–448`。

### 已证实：本地死亡会提前中断结算等待

- `qyz_combat.py:37–55`：漏窗扣本地 HP，归零置 dead。
- `qyz_protocol.py:424–428`：将本地 dead 合并进 `_boss_stop_requested()`。
- `qyz_reference/world_boss_features.py:3296–3316`：虽然计算了 minimum_finish_at，实际使用可被 stop 打断的 `_sleep_until()`；没有检查返回值是否真的等到 deadline。
- 同文件 `756–776`：stop=true 时 `_sleep_until` 直接返回 False，不等待。
- 同文件 `3357–3367`：提交 proof.durationMs 仍取 max(minimum_duration_ms, last_window_end+grace, last_action_ms, elapsed_ms)。

因此存在明确的“没有真实等到截止时刻，proof 时长却仍取最低门槛”的代码缺陷。离线调用真实 `_sleep_until` 方法验证：虚拟当前 69.3 s、目标 75.05 s、stop=true，立即返回 False，时间没有推进。此为离线 fixture，不是伪造实战回执。

本场目标账号终态约在本地时钟 69.4–69.8 s；同场其余五账号的服务端 challenge 均为 minDurationMs=75000。本账号 challenge/bossProof 未保存在失败归档中，**不能声称已经直接读到它提交了 75000 ms**。该缺陷与本次 `boss_client_clock_mismatch` 高度一致，但服务器具体拒绝判据、最终 proof 数值未完整留存，保留此证据边界。

## 4. 为什么战报写成“未开战”

三层信息丢失/误用：

1. `qyz_http.py:34–41,206–213`：允许的错误码集合没有 `boss_client_clock_mismatch`，对外降为 `server_response_rejected`；原码仍在 diagnostics.error，可恢复。
2. `qyz_protocol.py:472–519`：accepted_hits/server_perfects 等汇总只在正常结算返回路径组装；finish 抛错后只携带异常详情与 transport_diagnostics。
3. `qyz_battle_report.py:100–105,126–131,189`：只读 record/result 的 accepted_hits，缺失当 0；0 命中便写“未开战”，开战人数也只看 hits 或 settled 状态。即使同一行又从 begin_confirmed 写出“开战 +210s”，也没有纠正前半句。

离线用真实 account_line() 读取本场归档，复现：

`@demo_player_04: 未开战，server_response_rejected（finish_submitted），开战 +210s`

事实应改述为：

**已开战；服务器确认命中 5/16，PERFECT 0；仅处理 12/16 窗，本地判死；finish 被拒 boss_client_clock_mismatch，未正式结算。**

## 5. 全队正确口径

逐账号读取本场六份归档，并按 window_fingerprint 去重校验：

- `/begin` 已确认：6/6，而非战报的 5/6。
- 正式结算已确认：5/6，原数正确。
- 已确认逐击命中合计：83/96，而非 78/96；多出的 5 次来自目标账号。
- PERFECT 合计：57，原数正确。

注意：83 是逐击已确认命中总数，不代表六账号全部正式结算。

## 6. 修复方向（本次未实施）

1. 失败路径始终保留 begin_response/clock_sync、challenge、逐窗 reveal、hit 回执计数及脱敏 finish proof；不能仅在成功后才归档完整证据。
2. 战报分开报告“开战 / 已确认命中 / 正式结算”，开战以 begin_confirmed 为准；异常时从不同窗口的服务器回执恢复计数，不把缺失当零。
3. 为已观测的 boss_client_clock_mismatch 增加安全分类，不遮盖原始可审计错误码。
4. 将“停止攻击”和“可以跳过结算时间约束”分开；最终 duration 必须与真实经过的时长一致，禁止为了通过门槛虚报时长或盲重试 finish。
5. 在完整保存 begin/clock/reveal 证据后修正时钟与 request_lead 的一致性；不根据单次 unsigned delta 猜方向或套固定偏移。

本次只做复盘与离线验证，没有改生产代码、账号状态、调度、代理或游戏配置；没有重放旧入口，没有停止/重载服务。19:52 只读检查确认青元子 LaunchAgent 仍 running，PID 45446。

独立委托复核因 HTTP 429 限额未完成，不计作审查通过。结论由主助手直接读取归档、源码并执行离线探针获得。
