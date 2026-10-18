# 2026-09-20 青元子一周复盘：结构拆解、逐环节失分与 v21 修复

复盘对象：09-15 至 09-20 六场（事件 125–131，入口 12337832 / 12355818 / 12373121 /
12389522 / 12406242 / 12421535）。所有时间 Asia/Shanghai。证据只取六账号各自的
`battle_diagnostics/<入口ID>.json`（服务器回执、传输时序、浏览器事件）与 09-20 官方结算榜
（群消息 12421596）。认证材料、Turnstile token 均未进入本文与代码。

## 一、一周成绩总览（服务器正式结算）

| 场次 | 开战 | 正式结算 | 甲等 100 分 | 命中 / PERFECT | 主要失分 |
| --- | ---: | ---: | ---: | --- | --- |
| 09-15 事件 126 | 6/6 | 4/6 | 4 | 61/64 命中，56 PERFECT | demo_player_05、demo_player_06 `/finish` 被拒 `boss_duration_too_short` |
| 09-16 事件 127 | 6/6 | 4/6 | 4 | 61/64，55 PERFECT | 同上两账号 `boss_client_clock_mismatch` |
| 09-17 事件 128 | 2/6 | 2/6 | 2 | 31/32，27 PERFECT | 4 账号 `world_boss_turnstile_timeout`（v19 并行验证退化） |
| 09-18 事件 129 | 2/6 | 2/6 | 2 | 31/32，29 PERFECT | 4 账号 Turnstile 超时 |
| 09-19 事件 130 | 0/6 | 0/6 | 0 | 0 | 6 账号 Turnstile 超时 |
| 09-20 事件 131 (v20) | 6/6 拿到 token | 4/6 | 2 | 32/96，20 PERFECT | 见下：读连接冷启动 + 提前量过大 |

前三名对照（09-20 官方贡献榜，参战 33 人）：第 1–3 名均 3760 分、强攻 16、伤害 7.66 / 7.39 / 7.31 亿亿。
本方最好成绩 @demo_player_04 第 10 名 3714 分、6.93 亿亿；分差 46 分（16/16 命中、15 PERFECT）。
其余五个账号未上榜前十。一周只有 09-15、09-16 两场达到 4 账号全甲等；三天（09-17..19）几乎空转。

**结论：v20 已修好验证（09-20 六账号 100–150 秒内全部拿到 token），本周最大的失分点已从
“进不了场”转移到“进场后的网络时序”。** 这正是本次 v21 针对的三处。

## 二、MiniApp 青元子完整结构（asc.aiopenai.app）

1. **入口**：群公告 `【世界通告｜真仙试锋开启】`（hantianzun35_bot 等）带 `qyz_` 深链，60 秒入场期。
2. **`/start`**：入场并轮询 `roomStatus` 直到 `battle`，每 5–14 秒一次，共 7–9 次；回复中的
   `challenge` 只有 `attacks[16]`、`windowCount=16`、`durationMs=300000`，**没有窗口时刻表**。
3. **`/begin`**：先无 token 探测（403 `turnstile_failed`），浏览器解 Turnstile 后再带 token 提交；
   成功回复 `startsInMs`（本周恒为 1499 ms）与 `serverStartedAtMs`。本地战斗时钟 = 收到回复时刻 +
   startsIn − RTT/2；固定提前量 `request_lead_ms = clock_rtt/2`，其中 `clock_rtt` 取探测 403 的 RTT。
4. **`/window`**：以 360 ms 节奏轮询，逐个揭示窗口 `{id, centerMs, hitMs=620, perfectMs=210}`，
   揭示通常领先中心 1.5–1.9 秒。读请求走独立 2 连接“读池”，1 秒超时，错误预算 25 次。
5. **`/charge-start` → `/hit`**：每窗口先取 chargeTicket，本地蓄力约 1000 ms 后打击，
   `hit` 回复 `deltaMs`、`holdMs`、`perfect`、`damageYi`、`bossHp`。服务器判定：|delta| ≤ 620 命中，
   ≤ 210 且 520 ≤ hold ≤ 1250 为 PERFECT。三次漏窗/反攻会致死（`player_hp=0`，`quality_multiplier=0.35`）。
6. **`/finish`**：带 bossProof 提交，返回 `grade/score/hits/perfects/realtime_damage_yi/quality_multiplier`
   与 `automation_risk.damageMultiplier`（本周全为 1.0，无降伤）。总分上限 100，贡献分≈伤害驱动。
7. **结算榜**：约入口后 7 分钟发布 `【世界通告｜真仙试锋败退】`，含 `贡献榜`（分、强攻数、伤害）与 `保底结算`。

## 三、逐环节解析（一周 216 次服务器命中的统计）

- **入场/`start`**：六账号均在 8 秒内 start_confirmed，不失分。
- **验证**：v19 三天 18 场次仅 3 个开战；v20（限流 2 启动 / 3 加载、profile 播种、共享兜底器）
  09-20 六账号全部拿到 token，开战延迟 165–213 秒。本环节已修。
- **窗口揭示（09-20 新失分点）**：`/begin` 前浏览器验证等待 100–150 秒，期间读池空闲，源站已关闭连接；
  第一批 `/window` 全部在 **冷 TLS 握手** 中被 1 秒超时切断（demo_player_01 24 次、demo_player_02 25 次、
  demo_player_06 24 次、demo_player_03 12 次连续 `api_unreachable`，握手完成时间 0.8–1.1 秒）。
  结果：demo_player_01 / demo_player_02 耗尽错误预算 `boss_windows_invalid` 未开打；demo_player_03 / demo_player_06 前 3 窗
  揭示落后 1.5–9.6 秒，打击全被判 `boss_window_expired / boss_hit_outside_window`，被反攻三次致死 0 分。
  demo_player_04 / demo_player_05 只是运气：第一次冷读 1.03 秒失败后第二次刚好 0.93 秒成功。
- **打击时序**：服务器 `deltaMs` 与伤害强相关：|delta| < 150 时中位 0.44–0.45 亿亿/击且 100% PERFECT，
  210–300 时 0.28 亿亿且 0% PERFECT。一周 29 次非 PERFECT 中 24 次来自 **提前量过大**：
  `clock_rtt` 取自冷探测 403（09-17 后 560–811 ms），而热连接的真实 `hit` RTT 只有 360–410 ms；
  提前量 280–406 ms 比实际单程多 100–200 ms，漂移控制器下限 −200 ms 无法完全抵消。
  09-20 @demo_player_05：lead 406、漂移已压到 −192 仍有 11 窗 delta 210–392（5/16 PERFECT，5.41 亿亿）；
  同场 @demo_player_04 lead 281 → 15/16 PERFECT。`AdmissionClock` 本应用同事件 `/start` 样本纠正，但
  90 秒样本有效期在 100–150 秒的验证等待后全部过期（本周所有场次 `sample_count=0`）。
- **蓄力**：PERFECT 内 hold 950–1250 ms 伤害 0.45–0.46 亿亿，500–800 ms 只有 0.29；第 1 窗因揭示晚、
  蓄力被压缩，均值 0.29。与前三名的 0.4–0.5 亿亿/击差距正是这里。
- **`/finish`**：09-15/16 demo_player_05、demo_player_06 两账号被拒（`boss_duration_too_short`、
  `boss_client_clock_mismatch`），09-17 之后未再出现（v20 之前已修）。

## 四、v21 修复（本地模块，不改参考脚本受保护函数）

1. **读池预热与保活**（`qyz_http.HttpTransport.warm_read_pool`，`qyz_protocol.BoundMonitor`）：
   `/begin` 等待验证期间每 40 秒用只读 `/state` 保活读池连接（最长 300 秒）；`_reveal_windows` 开始前
   先把两条读池连接握手完成，再进入 1 秒预算的揭示循环。
2. **冷读退让**（`read_pool_cold_failures` + `WINDOW_READ_COLD_TIMEOUT_SECONDS=2`）：上一次 `/window`
   死在握手阶段时，下一次读放宽到 2 秒，而不是连续烧掉错误预算。
3. **提前量上限**（`qyz_timing.effective_request_lead`）：每窗口用本场已完成、连接复用的 action 请求
   最小 RTT 的一半为上限修正 `request_lead_ms`（≥3 个样本才生效），并把证据写入
   `request_lead_reviewed` 事件；同时 `AdmissionClock` 样本有效期 90→300 秒、参考值改为网络下限。
   按 09-20 数据，demo_player_05 的 406 会被压到 182，与 demo_player_04 一致。
4. **战报对比前三**：宿主只读捕获官方结算榜写入 `state/world_boss_shared_board.json`，
   `qyz_battle_report` 追加“贡献榜前三 / 本方上榜 / 与第一名分差”。
5. `RUNTIME_VERSION` → `qyz-reference-v21`；战报 LaunchAgent `ai.hermes.xiuxian.qyz-battle-report`
   今日实际安装并已送出 09-20 战报（Hermes message 33790）。

刻意不改：`/window → charge-start → hit → finish` 语义、1000 ms 蓄力基准、hold/漂移反馈算法、
`boss_client_clock_mismatch` 不重放、不落盘任何验证材料。

## 五、验证与验收

- 离线：`qingyuanzi-new-source` 176 个单元测试通过（新增 5 个：热 RTT 上限、冷探测不降低提前量、
  揭示前预热、冷读只放宽下一次、结算榜解析与排名行）；`--check` 显示 v21。
- 宿主 22:08 已重载到 v21（pid 57071），六账号健康状态显示 `runtime_version=qyz-reference-v21`。
- 明天 13:40 事件 132 验收标准：六账号 `/begin` 全部拿到 token；`window_reveal.error_count ≤ 2`；
  六账号 16/16 揭示、正式结算；每账号 `request_lead_reviewed.lead_ms ≤ 220`；PERFECT ≥ 14/16；
  战报含“贡献榜前三”行且本方至少 4 账号进入前十。只有拿到这些服务器回执才算修复成功。
